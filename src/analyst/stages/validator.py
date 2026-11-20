"""SQL safety checker.

Every query - whether written by the LLM or by a template - passes through here before it
can run. The checker works on the parsed AST (sqlglot), not on regexes, and fails closed:
anything it doesn't positively recognise as a safe read is rejected.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from analyst.core.errors import AccessDenied, UnsafeQuery
from analyst.core.models import ValidatedQuery
from analyst.core.semantic import SemanticLayer, User
from analyst.security import rbac

DIALECT = "duckdb"

# Statement / node types that must never appear anywhere in the tree.
_FORBIDDEN_NODE_NAMES = [
    "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter", "AlterTable",
    "TruncateTable", "Command", "Pragma", "Copy", "Attach", "Detach", "Set", "Use",
    "Transaction", "Commit", "Rollback", "Grant", "Install", "Load", "Export",
]
FORBIDDEN_NODES = tuple(
    getattr(exp, n) for n in _FORBIDDEN_NODE_NAMES if isinstance(getattr(exp, n, None), type)
)

# Functions that touch the filesystem, network or engine internals.
FORBIDDEN_FUNCTION_PREFIXES = ("read_", "duckdb_", "pragma_", "sqlite_", "postgres_", "parquet_")
FORBIDDEN_FUNCTIONS = {
    "glob", "getenv", "current_setting", "query", "query_table", "load_extension",
    "system", "httpfs", "iceberg_scan", "delta_scan", "range", "generate_series",
}


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


class SqlValidator:
    def __init__(self, semantic: SemanticLayer, max_rows: int = 5000):
        self.semantic = semantic
        self.max_rows = max_rows

    # ------------------------------------------------------------------ public
    def validate(self, sql: str, user: User) -> ValidatedQuery:
        tree = self._parse_single_select(sql)
        self._reject_forbidden_nodes(tree)
        self._reject_forbidden_functions(tree)
        tables = self._check_tables(tree, user)
        self._reject_cartesian_joins(tree)
        rbac.check_blocked_columns(tree, user.role)
        rbac.check_scope_literals(tree, user.role)

        controls: list[str] = []
        tree = self._apply_secure_views(tree, user, controls)
        tree, limit_note = self._enforce_limit(tree)
        if limit_note:
            controls.append(limit_note)

        return ValidatedQuery(
            original_sql=sql.strip(),
            sql=tree.sql(dialect=DIALECT, pretty=True),
            tables=sorted(tables),
            applied_controls=controls,
        )

    # ------------------------------------------------------------------ checks
    def _parse_single_select(self, sql: str) -> exp.Query:
        if not sql or not sql.strip():
            raise UnsafeQuery("empty query")
        try:
            statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
        except ParseError as e:
            raise UnsafeQuery(f"could not parse SQL: {e}") from e
        if len(statements) != 1:
            raise UnsafeQuery(
                f"expected exactly one statement, got {len(statements)}",
                user_message="Only a single read-only query is allowed.",
            )
        tree = statements[0]
        if not isinstance(tree, exp.Query):
            raise UnsafeQuery(
                f"statement type {type(tree).__name__} is not allowed",
                user_message="Only read-only SELECT queries are allowed.",
            )
        return tree

    def _reject_forbidden_nodes(self, tree: exp.Expression) -> None:
        for node in tree.walk():
            if isinstance(node, FORBIDDEN_NODES):
                raise UnsafeQuery(
                    f"forbidden operation {type(node).__name__}",
                    user_message="Only read-only SELECT queries are allowed.",
                )

    def _reject_forbidden_functions(self, tree: exp.Expression) -> None:
        for node in tree.find_all(exp.Func):
            name = _function_name(node)
            if name in FORBIDDEN_FUNCTIONS or name.startswith(FORBIDDEN_FUNCTION_PREFIXES):
                raise UnsafeQuery(f"function '{name}' is not allowed")

    def _cte_names(self, tree: exp.Expression) -> set[str]:
        return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}

    def _check_tables(self, tree: exp.Expression, user: User) -> set[str]:
        ctes = self._cte_names(tree)
        used: set[str] = set()
        for table in tree.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                raise UnsafeQuery(f"table function '{table.this.sql()}' is not allowed")
            name = table.name.lower()
            if name in ctes and not table.db:
                continue
            if table.db or table.catalog or name not in self.semantic.allowed_tables:
                raise UnsafeQuery(
                    f"table '{table.sql()}' is not in the allowed schema",
                    user_message=f"The table '{table.sql()}' is not available for analysis.",
                )
            if name not in user.role.allowed_tables:
                raise AccessDenied(
                    f"role '{user.role.name}' cannot read table '{name}'",
                    user_message=f"Your role ({user.role.name}) can't access the {name} table.",
                )
            used.add(name)
        if not used:
            raise UnsafeQuery("query does not read from any allowed table")
        return used

    def _reject_cartesian_joins(self, tree: exp.Expression) -> None:
        for join in tree.find_all(exp.Join):
            has_condition = join.args.get("on") is not None or join.args.get("using")
            if not has_condition:
                raise UnsafeQuery(
                    "join without ON/USING (cartesian product)",
                    user_message="The query contained an unbounded join and was blocked.",
                )

    # ---------------------------------------------------------------- rewrites
    def _apply_secure_views(self, tree: exp.Query, user: User, controls: list[str]) -> exp.Query:
        ctes = self._cte_names(tree)
        targets = [
            t for t in tree.find_all(exp.Table)
            if not (t.name.lower() in ctes and not t.db)
        ]
        rewritten: set[str] = set()
        for table in targets:  # collected first, so new subqueries are not re-processed
            name = table.name.lower()
            view = rbac.secure_view(name, user.role, self.semantic)
            if view is None:
                continue
            alias = table.alias_or_name
            view.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
            table.replace(view)
            rewritten.add(name)
        for name in sorted(rewritten):
            parts = []
            blocked = user.role.blocked_columns.get(name)
            if blocked:
                parts.append(f"hid columns {sorted(blocked)}")
            pred = rbac.scope_predicate(name, user.role)
            if pred:
                parts.append(f"row scope {dict(user.role.scope)}")
            controls.append(f"{name}: " + "; ".join(parts))
        return tree

    def _enforce_limit(self, tree: exp.Query) -> tuple[exp.Query, str | None]:
        cap = self.max_rows + 1  # +1 lets the executor detect truncation
        limit = tree.args.get("limit")
        if limit is None:
            return tree.limit(cap), f"added LIMIT {cap}"
        value = limit.expression
        if isinstance(value, exp.Literal) and value.is_int and int(value.this) > cap:
            return tree.limit(cap), f"capped LIMIT {value.this} -> {cap}"
        if not (isinstance(value, exp.Literal) and value.is_int):
            return tree.limit(cap), f"replaced non-literal LIMIT with {cap}"
        return tree, None
