import re
from dataclasses import dataclass, field

from posthog.schema import HogQLNotice

from posthog.hogql import ast
from posthog.hogql.constants import FLAG_CALLED_EVENT
from posthog.hogql.context import HogQLContext
from posthog.hogql.database.models import Table
from posthog.hogql.database.schema.events import EventsTable
from posthog.hogql.database.schema.flag_evaluations import FlagEvaluationsTable
from posthog.hogql.database.schema.util.where_clause_extractor import event_field_type, unwrap_table_alias_type
from posthog.hogql.resolver import resolve_types
from posthog.hogql.transforms.flag_calls_from_flag_evaluations import EventsScan
from posthog.hogql.visitor import TraversingVisitor, clone_expr

from posthog.dataclasses import frozen

# The copy names the two steps of the move as the troubleshooting doc that the "Learn more" link opens names them.
FLAG_CALLED_ON_EVENTS_NOTICE = (
    f"Once PostHog stops writing flag calls to the events table, this query reads {FLAG_CALLED_EVENT} events from "
    "posthog.flag_evaluations, which keeps the last 90 days of flag calls. If part of the query can't read that "
    "table, the query fails and says which part."
)

FLAG_CALLED_ON_EVENTS_AFTER_MOVE_WARNING = (
    f"PostHog no longer writes {FLAG_CALLED_EVENT} events to the events table, so this query, which reads them from "
    "there, returns only older ones. Query posthog.flag_evaluations to include recent flag calls."
)


def flag_called_fails_after_move_warning(reason: str) -> str:
    return (
        f"Once PostHog stops writing flag calls to the events table, this query fails because {reason}. Change that "
        "part of the query, or query posthog.flag_evaluations directly."
    )


# Querying posthog.flag_evaluations directly does not help here, because that table lacks the field too.
def flag_called_missing_field_warning(field_name: str) -> str:
    return (
        f"Once PostHog stops writing flag calls to the events table, this query fails because it reads {field_name}, "
        "which posthog.flag_evaluations doesn't have. Change that part of the query."
    )


FLAG_CALLED_FROM_FLAG_EVALUATIONS_NOTICE = (
    f"This query reads {FLAG_CALLED_EVENT} events from posthog.flag_evaluations, which keeps the last 90 days "
    "of flag calls."
)

FLAG_CALLED_FROM_BOTH_TABLES_NOTICE = (
    f"This query reads {FLAG_CALLED_EVENT} events from posthog.flag_evaluations, which keeps the last 90 days "
    "of flag calls, and other events from events."
)

# While events still holds every flag call, the union reads only the recent ones from posthog.flag_evaluations.
FLAG_CALLED_SPLIT_BY_AGE_NOTICE = (
    f"This query reads {FLAG_CALLED_EVENT} events from the last 90 days from posthog.flag_evaluations. "
    "It reads older ones and all other events from events."
)


class _FlagCalledLiteralFinder(TraversingVisitor):
    def __init__(self, table_class: type[Table]) -> None:
        super().__init__()
        self.table_class = table_class
        self.literals: list[ast.Constant] = []

    def visit_select_query(self, node: ast.SelectQuery) -> None:
        # The resolver inlines a saved view's body, parsed from the view's own text. Its offsets do not
        # point into the user's query.
        if node.view_name is not None:
            return
        super().visit_select_query(node)

    def visit_alias(self, node: ast.Alias) -> None:
        # The resolver inlines a saved expression's body in a hidden alias. The body's offsets point into its own text.
        if node.hidden:
            return
        super().visit_alias(node)

    def visit_field(self, node: ast.Field) -> None:
        # The inherited visit follows the field's type into shared CTE types, which takes exponential time.
        pass

    def visit_compare_operation(self, node: ast.CompareOperation) -> None:
        if node.op in (ast.CompareOperationOp.Eq, ast.CompareOperationOp.In, ast.CompareOperationOp.GlobalIn):
            for field_side, value_side in ((node.left, node.right), (node.right, node.left)):
                if _is_event_field_of(field_side, self.table_class):
                    self.literals.extend(_flag_called_constants(value_side))
        super().visit_compare_operation(node)


def _is_event_field_of(expr: ast.Expr, table_class: type[Table]) -> bool:
    field_type = event_field_type(expr)
    if field_type is None:
        return False
    table_type = unwrap_table_alias_type(field_type.table_type)
    return isinstance(table_type, ast.TableType) and isinstance(table_type.table, table_class)


def _flag_called_constants(expr: ast.Expr) -> list[ast.Constant]:
    if isinstance(expr, ast.Constant):
        return [expr] if expr.value == FLAG_CALLED_EVENT else []
    if isinstance(expr, (ast.Tuple, ast.Array)):
        return [constant for sub in expr.exprs for constant in _flag_called_constants(sub)]
    return []


def count_flag_calls_read_from_events(resolved: ast.AST) -> int:
    """How many `$feature_flag_called` literals a resolved query compares to the events table's `event` column.

    Unlike the warnings below, it counts a literal that a query builder made, which has no span.
    """
    finder = _FlagCalledLiteralFinder(EventsTable)
    finder.visit(resolved)
    return len(finder.literals)


def flag_called_on_events_warnings(
    node: ast.SelectQuery | ast.SelectSetQuery, context: HogQLContext
) -> list[HogQLNotice]:
    """A warning on each `$feature_flag_called` literal that a query compares to the events table's `event` column.

    The query resolves on a clone, because the printed AST has been rewritten and its offsets no longer match
    what the user typed.
    """
    # A caller that passes an already printed AST never builds the database, and its AST may be rewritten.
    if context.database is None:
        return []
    finder = _FlagCalledLiteralFinder(EventsTable)
    with context.timings.measure("flag_called_warnings"):
        finder.visit(resolve_types(clone_expr(node), context, dialect="clickhouse"))
    # A literal that a variable supplies has no span, and a notice without one marks the whole query.
    return [
        HogQLNotice(message=FLAG_CALLED_ON_EVENTS_NOTICE, start=literal.start, end=literal.end)
        for literal in finder.literals
        if literal.start is not None and literal.end is not None
    ]


@frozen
class LiteralSpan:
    start: int | None
    end: int | None


def flag_called_spans_read_from_flag_evaluations(prepared: ast.AST) -> set[LiteralSpan]:
    """The spans of the `$feature_flag_called` literals that a prepared AST compares to `flag_evaluations.event`."""
    finder = _FlagCalledLiteralFinder(FlagEvaluationsTable)
    finder.visit(prepared)
    return {LiteralSpan(start=literal.start, end=literal.end) for literal in finder.literals}


def flag_called_spans_read_from_union(context: HogQLContext) -> set[LiteralSpan]:
    """The spans of the `$feature_flag_called` literals whose scan reads `events` and `flag_evaluations` together."""
    return {LiteralSpan(start=start, end=end) for start, end in context.flag_called_union_spans}


_GROUP_ON_EVENTS = re.compile(r"goe_\d+")


@dataclass(frozen=False)
class _SelectFrame:
    scan: EventsScan | None
    ctes: frozenset[str]
    warning: str | None
    literals: list[LiteralSpan] = field(default_factory=list)


class _MoveRefusalFinder(TraversingVisitor):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[_SelectFrame] = []
        self.refusals: dict[LiteralSpan, str] = {}

    def visit_select_query(self, node: ast.SelectQuery) -> None:
        ctes = (self.frames[-1].ctes if self.frames else frozenset()).union(node.ctes or ())
        scan = EventsScan.of(node, lambda: True, ctes)
        reason = scan.shape_refusal(node) if scan else None
        warning = flag_called_fails_after_move_warning(reason) if reason else None
        self.frames.append(_SelectFrame(scan=scan, ctes=ctes, warning=warning))
        super().visit_select_query(node)
        frame = self.frames.pop()
        if frame.warning is not None:
            self.refusals.update(dict.fromkeys(frame.literals, frame.warning))

    def visit_field(self, node: ast.Field) -> None:
        frame = self.frames[-1] if self.frames else None
        if frame is None or frame.scan is None or frame.warning is not None:
            return
        chain = frame.scan.column(node)
        # flag_evaluations has goe_N.key and no other goe_N field.
        if chain and len(chain) > 1 and _GROUP_ON_EVENTS.fullmatch(str(chain[0])) and chain[1] != "key":
            frame.warning = flag_called_missing_field_warning(f"{chain[0]}.{chain[1]}")

    def visit_constant(self, node: ast.Constant) -> None:
        if self.frames and node.value == FLAG_CALLED_EVENT and node.start is not None and node.end is not None:
            self.frames[-1].literals.append(LiteralSpan(start=node.start, end=node.end))


def flag_called_move_refusals(node: ast.SelectQuery | ast.SelectSetQuery) -> dict[LiteralSpan, str]:
    """The warning for each `$feature_flag_called` literal whose query fails once flag calls are only in
    posthog.flag_evaluations, keyed by the literal's span.

    It reads only the text of the unresolved query. It finds the shapes that the swap refuses and the `goe_N` fields
    that flag_evaluations lacks. It misses a refusal that only resolution shows, such as a name that binds to another
    column in flag_evaluations.
    """
    finder = _MoveRefusalFinder()
    finder.visit(node)
    return finder.refusals
