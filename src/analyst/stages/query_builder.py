"""Builds analysis SQL deterministically from a Plan and the semantic layer.

Standard analyses (explain a change, trend, breakdown) never use LLM-written SQL: the LLM
only chooses *which* metric, dimensions, filters and windows to use, and this module turns
that choice into SQL. The output still goes through the SqlValidator like any other query.
"""

from __future__ import annotations

from analyst.core.models import Filter, Plan, TimeWindow
from analyst.core.semantic import SemanticLayer


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _window_predicate(w: TimeWindow) -> str:
    start, end = _quote(str(w.start)), _quote(str(w.end))
    return f"(t.created_at >= DATE {start} AND t.created_at < DATE {end})"


class QueryBuilder:
    def __init__(self, semantic: SemanticLayer):
        self.semantic = semantic

    # ------------------------------------------------------------- helpers
    def _joins_for(self, dims: list[str], filters: list[Filter]) -> str:
        needed: list[str] = []
        for name in [*dims, *(f.dimension for f in filters)]:
            join = self.semantic.dimension(name).join
            if join and join not in needed:
                needed.append(join)
        return "\n".join(self.semantic.joins[j] for j in needed)

    def _filter_sql(self, filters: list[Filter]) -> list[str]:
        clauses = []
        for f in filters:
            col = self.semantic.dimension(f.dimension).sql
            values = ", ".join(_quote(v) for v in f.values)
            op = "NOT IN" if f.negate else "IN"
            clauses.append(f"{col} {op} ({values})")
        return clauses

    def _metric_parts(self, metric_name: str) -> tuple[str, str]:
        m = self.semantic.metric(metric_name)
        if m.is_ratio:
            return m.numerator, m.denominator  # type: ignore[return-value]
        return m.expression, "COUNT(*)"  # type: ignore[return-value]

    def _base(self) -> str:
        return f"WITH t AS {self.semantic.base_sql}"

    def _segment_expr(self, dims: list[str]) -> str:
        if not dims:
            return "'all'"
        parts = [
            f"COALESCE(CAST({self.semantic.dimension(d).sql} AS VARCHAR), '(none)')" for d in dims
        ]
        return parts[0] if len(parts) == 1 else f"CONCAT_WS(' | ', {', '.join(parts)})"

    @staticmethod
    def _as_list(dimension: str | list[str] | None) -> list[str]:
        if dimension is None:
            return []
        return [dimension] if isinstance(dimension, str) else list(dimension)

    # ------------------------------------------------------------- builders
    def segment_comparison(self, plan: Plan, dimension: str | list[str] | None,
                           extra_filters: list[Filter] | None = None) -> str:
        """num/den per segment per period (current vs baseline).

        dimension=None -> totals; a list of dimensions -> crossed segments ("UPI | ONLINE").
        """
        assert plan.current and plan.baseline
        num, den = self._metric_parts(plan.metric)
        filters = [*plan.filters, *(extra_filters or [])]
        dims = self._as_list(dimension)
        seg = self._segment_expr(dims)
        where = [f"({_window_predicate(plan.current)} OR {_window_predicate(plan.baseline)})"]
        where += self._filter_sql(filters)
        return f"""{self._base()}
SELECT
  {seg} AS segment,
  CASE WHEN {_window_predicate(plan.current)} THEN 'current' ELSE 'baseline' END AS period,
  {num} AS num,
  {den} AS den
FROM t
{self._joins_for(dims, filters)}
WHERE {' AND '.join(where)}
GROUP BY 1, 2
ORDER BY 1, 2"""

    def trend(self, plan: Plan, window: TimeWindow,
              dimension: str | list[str] | None = None) -> str:
        num, den = self._metric_parts(plan.metric)
        grain = self.semantic.time_grains[plan.grain]
        dims = self._as_list(dimension)
        seg = self._segment_expr(dims)
        where = [_window_predicate(window), *self._filter_sql(plan.filters)]
        return f"""{self._base()}
SELECT
  CAST({grain} AS DATE) AS period_start,
  {seg} AS segment,
  {num} AS num,
  {den} AS den
FROM t
{self._joins_for(dims, plan.filters)}
WHERE {' AND '.join(where)}
GROUP BY 1, 2
ORDER BY 1, 2"""

    def breakdown(self, plan: Plan, window: TimeWindow,
                  dimension: str | list[str] | None) -> str:
        num, den = self._metric_parts(plan.metric)
        dims = self._as_list(dimension)
        seg = self._segment_expr(dims)
        where = [_window_predicate(window), *self._filter_sql(plan.filters)]
        metric_sql = self.semantic.metric(plan.metric).sql()
        limit = f"\nLIMIT {int(plan.top_n)}" if plan.top_n else ""
        return f"""{self._base()}
SELECT
  {seg} AS segment,
  {num} AS num,
  {den} AS den,
  {metric_sql} AS value
FROM t
{self._joins_for(dims, plan.filters)}
WHERE {' AND '.join(where)}
GROUP BY 1
ORDER BY value DESC NULLS LAST{limit}"""

    def error_mix(self, plan: Plan, extra_filters: list[Filter]) -> str:
        """Failure counts by error code in both periods for one segment (cell)."""
        assert plan.current and plan.baseline
        filters = [*plan.filters, *extra_filters]
        where = [f"({_window_predicate(plan.current)} OR {_window_predicate(plan.baseline)})"]
        where += self._filter_sql(filters)
        return f"""{self._base()}
SELECT
  COALESCE(t.error_code, '(none)') AS error_code,
  CASE WHEN {_window_predicate(plan.current)} THEN 'current' ELSE 'baseline' END AS period,
  COUNT(*) FILTER (WHERE t.status = 'FAILED') AS failures,
  COUNT(*) FILTER (WHERE t.status <> 'USER_CANCELLED') AS attempts
FROM t
{self._joins_for([], filters)}
WHERE {' AND '.join(where)}
GROUP BY 1, 2
ORDER BY 1, 2"""
