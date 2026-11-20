"""Typed contracts passed between pipeline stages."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------- #
# Query layer
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #

Intent = Literal["explain_change", "trend", "breakdown", "custom_sql"]


class TimeWindow(BaseModel):
    """Half-open date window [start, end)."""

    start: date
    end: date
    label: str = ""

    @model_validator(mode="after")
    def _check(self) -> TimeWindow:
        if self.end <= self.start:
            raise ValueError(f"window end {self.end} must be after start {self.start}")
        return self

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    def describe(self) -> str:
        last = self.end.fromordinal(self.end.toordinal() - 1)
        if self.days == 1:
            span = f"{self.start:%d %b %Y}"
        else:
            span = f"{self.start:%d %b} – {last:%d %b %Y}"
        return f"{self.label} ({span})" if self.label else span


class Filter(BaseModel):
    dimension: str
    values: list[str]
    negate: bool = False


class Plan(BaseModel):
    intent: Intent
    metric: str = "success_rate"
    dimensions: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    current: TimeWindow | None = None
    baseline: TimeWindow | None = None
    grain: Literal["day", "week"] = "day"
    top_n: int | None = None
    assumptions: list[str] = Field(default_factory=list)
    clarification_needed: str | None = None
    clarification_options: list[str] = Field(default_factory=list)
    custom_request: str | None = None  # for custom_sql: the user's request
    planner: str = "rules"  # which planner produced it ("llm" / "rules")


# --------------------------------------------------------------------------- #
# Answer
# --------------------------------------------------------------------------- #


class Fact(BaseModel):
    statement: str
    query_id: str
    dataset: str
    time_window: str
    filters: str = "none"


class Hypothesis(BaseModel):
    statement: str
    evidence: str
    confidence: Literal["high", "medium", "low"]


class QualityWarning(BaseModel):
    check: str
    severity: Literal["info", "warning", "critical"]
    message: str
    query_id: str | None = None


class ChartSpec(BaseModel):
    kind: Literal["bar", "line", "waterfall", "grouped_bar"]
    title: str
    x: str
    y: str
    color: str | None = None
    data: list[dict[str, Any]]
    y_format: Literal["percent", "number", "currency", "pp"] = "number"


class TableSpec(BaseModel):
    title: str
    columns: list[str]
    rows: list[dict[str, Any]]


class QueryEvidence(BaseModel):
    query_id: str
    purpose: str
    sql: str
    original_sql: str
    controls: list[str]
    row_count: int
    elapsed_ms: float
    cached: bool


class TraceStep(BaseModel):
    stage: str
    status: Literal["ok", "warning", "failed", "skipped"]
    ms: float
    detail: str = ""


AnswerStatus = Literal["answered", "needs_clarification", "denied", "blocked", "error"]


class Answer(BaseModel):
    question: str
    user: str
    status: AnswerStatus
    summary: str = ""
    facts: list[Fact] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    quality_warnings: list[QualityWarning] = Field(default_factory=list)
    charts: list[ChartSpec] = Field(default_factory=list)
    tables: list[TableSpec] = Field(default_factory=list)
    queries: list[QueryEvidence] = Field(default_factory=list)
    plan: Plan | None = None
    clarification_options: list[str] = Field(default_factory=list)
    trace: list[TraceStep] = Field(default_factory=list)
    error: dict[str, Any] | None = None
