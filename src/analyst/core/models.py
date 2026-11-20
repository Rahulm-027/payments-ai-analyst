"""Typed contracts passed between pipeline stages."""

from __future__ import annotations

from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field


class ValidatedQuery(BaseModel):
    """A query that passed the safety checker, rewritten for the user's access scope."""

    original_sql: str
    sql: str  # the rewritten SQL that will actually run
    tables: list[str]
    applied_controls: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class QueryResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    query_id: str
    sql: str
    data: pd.DataFrame
    row_count: int
    truncated: bool
    elapsed_ms: float
    cached: bool = False

    def preview(self, n: int = 20) -> list[dict[str, Any]]:
        return self.data.head(n).to_dict(orient="records")
