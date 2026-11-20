"""Access control applied by rewriting the SQL AST.

Two mechanisms:
1. Secure views: every base-table reference is replaced with a subquery that projects only
   the columns the role may see and applies the role's row scope. Even `SELECT *` cannot
   leak a blocked column, and rows outside the scope never reach the query.
2. Explicit denial: if the query *asks* for something out of scope (a blocked column, or a
   literal like region = 'South' for a North-only user) we fail loudly instead of silently
   returning an empty or misleading result.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from analyst.core.errors import AccessDenied
from analyst.core.semantic import Role, SemanticLayer

# Tables whose rows belong to a merchant and are scoped through merchant_id.
MERCHANT_SCOPED_TABLES = {"transactions", "settlements"}


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def scope_predicate(table: str, role: Role) -> str | None:
    """SQL predicate restricting `table` to the role's scope, or None if unrestricted."""
    if not role.scope:
        return None
    merchant_conditions = " AND ".join(
        f"{col} IN ({_in_list(vals)})" for col, vals in sorted(role.scope.items())
    )
    if table == "merchants":
        return merchant_conditions
    if table in MERCHANT_SCOPED_TABLES:
        return f"merchant_id IN (SELECT merchant_id FROM merchants WHERE {merchant_conditions})"
    return None


def secure_view(table: str, role: Role, semantic: SemanticLayer) -> exp.Subquery | None:
    """Subquery that replaces `table` for this role, or None if no restriction applies."""
    blocked = role.blocked_columns.get(table, frozenset())
    predicate = scope_predicate(table, role)
    if not blocked and predicate is None:
        return None
    columns = [c for c in semantic.allowed_tables[table] if c not in blocked]
    sql = f"SELECT {', '.join(columns)} FROM {table}"
    if predicate:
        sql += f" WHERE {predicate}"
    return sqlglot.parse_one(sql, read="duckdb").subquery()


def check_blocked_columns(tree: exp.Expression, role: Role) -> None:
    blocked = role.all_blocked_columns()
    if not blocked:
        return
    used = {c.name.lower() for c in tree.find_all(exp.Column)}
    hits = sorted(used & {b.lower() for b in blocked})
    if hits:
        raise AccessDenied(
            f"role '{role.name}' cannot read columns {hits}",
            user_message=(
                f"Your role ({role.name}) can't access {', '.join(hits)}. "
                "These are customer/card-level identifiers."
            ),
        )


def _literal_values(node: exp.Expression) -> list[str]:
    if isinstance(node, exp.Literal) and node.is_string:
        return [node.this]
    return []


def check_scope_literals(tree: exp.Expression, role: Role) -> None:
    """Reject filters that explicitly target data outside the role's scope."""
    if not role.scope:
        return
    for col_name, allowed in role.scope.items():
        allowed_lower = {a.lower() for a in allowed}
        requested: list[str] = []
        for node in tree.find_all(exp.EQ, exp.In):
            col = node.this if isinstance(node.this, exp.Column) else None
            if isinstance(node, exp.EQ) and col is None and isinstance(node.expression, exp.Column):
                col = node.expression
                values = _literal_values(node.this)
            elif isinstance(node, exp.EQ):
                values = _literal_values(node.expression)
            else:
                values = [v for e in node.expressions for v in _literal_values(e)]
            if col is not None and col.name.lower() == col_name.lower():
                requested += values
        outside = sorted({v for v in requested if v.lower() not in allowed_lower})
        if outside:
            raise AccessDenied(
                f"role '{role.name}' scope {col_name}={list(allowed)}; query asked for {outside}",
                user_message=(
                    f"You can only view {col_name} = {', '.join(allowed)} data, "
                    f"but the question asks about {', '.join(outside)}."
                ),
            )
