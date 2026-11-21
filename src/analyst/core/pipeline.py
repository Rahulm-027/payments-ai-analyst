"""End-to-end orchestration: question -> plan -> checks -> SQL -> analysis -> answer.

Every stage is timed and recorded in the answer's trace, every query is recorded as
evidence, and every failure maps to an explicit answer status instead of an exception.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

import pandas as pd

from analyst.core.errors import AccessDenied, AnalystError, QueryTooExpensive, UnsafeQuery
from analyst.core.models import (
    Answer,
    Fact,
    Plan,
    QueryEvidence,
    QueryResult,
    TableSpec,
    TimeWindow,
    TraceStep,
)
from analyst.core.semantic import User, get_user, load_semantic_layer
from analyst.llm.base import LLMProvider, LLMUsage
from analyst.stages import analyzer as A
from analyst.stages.catalog import Catalog, load_catalog
from analyst.stages.executor import QueryExecutor
from analyst.stages.narrator import (
    DATASET,
    Narrative,
    Narrator,
    analyse_error_mix,
    cell_filters,
    fmt_filters,
)
from analyst.stages.planner import Planner, explanatory_dimensions
from analyst.stages.quality import DataQualityChecker
from analyst.stages.query_builder import QueryBuilder
from analyst.stages.sql_generator import SqlGenerator
from analyst.stages.validator import SqlValidator
from config.settings import Settings
from config.settings import settings as default_settings

log = logging.getLogger("analyst")


@dataclass
class RunContext:
    request_id: str
    user: User
    trace: list[TraceStep] = field(default_factory=list)
    evidence: list[QueryEvidence] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)

    @contextmanager
    def stage(self, name: str):
        start = time.perf_counter()
        step = TraceStep(stage=name, status="ok", ms=0)
        try:
            yield step
        except Exception as e:
            step.status = "failed"
            step.detail = step.detail or (e.user_message if isinstance(e, AnalystError)
                                          else str(e))
            raise
        finally:
            step.ms = round((time.perf_counter() - start) * 1000, 1)
            self.trace.append(step)
            log.info("request=%s stage=%s status=%s ms=%.1f %s", self.request_id, name,
                     step.status, step.ms, step.detail)


def build_llm(cfg: Settings) -> LLMProvider | None:
    if cfg.llm_mode != "live":
        return None
    from analyst.llm.groq_provider import GroqProvider

    return GroqProvider(cfg.groq_api_key, cfg.groq_model)


class AnalystPipeline:
    def __init__(self, cfg: Settings | None = None, llm: LLMProvider | None = None,
                 use_llm_from_settings: bool = True):
        self.cfg = cfg or default_settings
        self.semantic = load_semantic_layer(self.cfg.metrics_path)
        self.executor = QueryExecutor(self.cfg.db_path, self.cfg.query_timeout_seconds,
                                      self.cfg.max_result_rows)
        self.validator = SqlValidator(self.semantic, self.cfg.max_result_rows)
        self.builder = QueryBuilder(self.semantic)
        self.quality = DataQualityChecker(self.validator, self.executor)
        self.catalog: Catalog = load_catalog(self.executor, self.semantic)
        self.llm = llm if llm is not None else (build_llm(self.cfg) if use_llm_from_settings
                                                else None)
        self.planner = Planner(self.semantic, self.catalog, self.llm)
        self.narrator = Narrator(self.llm)
        self.sql_generator = (SqlGenerator(self.llm, self.semantic, self.validator,
                                           self.executor) if self.llm else None)

    @property
    def mode(self) -> str:
        return f"live ({self.llm.name})" if self.llm else "offline"

    # ------------------------------------------------------------------ query helper
    def _query(self, ctx: RunContext, purpose: str, sql: str) -> QueryResult:
        vq = self.validator.validate(sql, ctx.user)
        result = self.executor.run(vq)
        self._record(ctx, purpose, result, vq.applied_controls, vq.original_sql)
        return result

    def _record(self, ctx, purpose, result: QueryResult, controls, original_sql) -> None:
        ctx.evidence.append(QueryEvidence(
            query_id=result.query_id, purpose=purpose, sql=result.sql, original_sql=original_sql,
            controls=controls, row_count=result.row_count, elapsed_ms=result.elapsed_ms,
            cached=result.cached))

    # ------------------------------------------------------------------ public API
    def ask(self, question: str, username: str) -> Answer:
        ctx = RunContext(request_id=uuid.uuid4().hex[:8], user=get_user(username))
        answer = Answer(question=question, user=username, status="answered")
        try:
            self._ask(question, ctx, answer)
        except AccessDenied as e:
            answer.status, answer.summary, answer.error = "denied", e.user_message, e.to_dict()
        except (UnsafeQuery, QueryTooExpensive) as e:
            answer.status, answer.summary, answer.error = "blocked", e.user_message, e.to_dict()
        except AnalystError as e:
            answer.status, answer.summary, answer.error = "error", e.user_message, e.to_dict()
        except Exception as e:  # last-resort guard: never leak a stack trace to the user
            log.exception("request=%s unexpected failure", ctx.request_id)
            answer.status = "error"
            answer.summary = "Something went wrong while analysing your question."
            answer.error = {"code": "internal_error", "message": answer.summary,
                            "detail": f"{type(e).__name__}: {e}"}
        answer.trace = ctx.trace
        answer.queries = ctx.evidence
        if ctx.usage.calls:
            answer.trace.append(TraceStep(
                stage="llm_usage", status="ok" if not ctx.usage.failures else "warning",
                ms=round(ctx.usage.latency_ms, 1),
                detail=(f"{ctx.usage.calls} calls, {ctx.usage.prompt_tokens} prompt + "
                        f"{ctx.usage.completion_tokens} completion tokens, "
                        f"{ctx.usage.failures} failures")))
        return answer

    def run_sql(self, sql: str, username: str) -> Answer:
        """Power-user path: raw SQL through the same safety checks."""
        ctx = RunContext(request_id=uuid.uuid4().hex[:8], user=get_user(username))
        answer = Answer(question=sql, user=username, status="answered")
        try:
            with ctx.stage("validate_and_execute") as st:
                result = self._query(ctx, "User-supplied SQL", sql)
                st.detail = f"{result.row_count} rows"
            answer.summary = (f"Query returned {result.row_count} rows"
                              f"{' (truncated)' if result.truncated else ''}.")
            answer.tables = [TableSpec(title="Result", columns=list(result.data.columns),
                                       rows=_records(result.data, 200))]
        except AccessDenied as e:
            answer.status, answer.summary, answer.error = "denied", e.user_message, e.to_dict()
        except (UnsafeQuery, QueryTooExpensive) as e:
            answer.status, answer.summary, answer.error = "blocked", e.user_message, e.to_dict()
        except AnalystError as e:
            answer.status, answer.summary, answer.error = "error", e.user_message, e.to_dict()
        answer.trace, answer.queries = ctx.trace, ctx.evidence
        return answer

    # ------------------------------------------------------------------ internals
    def _ask(self, question: str, ctx: RunContext, answer: Answer) -> None:
        with ctx.stage("access_precheck") as st:
            _deny_restricted_columns(question, ctx.user)
            st.detail = "no restricted fields requested"
        with ctx.stage("plan") as st:
            outcome = self.planner.plan(question, ctx.usage)
            plan = outcome.plan
            st.detail = "; ".join(outcome.notes)
            if any("rejected" in n or "fell back" in n for n in outcome.notes):
                st.status = "warning"
        answer.plan = plan

        if plan.clarification_needed:
            answer.status = "needs_clarification"
            answer.summary = plan.clarification_needed
            answer.clarification_options = plan.clarification_options
            return

        if plan.intent == "custom_sql":
            self._custom_sql(plan, question, ctx, answer)
            return

        with ctx.stage("data_quality") as st:
            report = self.quality.check(plan, ctx.user)
            for purpose, res, controls, original in report.queries:
                self._record(ctx, purpose, res, controls, original)
            st.detail = f"{len(report.warnings)} warnings"
            if report.warnings:
                st.status = "warning"
        answer.quality_warnings = report.warnings
        if report.blocking:
            answer.status = "error"
            answer.summary = report.warnings[0].message
            return

        metric = self.semantic.metric(plan.metric)
        if plan.intent == "explain_change":
            narrative = self._explain_change(plan, metric, ctx, report.effective_days)
        elif plan.intent == "trend":
            with ctx.stage("analyse_trend"):
                res = self._query(ctx, "Trend", self.builder.trend(
                    plan, plan.current, plan.dimensions[0] if plan.dimensions else None))
            narrative = self.narrator.trend(plan, metric, res.data, res.query_id)
        else:
            with ctx.stage("analyse_breakdown"):
                res = self._query(ctx, "Breakdown", self.builder.breakdown(
                    plan, plan.current, plan.dimensions[0] if plan.dimensions else None))
            narrative = self.narrator.breakdown(plan, metric, res.data, res.query_id,
                                                report.effective_days.get("current"))

        if self.llm:
            with ctx.stage("narrate_llm") as st:
                self.narrator.polish_summary(question, narrative, report.warnings, ctx.usage)
                st.detail = "; ".join(narrative.notes)
                if any("rejected" in n or "skipped" in n for n in narrative.notes):
                    st.status = "warning"

        caveats = list(plan.assumptions) + narrative.caveats
        if self.catalog.as_of and plan.current and plan.current.end > self.catalog.as_of.date():
            pass  # freshness already reported as a quality warning
        self._fill(answer, narrative, caveats)

    def _fill(self, answer: Answer, n: Narrative, caveats: list[str]) -> None:
        answer.summary = n.summary
        answer.facts = n.facts
        answer.hypotheses = n.hypotheses
        answer.recommendations = n.recommendations
        answer.caveats = caveats
        answer.charts = n.charts
        answer.tables = n.tables

    def _explain_change(self, plan: Plan, metric, ctx: RunContext,
                        effective_days: dict[str, float]) -> Narrative:
        assert plan.current and plan.baseline
        days0 = effective_days.get("baseline", plan.baseline.days)
        days1 = effective_days.get("current", plan.current.days)
        dims = [d for d in explanatory_dimensions(plan.dimensions,
                                                  {f.dimension for f in plan.filters})
                if d in self.semantic.dimensions]
        dim_qids: dict[str, str] = {}

        with ctx.stage("compare_periods") as st:
            res = self._query(ctx, "Overall metric, both periods",
                              self.builder.segment_comparison(plan, None))
            totals = A.decompose(res.data, is_ratio=metric.is_ratio, days0=days0, days1=days1)
            totals_qid = res.query_id
            st.detail = f"delta={totals.delta:+.4f}"

        with ctx.stage("segment_by_dimension") as st:
            by_dim: dict[str, A.Decomposition] = {}
            for d in dims:
                r = self._query(ctx, f"Segment comparison by {d}",
                                self.builder.segment_comparison(plan, d))
                dec = A.decompose(r.data, is_ratio=metric.is_ratio, dimension=d,
                                  days0=days0, days1=days1)
                if len(dec.segments) < 2:
                    continue  # e.g. channel when filtered to NETBANKING: nothing to compare
                by_dim[d] = dec
                dim_qids[d] = r.query_id
            dims = list(by_dim)
            st.detail = f"{len(dims)} informative dimensions"

        material = 0.003 if metric.is_ratio else 0.03 * abs(totals.value0 or 1)
        stratified: list[A.Decomposition] = []
        mix_dim, mix_driver, strat_dim = None, None, None
        with ctx.stage("decompose_and_drill_down") as st:
            if metric.is_ratio and by_dim:
                mix_dim, mix_driver = A.find_mix_dimension(totals, by_dim)
                strat_dim = mix_dim or ("payment_method" if "payment_method" in by_dim
                                        else _most_explanatory(by_dim))
                stratified.append(by_dim[strat_dim])
                for other in dims:
                    if other == strat_dim:
                        continue
                    key = f"{strat_dim} x {other}"
                    r = self._query(ctx, f"Mix-adjusted cells: {key}",
                                    self.builder.segment_comparison(plan, [strat_dim, other]))
                    stratified.append(A.decompose(r.data, is_ratio=True, dimension=key))
                    dim_qids[key] = r.query_id
                drivers = ([mix_driver] if mix_driver else []) + A.find_rate_drivers(
                    totals.delta, stratified)
            else:
                drivers = A.find_contribution_drivers(totals.delta, by_dim)
            classification = A.classify(totals, mix_driver, material)
            st.detail = (f"{classification}; drivers: "
                         + ", ".join(f"{d.kind}:{d.segment}" for d in drivers))

        analysis = A.ChangeAnalysis(totals=totals, by_dimension=by_dim, stratified=stratified,
                                    drivers=drivers, primary_mix_dimension=mix_dim,
                                    stratify_by=strat_dim, classification=classification)

        evidence = None
        top_rate = next((d for d in drivers if d.kind == "rate"), None)
        if top_rate is not None:
            with ctx.stage("failure_reasons") as st:
                r = self._query(ctx, f"Failure reasons in {top_rate.segment}",
                                self.builder.error_mix(plan, cell_filters(top_rate)))
                evidence = analyse_error_mix(top_rate, r.data, r.query_id)
                st.detail = (f"top code {evidence.top_code} "
                             f"({evidence.share_of_increase:.0%} of increase)")

        with ctx.stage("daily_series"):
            span = TimeWindow(start=min(plan.baseline.start, plan.current.start),
                              end=max(plan.baseline.end, plan.current.end))
            daily_plan = plan.model_copy(update={"grain": "day"})
            tr = self._query(ctx, "Daily series across both periods",
                             self.builder.trend(daily_plan, span))

        with ctx.stage("narrate"):
            return self.narrator.explain_change(plan, metric, analysis, totals_qid, dim_qids,
                                                evidence, tr.data, tr.query_id)

    def _custom_sql(self, plan: Plan, question: str, ctx: RunContext, answer: Answer) -> None:
        if self.sql_generator is None:
            answer.status = "error"
            answer.summary = ("This question needs free-form SQL, which is only available in live "
                              "LLM mode. Try rephrasing it as a trend, breakdown or 'why did X "
                              "change' question, or use the SQL tab.")
            return
        with ctx.stage("generate_sql") as st:
            out = self.sql_generator.generate(plan.custom_request or question, ctx.user,
                                              f"{self.catalog.as_of:%Y-%m-%d %H:%M}", ctx.usage)
            st.detail = f"{len(out.attempts)} attempt(s)"
            if len(out.attempts) > 1:
                st.status = "warning"
        self._record(ctx, "LLM-generated SQL", out.result, out.validated.applied_controls,
                     out.validated.original_sql)
        answer.summary = (f"{out.explanation} The query returned {out.result.row_count} rows"
                          f"{' (truncated)' if out.result.truncated else ''}.")
        answer.facts = [Fact(statement=out.explanation or "Custom query result",
                             query_id=out.result.query_id, dataset=", ".join(out.validated.tables),
                             time_window="as specified in the query", filters=fmt_filters([]))]
        answer.tables = [TableSpec(title="Result", columns=list(out.result.data.columns),
                                   rows=_records(out.result.data, 200))]
        answer.caveats = list(plan.assumptions) + [
            "Free-form SQL was written by the LLM and passed the safety checks; review the "
            "query in the evidence panel before relying on it."]


def _deny_restricted_columns(question: str, user: User) -> None:
    """Deterministic guard: a question naming a column hidden from this role is denied
    up front with a clear reason, whatever the LLM would have planned."""
    q = re.sub(r"[^a-z0-9]+", " ", question.lower())
    for col in sorted(user.role.all_blocked_columns()):
        if re.search(rf"\b{re.escape(col.replace('_', ' '))}s?\b", q):
            raise AccessDenied(
                f"question references restricted column '{col}'",
                user_message=(f"Your role ({user.role.name}) can't access {col}. "
                              "It is a customer/card-level identifier."),
            )


def _most_explanatory(by_dim: dict[str, A.Decomposition]) -> str:
    """Dimension containing the single largest segment-level rate effect."""
    return max(by_dim, key=lambda d: max((abs(s.rate) for s in by_dim[d].segments), default=0))


def _records(df: pd.DataFrame, n: int) -> list[dict]:
    out = df.head(n).copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].astype(str)
    return out.astype(object).where(out.notna(), None).to_dict(orient="records")


__all__ = ["AnalystPipeline", "DATASET"]
