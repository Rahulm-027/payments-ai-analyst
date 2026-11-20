"""Turns analysis results into an Answer.

Facts, numbers, charts and recommendations are built deterministically from the analysis
objects - every fact carries the query id, dataset, window and filters behind it. An LLM
(live mode) may only rewrite the executive summary, and its text is checked: any number in
it that does not appear in the computed facts makes us discard it and keep the template.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from analyst.core.errors import LLMError
from analyst.core.models import (
    ChartSpec,
    Fact,
    Filter,
    Hypothesis,
    Plan,
    QualityWarning,
    TableSpec,
)
from analyst.core.semantic import Metric
from analyst.llm.base import LLMProvider, LLMUsage
from analyst.stages import analyzer as A

DATASET = "transactions (de-duplicated)"


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def fmt_value(metric: Metric, v: float | None) -> str:
    if v is None or pd.isna(v):
        return "n/a"
    if metric.format == "percent":
        return f"{v:.2%}"
    if metric.format == "currency":
        return f"₹{v:,.0f}"
    return f"{v:,.0f}"


def fmt_delta(metric: Metric, d: float, base: float | None = None) -> str:
    if metric.format == "percent":
        return f"{d * 100:+.2f} pp"
    rel = f" ({d / base:+.1%})" if base else ""
    return (f"{'+' if d >= 0 else '−'}₹{abs(d):,.0f}{rel}" if metric.format == "currency"
            else f"{d:+,.0f}{rel}")


def fmt_filters(filters: list[Filter]) -> str:
    if not filters:
        return "none"
    return "; ".join(f"{f.dimension} {'not in' if f.negate else 'in'} {', '.join(f.values)}"
                     for f in filters)


def cell_filters(driver: A.Driver) -> list[Filter]:
    dims = driver.dimension.split(" x ")
    vals = driver.segment.split(" | ")
    return [Filter(dimension=d, values=[v]) for d, v in zip(dims, vals, strict=False)
            if v != "(none)"]


def cell_label(driver: A.Driver) -> str:
    return driver.segment.replace(" | ", " / ")


# --------------------------------------------------------------------------- #
# Containers
# --------------------------------------------------------------------------- #
@dataclass
class Narrative:
    summary: str = ""
    facts: list[Fact] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    charts: list[ChartSpec] = field(default_factory=list)
    tables: list[TableSpec] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ErrorEvidence:
    driver: A.Driver
    query_id: str
    top_code: str | None
    rate0: float
    rate1: float
    share_of_increase: float


def analyse_error_mix(driver: A.Driver, df: pd.DataFrame, query_id: str) -> ErrorEvidence:
    df = df.copy()
    for c in ("failures", "attempts"):
        df[c] = pd.to_numeric(df[c]).astype(float)
    attempts = df.groupby("period")["attempts"].sum()
    fails = df[df["error_code"] != "(none)"].pivot_table(
        index="error_code", columns="period", values="failures", aggfunc="sum", fill_value=0.0)
    for p in ("baseline", "current"):
        if p not in fails:
            fails[p] = 0.0
    r0 = fails["baseline"] / max(attempts.get("baseline", 0), 1)
    r1 = fails["current"] / max(attempts.get("current", 0), 1)
    inc = (r1 - r0)
    total_inc = inc[inc > 0].sum()
    if total_inc <= 0:
        return ErrorEvidence(driver, query_id, None, 0, 0, 0)
    top = inc.idxmax()
    return ErrorEvidence(driver, query_id, str(top), float(r0[top]), float(r1[top]),
                         float(inc[top] / total_inc))


ERROR_HYPOTHESES = {
    "ISSUER_DOWN": ("an issuer/bank-side outage", "Raise an incident with the bank, add "
                    "automatic fallback routing away from failing issuers, and alert on "
                    "issuer-level success rate."),
    "TIMEOUT": ("latency or timeouts between gateway, PSP/switch and bank", "Check PSP/switch "
                "latency and timeout settings for this segment, and alert on its success rate."),
    "AUTH_FAILED": ("a problem in the authentication step (OTP/PIN/3DS)", "Review recent "
                    "changes to the authentication flow and OTP delivery for this segment."),
    "RISK_DECLINED": ("a stricter risk/fraud rule", "Check recent risk-rule changes and their "
                      "false-positive rate for this segment."),
    "INSUFFICIENT_FUNDS": ("a change in customer mix (customer-side declines)", "No system "
                           "action indicated; monitor, and consider retry/alternate-method nudges."),
}


# --------------------------------------------------------------------------- #
# Narrator
# --------------------------------------------------------------------------- #
class Narrator:
    def __init__(self, llm: LLMProvider | None = None):
        self.llm = llm

    # ................................................................ explain
    def explain_change(self, plan: Plan, metric: Metric, ch: A.ChangeAnalysis,
                       totals_qid: str, dim_qids: dict[str, str],
                       evidence: ErrorEvidence | None, trend_df: pd.DataFrame | None,
                       trend_qid: str | None) -> Narrative:
        n = Narrative()
        assert plan.current and plan.baseline
        cur, base = plan.current.describe(), plan.baseline.describe()
        window = f"{base} vs {cur}"
        filt = fmt_filters(plan.filters)
        t = ch.totals
        direction = "fell" if t.delta < 0 else "rose"
        unit = "" if metric.is_ratio else " (daily average)"
        if plan.filters:
            unit += " for " + " / ".join(
                ("not " if f.negate else "") + ", ".join(f.values) for f in plan.filters)

        n.facts.append(Fact(
            statement=(f"{metric.label}{unit} {direction} from {fmt_value(metric, t.value0)} to "
                       f"{fmt_value(metric, t.value1)} ({fmt_delta(metric, t.delta, t.value0)})."),
            query_id=totals_qid, dataset=DATASET, time_window=window, filters=filt,
        ))

        mix = next((d for d in ch.drivers if d.kind == "mix"), None)
        rates = [d for d in ch.drivers if d.kind == "rate"]
        contribs = [d for d in ch.drivers if d.kind == "contribution"]

        # --- summary
        parts = [f"{metric.label}{unit} {direction} from {fmt_value(metric, t.value0)} to "
                 f"{fmt_value(metric, t.value1)} ({fmt_delta(metric, t.delta, t.value0)}) "
                 f"between {base} and {cur}."]
        if ch.classification == "no_material_change":
            parts[0] = (f"{metric.label}{unit} was broadly stable: {fmt_value(metric, t.value0)} → "
                        f"{fmt_value(metric, t.value1)} ({fmt_delta(metric, t.delta, t.value0)}) "
                        f"between {base} and {cur}.")
            if rates:
                r = rates[0]
                parts.append(f"However, there was a localised problem in {cell_label(r)}: "
                             f"{r.detail.split(' (')[0]}.")
        else:
            if mix:
                seg_dec = ch.by_dimension[mix.dimension]
                stable = [s for s in seg_dec.segments if s.value0 is not None
                          and s.value1 is not None and abs(s.value1 - s.value0) < 0.01]
                parts.append(
                    f"{abs(mix.share_of_delta):.0%} of the change ({mix.effect * 100:+.2f} pp) is a "
                    f"mix effect: {mix.detail}."
                    + (" Most segments' own success rates barely moved, so the overall rate fell "
                       "because volume shifted toward lower-success segments."
                       if len(stable) >= len(seg_dec.segments) / 2 and t.delta < 0 else "")
                )
            for i, r in enumerate(rates):
                lead = "On top of that" if (mix and i == 0) else ("The main driver is"
                                                                  if i == 0 else "Also")
                sep = "," if lead != "The main driver is" else ""
                offset = (" - more than the whole change, because other segments partly offset it"
                          if abs(r.share_of_delta) > 1.05 else "")
                parts.append(f"{lead}{sep} {cell_label(r)} {r.detail.split(' (')[0]}, "
                             f"accounting for {r.effect * 100:+.2f} pp "
                             f"({abs(r.share_of_delta):.0%} of the change{offset}).")
            for c in contribs:
                parts.append(f"{c.dimension.replace('_', ' ').title()} '{c.segment}' contributed "
                             f"{fmt_delta(metric, c.effect)} ({abs(c.share_of_delta):.0%} of the "
                             "change).")
        if evidence and evidence.top_code and evidence.share_of_increase >= 0.6:
            parts.append(f"Failures in {cell_label(evidence.driver)} are dominated by "
                         f"{evidence.top_code} ({evidence.rate0:.1%} → {evidence.rate1:.1%} of "
                         "attempts).")
        n.summary = " ".join(parts)

        # --- facts per driver
        if mix:
            dec = ch.by_dimension[mix.dimension]
            n.facts.append(Fact(
                statement=(f"Mix shift across {mix.dimension}: {mix.detail}; mix effect "
                           f"{mix.effect * 100:+.2f} pp ({abs(mix.share_of_delta):.0%} of the change)."),
                query_id=dim_qids[mix.dimension], dataset=DATASET, time_window=window,
                filters=filt,
            ))
            rates_txt = ", ".join(
                f"{s.segment} {s.value0:.2%}→{s.value1:.2%}" for s in dec.segments
                if s.value0 is not None and s.value1 is not None)
            n.facts.append(Fact(statement=f"Success rate by {mix.dimension}: {rates_txt}.",
                                query_id=dim_qids[mix.dimension], dataset=DATASET,
                                time_window=window, filters=filt))
        for r in rates:
            dims = r.dimension.split(" x ")
            qid = dim_qids.get(r.dimension) or dim_qids.get(dims[-1]) or totals_qid
            n.facts.append(Fact(
                statement=(f"{cell_label(r)}: {r.detail}; rate effect {r.effect * 100:+.2f} pp "
                           f"({abs(r.share_of_delta):.0%} of the change)."),
                query_id=qid, dataset=DATASET, time_window=window, filters=filt,
            ))
        if evidence and evidence.top_code:
            n.facts.append(Fact(
                statement=(f"In {cell_label(evidence.driver)}, {evidence.top_code} failures went "
                           f"from {evidence.rate0:.1%} to {evidence.rate1:.1%} of attempts "
                           f"({evidence.share_of_increase:.0%} of the increase in failures)."),
                query_id=evidence.query_id, dataset=DATASET, time_window=window,
                filters=fmt_filters([*plan.filters, *cell_filters(evidence.driver)]),
            ))

        # --- hypotheses & recommendations
        if mix:
            movers = mix.segment.split(", ")
            n.hypotheses.append(Hypothesis(
                statement=(f"The shift toward {movers[0]} is most likely commercial or product-"
                           "driven (e.g. an offer/cashback, a checkout default change, or large "
                           "merchants changing their payment mix), not a technical failure."),
                evidence=(f"Shares moved sharply ({mix.detail}) while most per-segment success "
                          "rates were stable."),
                confidence="medium",
            ))
            dec = ch.by_dimension[mix.dimension]
            top = next((s for s in dec.segments if s.segment == movers[0]), None)
            n.recommendations.append(
                f"Confirm with business/marketing what moved traffic to {movers[0]} "
                f"(campaign, checkout default, merchant change).")
            n.recommendations.append(
                f"Report success rate per {mix.dimension.replace('_', ' ')} (mix-adjusted) so a "
                "mix shift is not read as a performance drop.")
            if top and top.value1 is not None and metric.is_ratio:
                n.recommendations.append(
                    f"If the shift is lasting, invest in {movers[0]} success rate: at the new "
                    f"mix every 1 pp gained on {movers[0]} adds about {top.w1:.2f} pp overall.")
        for r in rates:
            ev = evidence if (evidence and evidence.driver is r) else None
            if ev and ev.top_code and ev.share_of_increase >= 0.6:
                cause, action = ERROR_HYPOTHESES.get(ev.top_code, (f"{ev.top_code} errors",
                                                                   "Investigate with logs."))
                n.hypotheses.append(Hypothesis(
                    statement=f"The drop in {cell_label(r)} points to {cause}.",
                    evidence=(f"{ev.top_code} rose from {ev.rate0:.1%} to {ev.rate1:.1%} of "
                              f"attempts and explains {ev.share_of_increase:.0%} of the extra "
                              f"failures; rate change is significant ({r.detail.split('(')[-1]}"),
                    confidence="high" if (r.value0 or 0) - (r.value1 or 0) > 0.05 else "medium",
                ))
                n.recommendations.append(f"{cell_label(r)}: {action}")
            else:
                n.hypotheses.append(Hypothesis(
                    statement=(f"{cell_label(r)} has a real, broad-based performance drop - "
                               "failures rose across reasons rather than one error type, which "
                               "fits capacity/latency or partner-side degradation."),
                    evidence=f"{r.detail}" + (
                        "; no single error code explains most of the increase." if ev else ""),
                    confidence="medium",
                ))
                n.recommendations.append(
                    f"Investigate {cell_label(r)} with gateway/PSP logs and set an alert on its "
                    "success rate.")
        if ch.classification == "no_material_change" and not rates:
            n.recommendations.append("No action needed on the headline metric; keep monitoring.")

        # --- caveats
        small = sorted({s.segment for d in ch.by_dimension.values() for s in d.segments
                        if min(s.den0, s.den1) < A.SMALL_SAMPLE_WARNING})
        if small:
            n.caveats.append(f"Small samples (<{A.SMALL_SAMPLE_WARNING} attempts) for: "
                             f"{', '.join(small[:8])}{'…' if len(small) > 8 else ''} - their "
                             "rates are noisy.")
        n.caveats.append("These are associations in historical data. Hypotheses about causes "
                         "need confirmation from logs or business context.")

        # --- charts & tables
        n.charts.append(self._bridge_chart(metric, t, mix, rates))
        primary = mix.dimension if mix else (rates[0].dimension.split(" x ")[0] if rates
                                             else next(iter(ch.by_dimension), None))
        if primary and primary in ch.by_dimension:
            dec = ch.by_dimension[primary]
            rows = []
            for s in dec.segments:
                for period, w, v in (("baseline", s.w0, s.value0), ("current", s.w1, s.value1)):
                    rows.append({"segment": s.segment, "period": period, "share": w,
                                 "value": v})
            n.charts.append(ChartSpec(
                kind="grouped_bar", title=f"{metric.label} by {primary}: baseline vs current",
                x="segment", y="value", color="period", data=rows,
                y_format="percent" if metric.is_ratio else "number"))
            if metric.is_ratio:
                n.charts.append(ChartSpec(
                    kind="grouped_bar", title=f"Share of attempts by {primary} (mix)",
                    x="segment", y="share", color="period", data=rows, y_format="percent"))
            n.tables.append(TableSpec(
                title=f"Decomposition by {primary}",
                columns=["segment", "attempts_baseline", "attempts_current", "rate_baseline",
                         "rate_current", "share_baseline", "share_current", "mix_pp", "rate_pp",
                         "significant"],
                rows=[{
                    "segment": s.segment, "attempts_baseline": int(s.den0),
                    "attempts_current": int(s.den1),
                    "rate_baseline": None if s.value0 is None else round(s.value0, 4),
                    "rate_current": None if s.value1 is None else round(s.value1, 4),
                    "share_baseline": round(s.w0, 4), "share_current": round(s.w1, 4),
                    "mix_pp": round(s.mix * 100, 3), "rate_pp": round(s.rate * 100, 3),
                    "significant": s.significant,
                } for s in dec.segments],
            ))
        if trend_df is not None and trend_qid:
            pts = A.compute_trend(trend_df, is_ratio=metric.is_ratio)
            n.charts.append(ChartSpec(
                kind="line", title=f"Daily {metric.label.lower()} across both periods",
                x="period_start", y="value",
                data=[{"period_start": p.period_start, "value": p.value} for p in pts],
                y_format="percent" if metric.is_ratio else "number"))
        return n

    def _bridge_chart(self, metric: Metric, t: A.Decomposition, mix: A.Driver | None,
                      rates: list[A.Driver]) -> ChartSpec:
        scale = 100 if metric.is_ratio else 1
        steps = [{"step": "Baseline", "value": t.value0 * scale, "kind": "total"}]
        explained = 0.0
        if mix:
            steps.append({"step": f"Mix shift ({mix.dimension})", "value": mix.effect * scale,
                          "kind": "delta"})
            explained += mix.effect
        for r in rates:
            steps.append({"step": cell_label(r), "value": r.effect * scale, "kind": "delta"})
            explained += r.effect
        rest = t.delta - explained
        if abs(rest) > 1e-9:
            steps.append({"step": "Other", "value": rest * scale, "kind": "delta"})
        steps.append({"step": "Current", "value": t.value1 * scale, "kind": "total"})
        return ChartSpec(kind="waterfall", title="What explains the change", x="step", y="value",
                         data=steps, y_format="pp" if metric.is_ratio else "number")

    # ................................................................ trend
    def trend(self, plan: Plan, metric: Metric, df: pd.DataFrame, qid: str) -> Narrative:
        n = Narrative()
        assert plan.current
        pts = A.compute_trend(df, is_ratio=metric.is_ratio)
        window = plan.current.describe()
        filt = fmt_filters(plan.filters)
        seg_dim = plan.dimensions[0] if plan.dimensions else None
        by_seg: dict[str, list[A.TrendPoint]] = {}
        for p in pts:
            by_seg.setdefault(p.segment, []).append(p)
        summaries = []
        for seg, sp in by_seg.items():
            vals = [p for p in sp if p.value is not None]
            if not vals:
                continue
            lo = min(vals, key=lambda p: p.value)
            hi = max(vals, key=lambda p: p.value)
            name = "" if seg == "all" else f"{seg}: "
            summaries.append(
                f"{name}from {fmt_value(metric, vals[0].value)} ({vals[0].period_start}) to "
                f"{fmt_value(metric, vals[-1].value)} ({vals[-1].period_start}); low "
                f"{fmt_value(metric, lo.value)} on {lo.period_start}, high "
                f"{fmt_value(metric, hi.value)} on {hi.period_start}")
        anomalies = [p for p in pts if p.anomaly]
        n.summary = (f"{plan.grain.title()}ly {metric.label.lower()} over {window}"
                     f"{' by ' + seg_dim if seg_dim else ''}: " + "; ".join(summaries[:6]) + ".")
        if anomalies:
            n.summary += " Unusual points: " + ", ".join(
                f"{p.period_start}{'' if p.segment == 'all' else ' ' + p.segment} "
                f"({fmt_value(metric, p.value)})" for p in anomalies[:5]) + "."
        for s in summaries[:6]:
            n.facts.append(Fact(statement=s[0].upper() + s[1:] + ".", query_id=qid,
                                dataset=DATASET, time_window=window, filters=filt))
        for p in anomalies[:5]:
            n.facts.append(Fact(
                statement=(f"{p.period_start}{'' if p.segment == 'all' else ' (' + p.segment + ')'}"
                           f" is an outlier: {fmt_value(metric, p.value)} vs the period median."),
                query_id=qid, dataset=DATASET, time_window=window, filters=filt))
            n.recommendations.append(
                f"Ask \"Why did {metric.label.lower()} change on {p.period_start}?\" to explain "
                "the outlier.")
        n.charts.append(ChartSpec(
            kind="line", title=f"{metric.label} - {plan.grain}ly", x="period_start", y="value",
            color="segment" if seg_dim else None,
            data=[{"period_start": p.period_start, "segment": p.segment, "value": p.value,
                   "anomaly": p.anomaly} for p in pts],
            y_format="percent" if metric.is_ratio else
            ("currency" if metric.format == "currency" else "number")))
        return n

    # ................................................................ breakdown
    def breakdown(self, plan: Plan, metric: Metric, df: pd.DataFrame, qid: str,
                  effective_days: float | None) -> Narrative:
        n = Narrative()
        assert plan.current
        window = plan.current.describe()
        filt = fmt_filters(plan.filters)
        dim = plan.dimensions[0] if plan.dimensions else None
        df = df.copy()
        for c in ("num", "den"):
            df[c] = pd.to_numeric(df[c]).astype(float)
        df["value"] = pd.to_numeric(df["value"]).astype(float)
        if not len(df):
            n.summary = f"No data matched for {window} (filters: {filt})."
            return n
        overall = (df["num"].sum() / df["den"].sum()) if metric.is_ratio else None
        if dim is None:
            v = df["value"].iloc[0]
            n.summary = f"{metric.label} for {window}: {fmt_value(metric, v)}."
            if not metric.is_ratio and effective_days and effective_days < plan.current.days:
                n.summary += (f" (Window has only {effective_days:.1f} days of data; daily "
                              f"average {fmt_value(metric, v / effective_days)}.)")
            n.facts.append(Fact(statement=n.summary, query_id=qid, dataset=DATASET,
                                time_window=window, filters=filt))
            return n
        top, bottom = df.iloc[0], df.iloc[-1]
        if dim == "error_code":
            lead = f"Failed attempts by error code (share of all attempts) for {window}"
            n.summary = (f"{lead}: largest {top['segment']} ({top['value']:.2%} of attempts), "
                         f"smallest {bottom['segment']} ({bottom['value']:.2%}); total failure "
                         f"rate {df['value'].sum():.2%}.")
            metric = metric.__class__(**{**metric.__dict__, "label": "Share of attempts"})
        else:
            lead = f"{metric.label} by {dim} for {window}"
            if len(df) == 1:
                n.summary = (f"{lead}: {top['segment']} {fmt_value(metric, top['value'])} - the "
                             "only segment within your data scope.")
            else:
                n.summary = (f"{lead}: highest {top['segment']} ({fmt_value(metric, top['value'])}), "
                             f"lowest {bottom['segment']} ({fmt_value(metric, bottom['value'])})"
                             + (f"; overall {fmt_value(metric, overall)}" if overall is not None
                                else "") + ".")
        small = df[df["den"] < A.SMALL_SAMPLE_WARNING]["segment"].tolist()
        if small and metric.is_ratio:
            n.caveats.append(f"Small samples (<{A.SMALL_SAMPLE_WARNING} attempts): "
                             f"{', '.join(map(str, small[:8]))} - rates are noisy.")
            if top["segment"] in small:
                n.summary += (f" Note: {top['segment']} has only {int(top['den'])} attempts, so "
                              "its rate is not reliable.")
        for _, r in df.head(10).iterrows():
            n.facts.append(Fact(
                statement=(f"{r['segment']}: {fmt_value(metric, r['value'])} "
                           f"({int(r['den']):,} attempts)."),
                query_id=qid, dataset=DATASET, time_window=window, filters=filt))
        n.charts.append(ChartSpec(
            kind="bar", title=lead, x="segment", y="value",
            data=df[["segment", "value", "den"]].head(25).to_dict(orient="records"),
            y_format="percent" if metric.is_ratio else
            ("currency" if metric.format == "currency" else "number")))
        n.tables.append(TableSpec(title=lead, columns=["segment", "value", "num", "den"],
                                  rows=df.head(50).to_dict(orient="records")))
        return n

    # ................................................................ LLM polish
    def polish_summary(self, question: str, narrative: Narrative,
                       warnings: list[QualityWarning], usage: LLMUsage | None) -> None:
        """Let the LLM rewrite the summary - accepted only if every number is grounded."""
        if self.llm is None:
            return
        grounded = " ".join([narrative.summary, *(f.statement for f in narrative.facts)])
        prompt = (f"QUESTION: {question}\n\nDRAFT SUMMARY: {narrative.summary}\n\nFACTS:\n"
                  + "\n".join(f"- {f.statement}" for f in narrative.facts)
                  + ("\n\nDATA WARNINGS:\n" + "\n".join(f"- {w.message}" for w in warnings)
                     if warnings else ""))
        system = ("Rewrite the draft into a clear 2-4 sentence executive answer for a payments "
                  "business leader. Use ONLY numbers that appear in the draft or facts, written "
                  "exactly the same way. Do not add causes that are not in the facts. Plain text, "
                  "no markdown.")
        try:
            text = self.llm.complete(system, prompt, purpose="summary", usage=usage).strip()
        except LLMError as e:
            narrative.notes.append(f"LLM summary skipped: {e}")
            return
        bad = ungrounded_numbers(text, grounded)
        if bad:
            narrative.notes.append(f"LLM summary rejected - ungrounded numbers {bad[:5]}; "
                                   "kept template summary")
            return
        narrative.summary = text
        narrative.notes.append("LLM summary accepted (all numbers grounded)")


NUMBER_RE = re.compile(r"(?<![\w.])[−+-]?\d[\d,]*(?:\.\d+)?")


def _norm(s: str) -> str:
    return s.replace(",", "").replace("−", "-").lstrip("+")


def ungrounded_numbers(text: str, source: str) -> list[str]:
    allowed = {_norm(m.group(0)) for m in NUMBER_RE.finditer(source)}
    allowed |= {a.lstrip("-") for a in allowed}
    bad = []
    for m in NUMBER_RE.finditer(text):
        raw = _norm(m.group(0))
        plain = raw.lstrip("-")
        if raw in allowed or plain in allowed:
            continue
        if plain.isdigit() and (int(plain) <= 12 or 2000 <= int(plain) <= 2100):
            continue  # small counts ("two drivers"), years
        bad.append(m.group(0))
    return bad
