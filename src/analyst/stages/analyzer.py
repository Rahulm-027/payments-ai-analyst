"""Deterministic analysis: no LLM touches the numbers.

Core idea - mix/rate decomposition of a change in a ratio metric (e.g. success rate):

    overall rate R = sum_i w_i * r_i       (w_i = segment share of the denominator)

    delta R = sum_i (w1_i - w0_i) * r0_i   <- mix effect  (volume moved between segments)
            + sum_i w1_i * (r1_i - r0_i)   <- rate effect (segments themselves got better/worse)

The two parts add up exactly to the observed change, which is checked in the unit tests.
For additive metrics (attempts, GMV) the analysis is a per-segment contribution to the
change in the daily average.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

MIN_SEGMENT_DENOMINATOR = 100  # below this, segment rates are too noisy to call a driver
SMALL_SAMPLE_WARNING = 300  # segments below this get a caveat
SIGNIFICANCE_ALPHA = 0.05
# Crossed segments mean ~100 tests per question, so drivers need stronger evidence.
DRIVER_ALPHA = 0.01
SECONDARY_DRIVER_MIN_SHARE = 0.20
MIX_SHIFT_MIN_PP = 0.05  # 5 percentage points of share


@dataclass
class SegmentRow:
    segment: str
    den0: float
    den1: float
    value0: float | None  # rate (ratio metric) or daily value (additive)
    value1: float | None
    w0: float
    w1: float
    mix: float
    rate: float
    contribution: float
    p_value: float | None
    significant: bool
    small_sample: bool


@dataclass
class Decomposition:
    dimension: str | None
    is_ratio: bool
    value0: float
    value1: float
    delta: float
    mix_total: float
    rate_total: float
    segments: list[SegmentRow]

    @property
    def mix_share(self) -> float:
        return self.mix_total / self.delta if self.delta else 0.0

    def as_frame(self) -> pd.DataFrame:
        return pd.DataFrame([s.__dict__ for s in self.segments])


@dataclass
class Driver:
    kind: str  # "mix" | "rate"
    dimension: str
    segment: str
    effect: float  # contribution to delta, in metric units
    share_of_delta: float
    detail: str
    significant: bool = True
    volume: float = 0.0  # current-period denominator of the segment
    value0: float | None = None
    value1: float | None = None


@dataclass
class ChangeAnalysis:
    totals: Decomposition
    by_dimension: dict[str, Decomposition]
    stratified: list[Decomposition]
    drivers: list[Driver]
    primary_mix_dimension: str | None
    stratify_by: str | None
    classification: str  # "mix_driven" | "rate_driven" | "mixed" | "no_material_change"
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def two_proportion_p_value(x0: float, n0: float, x1: float, n1: float) -> float | None:
    if n0 <= 0 or n1 <= 0:
        return None
    p_pool = (x0 + x1) / (n0 + n1)
    var = p_pool * (1 - p_pool) * (1 / n0 + 1 / n1)
    if var <= 0:
        return None
    z = (x1 / n1 - x0 / n0) / math.sqrt(var)
    return math.erfc(abs(z) / math.sqrt(2))


# --------------------------------------------------------------------------- #
# Decomposition
# --------------------------------------------------------------------------- #
def _pivot(df: pd.DataFrame) -> pd.DataFrame:
    """segment, period, num, den -> one row per segment with num0/den0/num1/den1."""
    df = df.copy()
    df["num"] = pd.to_numeric(df["num"], errors="coerce").fillna(0.0).astype(float)
    df["den"] = pd.to_numeric(df["den"], errors="coerce").fillna(0.0).astype(float)
    wide = df.pivot_table(index="segment", columns="period", values=["num", "den"],
                          aggfunc="sum", fill_value=0.0)
    wide.columns = [f"{a}_{b}" for a, b in wide.columns]
    for col in ("num_baseline", "num_current", "den_baseline", "den_current"):
        if col not in wide:
            wide[col] = 0.0
    return wide.reset_index()


def decompose(df: pd.DataFrame, *, is_ratio: bool, dimension: str | None = None,
              days0: float = 1.0, days1: float = 1.0) -> Decomposition:
    w = _pivot(df)
    N0, N1 = w["den_baseline"].sum(), w["den_current"].sum()
    X0, X1 = w["num_baseline"].sum(), w["num_current"].sum()

    if is_ratio:
        R0 = X0 / N0 if N0 else 0.0
        R1 = X1 / N1 if N1 else 0.0
    else:
        R0, R1 = X0 / days0, X1 / days1
    delta = R1 - R0

    rows: list[SegmentRow] = []
    for _, r in w.iterrows():
        n0, n1, x0, x1 = r["den_baseline"], r["den_current"], r["num_baseline"], r["num_current"]
        w0 = n0 / N0 if N0 else 0.0
        w1 = n1 / N1 if N1 else 0.0
        if is_ratio:
            r0 = x0 / n0 if n0 else None
            r1 = x1 / n1 if n1 else None
            # New / vanished segments: attribute everything to mix (keeps the identity exact).
            r0_eff = r0 if r0 is not None else (r1 or 0.0)
            r1_eff = r1 if r1 is not None else r0_eff
            mix = (w1 - w0) * r0_eff
            rate = w1 * (r1_eff - r0_eff)
            contribution = mix + rate
            p = two_proportion_p_value(x0, n0, x1, n1)
            v0, v1 = r0, r1
        else:
            v0, v1 = x0 / days0, x1 / days1
            mix, rate = 0.0, 0.0
            contribution = v1 - v0
            p = None
        rows.append(SegmentRow(
            segment=str(r["segment"]), den0=n0, den1=n1, value0=v0, value1=v1, w0=w0, w1=w1,
            mix=mix, rate=rate, contribution=contribution, p_value=p,
            significant=bool(p is not None and p < SIGNIFICANCE_ALPHA),
            small_sample=min(n0, n1) < MIN_SEGMENT_DENOMINATOR,
        ))
    rows.sort(key=lambda s: abs(s.contribution), reverse=True)
    return Decomposition(
        dimension=dimension, is_ratio=is_ratio, value0=R0, value1=R1, delta=delta,
        mix_total=sum(s.mix for s in rows), rate_total=sum(s.rate for s in rows), segments=rows,
    )


# --------------------------------------------------------------------------- #
# Driver identification
# --------------------------------------------------------------------------- #
def _same_direction(x: float, delta: float) -> bool:
    return (x < 0) == (delta < 0) and x != 0


def find_mix_dimension(totals: Decomposition,
                       by_dim: dict[str, Decomposition]) -> tuple[str | None, Driver | None]:
    """Dimension whose mix shift explains the largest part of the change (if material)."""
    delta = totals.delta
    if not totals.is_ratio or delta == 0:
        return None, None
    candidates = sorted(
        ((dim, d) for dim, d in by_dim.items() if _same_direction(d.mix_total, delta)),
        key=lambda kv: abs(kv[1].mix_total), reverse=True,
    )
    if not candidates or abs(candidates[0][1].mix_total) < 0.25 * abs(delta):
        return None, None
    dim, d = candidates[0]
    movers = sorted(
        (s for s in d.segments if abs(s.w1 - s.w0) >= MIX_SHIFT_MIN_PP),
        key=lambda s: abs(s.w1 - s.w0), reverse=True,
    )
    if not movers:
        return None, None
    detail = ", ".join(f"{s.segment} share {s.w0:.1%} → {s.w1:.1%}" for s in movers)
    return dim, Driver("mix", dim, ", ".join(s.segment for s in movers), d.mix_total,
                       d.mix_share, detail)


def find_rate_drivers(delta: float, stratified: list[Decomposition],
                      max_drivers: int = 3) -> list[Driver]:
    """Significant, mix-adjusted rate changes in the direction of the overall change.

    `stratified` decompositions use crossed segments (e.g. payment_method x channel), so a
    segment's rate change is not just the mix shift seen through another dimension.
    """
    candidates: list[Driver] = []
    for d in stratified:
        for s in d.segments:
            if (s.p_value is not None and s.p_value < DRIVER_ALPHA and not s.small_sample
                    and _same_direction(s.rate, delta) and abs(s.rate) >= 0.10 * abs(delta)
                    and s.value0 is not None and s.value1 is not None):
                candidates.append(Driver(
                    "rate", str(d.dimension), s.segment, s.rate, s.rate / delta if delta else 0,
                    f"success rate {s.value0:.2%} → {s.value1:.2%} "
                    f"(n={int(s.den0):,} → {int(s.den1):,}, p={s.p_value:.2g})",
                    volume=s.den1, value0=s.value0, value1=s.value1,
                ))
    candidates.sort(key=lambda x: abs(x.effect), reverse=True)

    # Cells that share the same stratum value (e.g. NETBANKING, NETBANKING | ONLINE,
    # NETBANKING | Coastal Bank) usually describe one effect. Within such a group keep the
    # most concentrated cell (smallest volume) that still carries >=70% of the group's
    # largest effect - that is the most specific explanation.
    groups: dict[str, list[Driver]] = {}
    for c in candidates:
        groups.setdefault(c.segment.split(" | ")[0], []).append(c)
    chosen: list[Driver] = []
    for members in groups.values():
        strongest = max(abs(m.effect) for m in members)
        eligible = [m for m in members if abs(m.effect) >= 0.7 * strongest]
        chosen.append(min(eligible, key=lambda m: m.volume))

    # Unique, strongest first; secondary drivers must carry a meaningful share.
    out: list[Driver] = []
    for c in sorted(chosen, key=lambda x: abs(x.effect), reverse=True):
        if c in out:
            continue
        if out and abs(c.share_of_delta) < SECONDARY_DRIVER_MIN_SHARE:
            continue
        out.append(c)
        if len(out) >= max_drivers:
            break
    return out


def find_contribution_drivers(delta: float, by_dim: dict[str, Decomposition]) -> list[Driver]:
    """Additive metrics: segments contributing most to the change in the daily average."""
    out: list[Driver] = []
    for dim, d in by_dim.items():
        for s in d.segments[:3]:
            if _same_direction(s.contribution, delta) and abs(s.contribution) >= 0.15 * abs(delta):
                out.append(Driver("contribution", dim, s.segment, s.contribution,
                                  s.contribution / delta, "change in daily value"))
    out.sort(key=lambda x: abs(x.effect), reverse=True)
    return out[:3]


def classify(totals: Decomposition, mix_driver: Driver | None, material_threshold: float) -> str:
    if abs(totals.delta) < material_threshold:
        return "no_material_change"
    if not totals.is_ratio:
        return "rate_driven"
    share = mix_driver.share_of_delta if mix_driver else 0.0
    if share >= 0.6:
        return "mix_driven"
    if share <= 0.3:
        return "rate_driven"
    return "mixed"


# --------------------------------------------------------------------------- #
# Trend
# --------------------------------------------------------------------------- #
@dataclass
class TrendPoint:
    period_start: str
    segment: str
    value: float | None
    den: float
    anomaly: bool = False


def compute_trend(df: pd.DataFrame, *, is_ratio: bool) -> list[TrendPoint]:
    df = df.copy()
    df["num"] = pd.to_numeric(df["num"], errors="coerce").fillna(0.0).astype(float)
    df["den"] = pd.to_numeric(df["den"], errors="coerce").fillna(0.0).astype(float)
    df["value"] = df["num"] / df["den"].where(df["den"] > 0) if is_ratio else df["num"]
    points: list[TrendPoint] = []
    for seg, g in df.groupby("segment"):
        vals = g["value"].astype(float)
        med = vals.median()
        mad = (vals - med).abs().median() or 1e-9
        for _, r in g.iterrows():
            robust_z = 0.6745 * (r["value"] - med) / mad if pd.notna(r["value"]) else 0
            points.append(TrendPoint(
                period_start=str(r["period_start"]), segment=str(seg),
                value=None if pd.isna(r["value"]) else float(r["value"]), den=float(r["den"]),
                anomaly=bool(abs(robust_z) > 4 and r["den"] >= MIN_SEGMENT_DENOMINATOR),
            ))
    return points
