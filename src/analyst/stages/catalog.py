"""Dimension-value catalog and data "as of" time, loaded once at startup.

The catalog lets planners map words in a question ("Coastal", "UPI", "Pune") to
dimension filters and lets the LLM planner's output be checked against real values.
Dimension values are reference data, not sensitive, so this runs without user scoping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from analyst.core.semantic import SemanticLayer
from analyst.stages.executor import QueryExecutor

CATALOG_DIMENSIONS = {
    "payment_method": "SELECT DISTINCT payment_method FROM transactions",
    "channel": "SELECT DISTINCT channel FROM transactions",
    "bank": "SELECT DISTINCT bank_name FROM banks",
    "bank_type": "SELECT DISTINCT bank_type FROM banks",
    "card_network": "SELECT DISTINCT card_network FROM transactions WHERE card_network IS NOT NULL",
    "error_code": "SELECT DISTINCT error_code FROM transactions WHERE error_code IS NOT NULL",
    "merchant_category": "SELECT DISTINCT category FROM merchants",
    "merchant_tier": "SELECT DISTINCT tier FROM merchants",
    "city": "SELECT DISTINCT city FROM merchants",
    "region": "SELECT DISTINCT region FROM merchants",
}


@dataclass(frozen=True)
class Catalog:
    values: dict[str, tuple[str, ...]]
    as_of: datetime  # data loaded through this moment
    data_start: datetime

    def describe(self) -> str:
        return "\n".join(f"- {dim}: {', '.join(vals)}" for dim, vals in self.values.items())


def load_catalog(executor: QueryExecutor, semantic: SemanticLayer) -> Catalog:
    values = {}
    for dim, sql in CATALOG_DIMENSIONS.items():
        if dim in semantic.dimensions:
            df = executor.run(sql).data
            values[dim] = tuple(sorted(str(v) for v in df.iloc[:, 0].dropna()))
    as_of = executor.run(
        "SELECT loaded_through FROM data_freshness WHERE table_name = 'transactions'"
    ).data.iloc[0, 0]
    start = executor.run("SELECT MIN(created_at) FROM transactions").data.iloc[0, 0]
    return Catalog(values=values, as_of=as_of.to_pydatetime(), data_start=start.to_pydatetime())
