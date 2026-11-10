from collections.abc import Mapping
from typing import TYPE_CHECKING

from posthog.hogql.database.models import FunctionCallTable
from posthog.hogql.errors import ExposedHogQLError, QueryError
from posthog.hogql.escape_sql import escape_hogql_identifier

from posthog.dataclasses import frozen

if TYPE_CHECKING:
    from sqlglot import exp
    from sqlglot.optimizer.scope import Scope
    from sqlglot.tokens import Token as DialectToken

    from posthog.hogql.context import HogQLContext

VIEW_CTE_PREFIX = "__ph_rv_"
MAX_VIEW_DEPTH = 32
MAX_EXPANDED_SQL_LENGTH = 1_000_000


@frozen
class RawTrinoView:
    name: str
    sql: str


@frozen
class RawTrinoViewCatalog:
    """The raw Trino views a caller can read, keyed by exact view name."""

    views: Mapping[str, RawTrinoView]


def raw_trino_view_sql(saved_query_json: object) -> str:
    sql = saved_query_json.get("query") if isinstance(saved_query_json, dict) else None
    return sql if isinstance(sql, str) else ""


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lex(sql: str) -> list["DialectToken"]:
    # The direct_sql package imports the warehouse sources product, which this module must not load at import time.
    from posthog.hogql.direct_sql.raw_sql import lex_direct_sql  # noqa: PLC0415

    return lex_direct_sql(sql, "trino")


def _words(tokens: list["DialectToken"]) -> list["DialectToken"]:
    from posthog.hogql.direct_sql.raw_sql import literal_token_types  # noqa: PLC0415 -- see _lex

    literals = literal_token_types()
    return [token for token in tokens if token.token_type not in literals]


def _statement_end(tokens: list["DialectToken"]) -> int:
    from sqlglot.tokens import TokenType  # noqa: PLC0415 -- load dialect grammars only for Trino queries

    end = len(tokens)
    while end > 0 and tokens[end - 1].token_type == TokenType.SEMICOLON:
        end -= 1
    return end


def _clean_body(view: RawTrinoView) -> str:
    """The view SQL without comments or trailing semicolons, so it can sit inside a subquery.

    Comments go too, because the parameter binder reads a quote inside a comment as the start of a string.
    """
    tokens = _lex(view.sql)
    tokens = tokens[: _statement_end(tokens)]
    if not tokens:
        raise ExposedHogQLError(f"View '{view.name}' has no SQL.")
    pieces: list[str] = []
    previous_end: int | None = None
    for token in tokens:
        if previous_end is not None:
            gap = view.sql[previous_end + 1 : token.start]
            pieces.append(gap if gap.isspace() or not gap else " ")
        pieces.append(view.sql[token.start : token.end + 1])
        previous_end = token.end
    return "".join(pieces)


@frozen
class _Edit:
    """Replace `sql[start:end]` with `text`."""

    start: int
    end: int
    text: str


def _apply(sql: str, edits: list[_Edit]) -> str:
    pieces: list[str] = []
    position = 0
    for edit in sorted(edits, key=lambda edit: (edit.start, edit.end)):
        pieces.append(sql[position : edit.start])
        pieces.append(edit.text)
        position = edit.end
    pieces.append(sql[position:])
    return "".join(pieces)


def _has_alias(table: "exp.Table") -> bool:
    from sqlglot import exp  # noqa: PLC0415 -- load dialect grammars only for Trino queries

    node: exp.Expression = table
    while True:
        if node.args.get("alias"):
            return True
        if not isinstance(node.parent, exp.Subquery):
            return False
        node = node.parent


class RawTrinoViewExpansion:
    """One expansion run. Each view becomes one CTE, so a view read twice or along two paths is hoisted once."""

    def __init__(self, catalog: RawTrinoViewCatalog) -> None:
        self.catalog = catalog
        self.ctes: list[str] = []
        self.cte_names: dict[str, str] = {}
        self.size = 0
        # Trino ignores identifier case, so views whose names differ only by case cannot be told apart.
        self.views: dict[str, RawTrinoView] = {}
        self.ambiguous_names: set[str] = set()
        for view in catalog.views.values():
            key = view.name.lower()
            if key in self.views:
                self.ambiguous_names.add(key)
            self.views[key] = view
        for key in self.ambiguous_names:
            del self.views[key]
        self.lookup_words = {word for name in (*self.views, *self.ambiguous_names) for word in (name, *name.split("."))}

    def statement(self, sql: str) -> str:
        """Hoist the views a raw Trino statement reads."""
        tokens = _lex(sql)
        return self._with_ctes(sql, tokens, self._rewrite(sql, tokens, path=[]))

    def hoist_view(self, name: str) -> str:
        """The CTE name for a view that HogQL resolved by exact name. Pass the printed statement to `with_ctes` after."""
        view = self.catalog.views.get(name)
        if view is None:
            raise QueryError(f"Unknown raw Trino view '{name}'.")
        return self._hoist(view, [])

    def with_ctes(self, sql: str) -> str:
        return self._with_ctes(sql, _lex(sql), [])

    def _with_ctes(self, sql: str, tokens: list["DialectToken"], edits: list[_Edit]) -> str:
        """Put the hoisted views first in the statement's WITH.

        A Trino WITH member sees only the members before it, so a CTE of the statement cannot shadow a table a view
        reads.
        """
        if not self.ctes:
            return _apply(sql, edits)

        from sqlglot.tokens import TokenType  # noqa: PLC0415 -- load dialect grammars only for Trino queries

        ctes = ", ".join(self.ctes)
        first = tokens[0]
        if first.token_type == TokenType.WITH:
            if len(tokens) > 1 and tokens[1].text.upper() == "RECURSIVE":
                # Trino requires column aliases on every CTE of a recursive WITH, and a hoisted view has none.
                raise ExposedHogQLError(
                    "A WITH RECURSIVE query cannot read a saved view. Copy the view's SQL into the query instead."
                )
            edit = _Edit(start=first.end + 1, end=first.end + 1, text=f" {ctes},")
        else:
            edit = _Edit(start=first.start, end=first.start, text=f"WITH {ctes}\n")
        return _apply(sql, [*edits, edit])

    def _rewrite(self, sql: str, tokens: list["DialectToken"], *, path: list[str]) -> list[_Edit]:
        from sqlglot import exp, parse_one  # noqa: PLC0415 -- load dialect grammars only for Trino queries
        from sqlglot.errors import SqlglotError  # noqa: PLC0415
        from sqlglot.optimizer.scope import traverse_scope  # noqa: PLC0415

        words = _words(tokens)
        for token in words:
            if token.text.lower().startswith(VIEW_CTE_PREFIX):
                raise ExposedHogQLError(f"Names that start with {VIEW_CTE_PREFIX} are reserved. Rename '{token.text}'.")
        named = next((token.text for token in words if token.text.lower() in self.lookup_words), None)
        if named is None:
            return []

        end = _statement_end(tokens)
        try:
            tree = parse_one(sql[: tokens[end - 1].end + 1], read="trino") if end else None
            scopes = traverse_scope(tree) if isinstance(tree, exp.Query) else None
        except (SqlglotError, RecursionError):
            scopes = None
        if scopes is None:
            raise ExposedHogQLError(f"Could not parse the query to expand view '{named}'.")

        edits: list[_Edit] = []
        for scope in scopes:
            ctes = {name.lower() for name in scope.cte_sources}
            # A parenthesized table such as `FROM (daily) d` gets its own scope with the table as the expression.
            tables = [scope.expression] if isinstance(scope.expression, exp.Table) else scope.tables
            for table in tables:
                parts = table.parts
                if not isinstance(table.this, exp.Identifier) or not all(isinstance(p, exp.Identifier) for p in parts):
                    continue
                key = ".".join(part.name for part in parts).lower()
                if len(parts) == 1 and key in ctes:
                    continue
                if key in self.ambiguous_names:
                    raise ExposedHogQLError(
                        f"Several views are named '{parts[-1].name}' when case is ignored. Rename one."
                    )
                view = self.views.get(key)
                if view is None:
                    continue
                start, stop = parts[0].meta.get("start"), parts[-1].meta.get("end")
                if start is None or stop is None:
                    raise ExposedHogQLError(f"Could not parse the query to expand view '{view.name}'.")
                aliased = _has_alias(table)
                alias = "" if aliased else f" AS {_quote(parts[-1].name)}"
                edits.append(_Edit(start=start, end=stop + 1, text=self._hoist(view, path) + alias))
                if len(parts) > 1 and not aliased:
                    edits.extend(self._requalify(scope, tables, table, view))
        for edit in edits:
            self._grow(len(edit.text))
        return edits

    def _requalify(
        self, scope: "Scope", tables: list["exp.Table"], table: "exp.Table", view: RawTrinoView
    ) -> list[_Edit]:
        """Point `reports.daily.x` and `reports.daily.*` at the `daily` alias that replaces the dotted name."""
        name = table.parts[-1].name
        if sum(1 for other in tables if other.alias_or_name.lower() == name.lower()) > 1:
            raise ExposedHogQLError(
                f"View '{view.name}' and another table in the query are both named '{name}'. Give one an alias."
            )
        dotted = [part.name.lower() for part in table.parts]
        edits: list[_Edit] = []
        for column in (*scope.columns, *scope.stars):
            qualifier = column.parts[:-1]
            if [part.name.lower() for part in qualifier] != dotted:
                continue
            start, stop = qualifier[0].meta.get("start"), qualifier[-1].meta.get("end")
            if start is None or stop is None:
                raise ExposedHogQLError(f"Could not parse the query to expand view '{view.name}'.")
            edits.append(_Edit(start=start, end=stop + 1, text=_quote(name)))
        return edits

    def _grow(self, length: int) -> None:
        self.size += length
        if self.size > MAX_EXPANDED_SQL_LENGTH:
            raise ExposedHogQLError("The saved views this query reads expand to too much SQL. Read fewer views.")

    def _hoist(self, view: RawTrinoView, path: list[str]) -> str:
        key = view.name.lower()
        lowered = [name.lower() for name in path]
        if key in lowered:
            cycle = " -> ".join([*path[lowered.index(key) :], view.name])
            raise ExposedHogQLError(f"Views reference each other in a cycle: {cycle}.")
        # HogQL tells views apart by case, so the CTE is keyed by the exact name.
        if view.name in self.cte_names:
            return self.cte_names[view.name]
        if len(path) >= MAX_VIEW_DEPTH:
            raise ExposedHogQLError(f"Views are nested more than {MAX_VIEW_DEPTH} levels deep: {' -> '.join(path)}.")

        body = _clean_body(view)
        expanded = _apply(body, self._rewrite(body, _lex(body), path=[*path, view.name]))
        self._grow(len(expanded))
        cte_name = f"{VIEW_CTE_PREFIX}{len(self.ctes)}"
        self.ctes.append(f"{cte_name} AS (\n{expanded}\n)")
        self.cte_names[view.name] = cte_name
        return cte_name


def expand_raw_trino_view_references(sql: str, catalog: RawTrinoViewCatalog) -> str:
    """Hoist each raw Trino view the SQL reads into a leading CTE, since Trino has no table of that name.

    The original text is kept. Only the table references change, so comments, literals and formatting reach Trino as
    written.
    """
    return RawTrinoViewExpansion(catalog).statement(sql)


class RawTrinoViewTable(FunctionCallTable):
    """A saved view whose SQL is raw Trino SQL. Only the Trino printer can read it, as a hoisted CTE."""

    requires_args: bool = False

    def to_printed_hogql(self) -> str:
        return escape_hogql_identifier(self.name or "")

    def _refuse(self, dialect: str) -> str:
        raise QueryError(f"View '{self.name}' runs raw Trino SQL, so it cannot be printed into {dialect} SQL.")

    def to_printed_clickhouse(self, context: "HogQLContext") -> str:
        return self._refuse("ClickHouse")

    def to_printed_postgres(self, context: "HogQLContext") -> str:
        return self._refuse("Postgres")

    def to_printed_duckdb(self, context: "HogQLContext") -> str:
        return self._refuse("DuckDB")
