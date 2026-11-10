from typing import Optional

from django.conf import settings

import posthoganalytics

from posthog.hogql import ast
from posthog.hogql.constants import HogQLGlobalSettings
from posthog.hogql.parser import parse_expr
from posthog.hogql.property import property_to_expr
from posthog.hogql.query import execute_hogql_query

from posthog.clickhouse.client.connection import Workload
from posthog.clickhouse.query_tagging import Feature, Product, tag_queries
from posthog.models.property import GroupTypeIndex, PropertyGroup
from posthog.models.team.team import Team

from products.feature_flags.backend.user_blast_radius import (
    PERSON_BATCH_SIZE as GROUP_AUDIENCE_PAGE_SIZE,
    get_user_blast_radius_persons,
    replace_proxy_properties,
    unevaluable_filters_as_validation_errors,
)

EMAIL_DEDUPE_KEY = "email"
SUPPORTED_DEDUPE_KEYS = (EMAIL_DEDUPE_KEY,)

AUDIENCE_ID_WINDOWS_FLAG = "workflows-batch-audience-id-windows"

# ClickHouse sorts a UUID by its second half first, so a range on that half prunes the person
# table's (team_id, id) primary key. The RFC variant bits put almost every RFC 4122 person id (v4, v5, v7)
# between 8000 and c000 there, so the windows split that range.
_ID_WINDOW_COUNT = 16
_RANDOM_ID_PREFIX_START = 0x8000
_RANDOM_ID_PREFIX_END = 0xC000
_ID_WINDOW_ENDS = [
    f"00000000-0000-0000-{prefix:04x}-000000000000"
    for prefix in range(
        _RANDOM_ID_PREFIX_START,
        _RANDOM_ID_PREFIX_END + 1,
        (_RANDOM_ID_PREFIX_END - _RANDOM_ID_PREFIX_START) // _ID_WINDOW_COUNT,
    )
]


def person_audience_page_size() -> int:
    return settings.WORKFLOWS_PERSON_BATCH_SIZE


def audience_page_size(group_type_index: Optional[GroupTypeIndex]) -> int:
    """
    Page size the resolver must compare a page length against to decide `has_more`.

    Group audiences page through the flags-owned group query, which keeps its own fixed limit.
    """
    if group_type_index is not None:
        return GROUP_AUDIENCE_PAGE_SIZE
    return person_audience_page_size()


def audience_id_windows_enabled(team: Team) -> bool:
    return bool(
        posthoganalytics.feature_enabled(
            AUDIENCE_ID_WINDOWS_FLAG,
            str(team.uuid),
            groups={"organization": str(team.organization_id), "project": str(team.id)},
            group_properties={
                "organization": {"id": str(team.organization_id)},
                "project": {"id": str(team.id), "uuid": str(team.uuid)},
            },
        )
    )


def _clickhouse_uuid_order(uuid: str) -> tuple[int, int]:
    digits = uuid.replace("-", "")
    return int(digits[16:], 16), int(digits[:16], 16)


def id_window_end(cursor: Optional[str]) -> Optional[str]:
    """
    The inclusive end of the id window that starts after `cursor`, or None for the last window.
    """
    if cursor is None:
        return _ID_WINDOW_ENDS[0]
    position = _clickhouse_uuid_order(cursor)
    return next((end for end in _ID_WINDOW_ENDS if _clickhouse_uuid_order(end) > position), None)


def get_batch_audience_person_ids(
    team: Team,
    filters: dict,
    group_type_index: Optional[GroupTypeIndex] = None,
    cursor: Optional[str] = None,
    dedupe_key: Optional[str] = None,
    settings: Optional[HogQLGlobalSettings] = None,
    window_end: Optional[str] = None,
) -> list[str]:
    """
    Enumerate one page of a batch workflow's audience (person UUIDs, cursor-paginated).

    With dedupe_key="email", persons sharing a normalized email collapse to the one with
    the smallest UUID, so an email address receives a given batch send only once. Persons
    without an email are never collapsed.
    """
    if group_type_index is not None:
        # Group keys are already unique; the flags-owned group query needs no dedup.
        return get_user_blast_radius_persons(team, filters, group_type_index, cursor)

    with unevaluable_filters_as_validation_errors():
        cleaned_filter = replace_proxy_properties(team, filters)
        select_query = _build_audience_person_query(
            team, cleaned_filter, cursor=cursor, dedupe_key=dedupe_key, window_end=window_end
        )

        tag_queries(product=Product.WORKFLOWS, feature=Feature.QUERY)
        # Background traffic: the only caller is the internal batch-send resolver, so route to
        # the offline pool like the group branch does, away from interactive product queries.
        response = execute_hogql_query(query=select_query, team=team, settings=settings, workload=Workload.OFFLINE)

    return [str(row[0]) for row in response.results] if response.results else []


def email_dedupe_group_expr() -> ast.Expr:
    # Fields stay fully qualified so nothing resolves to an enclosing query's alias.
    return parse_expr(
        """
        if(
            isNull(persons.properties.email) OR trim(toString(persons.properties.email)) = '',
            toString(persons.id),
            lower(trim(toString(persons.properties.email)))
        )
        """
    )


def _build_audience_person_query(
    team: Team,
    prop_group: PropertyGroup,
    cursor: Optional[str] = None,
    dedupe_key: Optional[str] = None,
    window_end: Optional[str] = None,
) -> ast.SelectQuery:
    where_exprs: list[ast.Expr] = [
        ast.CompareOperation(
            op=ast.CompareOperationOp.Eq,
            left=ast.Field(chain=["persons", "team_id"]),
            right=ast.Constant(value=team.pk),
        ),
        property_to_expr(prop_group, team, scope="person"),
    ]

    if dedupe_key == EMAIL_DEDUPE_KEY:
        return _wrap_with_email_dedupe(where_exprs, cursor)

    if cursor is not None:
        where_exprs.append(
            ast.CompareOperation(
                op=ast.CompareOperationOp.Gt,
                left=ast.Field(chain=["persons", "id"]),
                right=ast.Constant(value=cursor),
            )
        )
    if window_end is not None:
        where_exprs.append(
            ast.CompareOperation(
                op=ast.CompareOperationOp.LtEq,
                left=ast.Field(chain=["persons", "id"]),
                right=ast.Constant(value=window_end),
            )
        )

    return ast.SelectQuery(
        select=[ast.Field(chain=["persons", "id"])],
        select_from=ast.JoinExpr(table=ast.Field(chain=["persons"])),
        distinct=True,
        where=ast.And(exprs=where_exprs),
        order_by=[ast.OrderExpr(expr=ast.Field(chain=["persons", "id"]), order="ASC")],
        limit=ast.Constant(value=person_audience_page_size()),
    )


def _wrap_with_email_dedupe(where_exprs: list[ast.Expr], cursor: Optional[str]) -> ast.SelectQuery:
    """
    One person (min UUID) per normalized email; persons without an email keep their own group.

    The cursor filter MUST sit outside the aggregation: applying `id > cursor` before the
    GROUP BY would recompute min(id) over the remaining persons only, re-emitting an email
    whose persons straddle a page boundary.
    """
    inner_query = ast.SelectQuery(
        select=[ast.Alias(alias="person_id", expr=ast.Call(name="min", args=[ast.Field(chain=["persons", "id"])]))],
        select_from=ast.JoinExpr(table=ast.Field(chain=["persons"])),
        where=ast.And(exprs=where_exprs),
        group_by=[email_dedupe_group_expr()],
    )

    outer_where: Optional[ast.Expr] = None
    if cursor is not None:
        outer_where = ast.CompareOperation(
            op=ast.CompareOperationOp.Gt,
            left=ast.Field(chain=["person_id"]),
            right=ast.Constant(value=cursor),
        )

    return ast.SelectQuery(
        select=[ast.Field(chain=["person_id"])],
        select_from=ast.JoinExpr(table=inner_query),
        where=outer_where,
        order_by=[ast.OrderExpr(expr=ast.Field(chain=["person_id"]), order="ASC")],
        limit=ast.Constant(value=person_audience_page_size()),
    )
