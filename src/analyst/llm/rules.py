"""Deterministic, rule-based planner.

Used in offline mode (no API key needed) and as the fallback whenever the LLM planner
fails or returns an invalid plan. It handles the common question shapes: explaining a
change, trends, breakdowns and single-number lookups, with explicit time windows.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from analyst.core.models import Filter, Plan, TimeWindow
from analyst.stages.catalog import Catalog

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9

METRIC_PATTERNS: list[tuple[str, str]] = [
    (r"(including|incl\.?|with) cancel", "success_rate_incl_cancelled"),
    (r"\b(raw|all[- ]attempts?) success", "success_rate_incl_cancelled"),
    (r"\b(failure|decline|error) rate\b", "failure_rate"),
    (r"\bfailures?\b", "failure_rate"),
    (r"\b(success|approval|acceptance) rate\b|\bsr\b|\bconversion\b", "success_rate"),
    (r"\bgmv\b|\btpv\b|\bprocessed value\b|\brevenue\b|\bvalue processed\b", "gmv"),
    (r"\b(ticket size|atv|average transaction value|average ticket)\b", "avg_ticket_size"),
    (r"\bsuccessful (transactions|payments|txns)\b", "successful_txns"),
    (r"\b(volume|attempts|transaction count|txn count|number of (transactions|payments)|"
     r"transactions|payments)\b", "attempts"),
]

DIMENSION_PATTERNS: list[tuple[str, str]] = [
    (r"payment (method|mode|type)s?|\bmethods?\b|\bmodes?\b|\binstruments?\b", "payment_method"),
    (r"\bchannels?\b|pos vs online|online vs pos", "channel"),
    (r"\bbank types?\b", "bank_type"),
    (r"\bbanks?\b|\bissuers?\b", "bank"),
    (r"card networks?|\bnetworks?\b|\bschemes?\b", "card_network"),
    (r"error codes?|failure reasons?|decline reasons?|\berrors?\b|\breasons?\b", "error_code"),
    (r"\bcategor(y|ies)\b|\bverticals?\b|\bmcc\b|\bsegments?\b", "merchant_category"),
    (r"\btiers?\b|merchant size", "merchant_tier"),
    (r"\bcit(y|ies)\b", "city"),
    (r"\bregions?\b|\bzones?\b", "region"),
    (r"\bmerchants?\b|\bstores?\b", "merchant"),
]

VALUE_ALIASES = {
    "payment_method": {"UPI": ["upi"], "CARD": ["cards?", "credit cards?", "debit cards?"],
                       "NETBANKING": ["net ?banking", "nb"], "WALLET": ["wallets?"]},
    "channel": {"POS": ["pos", "in[- ]store", "offline", "terminals?"],
                "ONLINE": ["online", "e-?commerce", "web", "checkout"]},
}

EXPLAIN_WORDS = (r"\bwhy\b|\bdrop(ped)?\b|\bdecline[sd]?\b|\bfell\b|\bfall(en|ing)?\b|"
                 r"\bdecrease[sd]?\b|\bincrease[sd]?\b|\brose\b|\brise\b|\bimprove[sd]?\b|"
                 r"\bchange[sd]?\b|\bdip\b|\bspike\b|what happened|root cause|\bdrivers?\b|"
                 r"contribut|\bexplain\b|\bdeteriorat|\bworse\b|\bbetter\b")
TREND_WORDS = r"\btrend\b|over time|\bdaily\b|\bweekly\b|day by day|week by week|time series"
BREAKDOWN_WORDS = (r"\bby\b|\bper\b|\bwhich\b|\bbreak ?down\b|\bsplit\b|\btop\s+\d+|"
                   r"\bbest\b|\bworst\b|\bacross\b|\bcompare\b")
VAGUE_WORDS = r"how (are|is|did) (we|it|things|business)|what'?s going on|\bstatus\b|\boverview\b"

DEFAULT_ANALYSIS_DIMENSIONS = ["payment_method", "channel", "bank", "merchant_tier",
                               "merchant_category", "region"]


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


class RuleBasedPlanner:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.as_of_day = catalog.as_of.date()

    # ------------------------------------------------------------- windows
    def _latest_week(self) -> TimeWindow:
        start = monday(self.as_of_day)
        return TimeWindow(start=start, end=start + timedelta(days=7), label="latest week")

    def _preceding(self, w: TimeWindow, label: str = "previous period") -> TimeWindow:
        return TimeWindow(start=w.start - timedelta(days=w.days), end=w.start, label=label)

    def _parse_date(self, text: str) -> date | None:
        text = text.strip().lower().rstrip(",")
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)(?:\s+(\d{4}))?", text)
        if m and m[2] in MONTHS:
            return date(int(m[3] or self.as_of_day.year), MONTHS[m[2]], int(m[1]))
        m = re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", text)
        if m and m[1] in MONTHS:
            return date(int(m[3] or self.as_of_day.year), MONTHS[m[1]], int(m[2]))
        return None

    DATE_RE = (r"(\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+[a-z]{3,9}(?:\s+\d{4})?|"
               r"[a-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)")

    def _find_windows(self, q: str, assumptions: list[str]) -> list[TimeWindow]:
        """Return 0, 1 or 2 windows found in the question (current first)."""
        found: list[TimeWindow] = []

        # explicit ranges: between X and Y / from X to Y
        for m in re.finditer(rf"(?:between|from)\s+{self.DATE_RE}\s+(?:and|to|-|–)\s+{self.DATE_RE}",
                             q):
            a, b = self._parse_date(m[1]), self._parse_date(m[2])
            if a and b and b >= a:
                found.append(TimeWindow(start=a, end=b + timedelta(days=1)))
        # week of X
        for m in re.finditer(rf"week (?:of|starting|beginning)\s+{self.DATE_RE}", q):
            d = self._parse_date(m[1])
            if d:
                s = monday(d)
                found.append(TimeWindow(start=s, end=s + timedelta(days=7), label=f"week of {s:%d %b}"))
        if not found:
            # single dates ("on 21 Oct", "Oct 21")
            for m in re.finditer(self.DATE_RE, q):
                d = self._parse_date(m[0])
                if d and not any(w.start <= d < w.end for w in found):
                    found.append(TimeWindow(start=d, end=d + timedelta(days=1)))
        if not found:
            # month names ("in October", "last month")
            if re.search(r"\blast month\b|\bprevious month\b", q):
                first_this = self.as_of_day.replace(day=1)
                prev_last = first_this - timedelta(days=1)
                found.append(TimeWindow(start=prev_last.replace(day=1), end=first_this,
                                        label=f"{prev_last:%B}"))
            elif re.search(r"\bthis month\b", q):
                first = self.as_of_day.replace(day=1)
                nxt = (first + timedelta(days=32)).replace(day=1)
                found.append(TimeWindow(start=first, end=nxt, label=f"{first:%B} (to date)"))
            else:
                for name, num in MONTHS.items():
                    if len(name) > 3 and re.search(rf"\b{name}\b", q):
                        y = self.as_of_day.year
                        start = date(y, num, 1)
                        end = date(y + (num == 12), num % 12 + 1, 1)
                        found.append(TimeWindow(start=start, end=end, label=start.strftime("%B")))
        if not found:
            m = re.search(r"\b(?:last|past|previous)\s+(\d+)\s+(day|week)s?\b", q)
            if m:
                n = int(m[1]) * (7 if m[2] == "week" else 1)
                end = self.as_of_day  # as-of day is partial -> use complete days
                found.append(TimeWindow(start=end - timedelta(days=n), end=end,
                                        label=f"last {m[1]} {m[2]}s"))
                assumptions.append(f"'Last {m[1]} {m[2]}s' means the {n} complete days before "
                                   f"{self.as_of_day:%d %b} (data for {self.as_of_day:%d %b} is "
                                   "still loading).")
            elif re.search(r"\byesterday\b", q):
                d = self.as_of_day - timedelta(days=1)
                found.append(TimeWindow(start=d, end=d + timedelta(days=1), label="yesterday"))
            elif re.search(r"\btoday\b", q):
                found.append(TimeWindow(start=self.as_of_day,
                                        end=self.as_of_day + timedelta(days=1), label="today"))
            elif re.search(r"\b(last|this|past|latest|recent|current) week\b|\bweek over week\b|"
                           r"\bwow\b", q):
                found.append(self._latest_week())
                lw = self._latest_week().model_copy(update={"label": ""})
                assumptions.append(
                    f"'Last week' is read as the latest week in the data ({lw.describe()}), "
                    "compared with the week before."
                )
        return found[:2]

    # ------------------------------------------------------------- pieces
    def _metric(self, q: str) -> str | None:
        for pattern, metric in METRIC_PATTERNS:
            if re.search(pattern, q):
                return metric
        return None

    def _dimensions(self, q: str) -> list[str]:
        dims: list[str] = []
        for pattern, dim in DIMENSION_PATTERNS:
            if re.search(pattern, q) and dim not in dims:
                dims.append(dim)
        return dims

    def _filters(self, q: str) -> list[Filter]:
        filters: list[Filter] = []
        for dim, aliases in VALUE_ALIASES.items():
            hits = [val for val, pats in aliases.items()
                    if any(re.search(rf"\b{p}\b", q) for p in pats)]
            if hits:
                filters.append(Filter(dimension=dim, values=hits))
        for dim in ("bank", "region", "city", "merchant_category", "merchant_tier"):
            hits = []
            for val in self.catalog.values.get(dim, ()):
                key = val.lower().replace("_", " ")
                short = key.replace(" bank", "")
                if re.search(rf"\b{re.escape(key)}\b", q) or (
                    dim == "bank" and re.search(rf"\b{re.escape(short)}\b", q)
                ):
                    hits.append(val)
            if hits:
                filters.append(Filter(dimension=dim, values=hits))
        return filters

    # ------------------------------------------------------------- main
    def plan(self, question: str) -> Plan:
        q = " ".join(question.lower().split())
        assumptions: list[str] = []
        metric = self._metric(q)
        windows = self._find_windows(q, assumptions)
        filters = self._filters(q)
        dims = self._dimensions(q)
        top = re.search(r"\b(?:top|bottom|best|worst)\s+(\d+)\b", q)
        top_n = int(top[1]) if top else None

        # A dimension used as a filter isn't also a breakdown dimension
        # ("UPI success rate by bank" -> filter UPI, break down by bank).
        filtered_dims = {f.dimension for f in filters}
        explicit_by = re.findall(r"\b(?:by|per|which|across|each)\s+([a-z ]+)", q)
        dims = [d for d in dims if d not in filtered_dims
                or any(re.search(p, " ".join(explicit_by)) for p, dd in DIMENSION_PATTERNS
                       if dd == d)]

        is_explain = bool(re.search(EXPLAIN_WORDS, q))
        is_trend = bool(re.search(TREND_WORDS, q))
        is_breakdown = bool(re.search(BREAKDOWN_WORDS, q)) and bool(dims)

        if metric is None and re.search(VAGUE_WORDS, q) and not dims:
            return Plan(
                intent="breakdown", planner="rules",
                clarification_needed=(
                    "Which metric should I look at? I can analyse payment success rate, "
                    "transaction volume, GMV, failure rate or average ticket size."),
                clarification_options=[
                    f"How did success rate change {self._phrase(windows)}?",
                    f"How did transaction volume change {self._phrase(windows)}?",
                    f"How did GMV change {self._phrase(windows)}?",
                ],
            )
        if metric is None:
            metric = "success_rate"
            assumptions.append("No metric named - assuming payment success rate.")
        if metric == "success_rate":
            assumptions.append("Success rate = successful / attempted payments, excluding "
                               "user-cancelled attempts (ask for 'including cancelled' to change).")

        if is_explain and not is_trend:
            current = windows[0] if windows else self._latest_week()
            if not windows:
                assumptions.append(
                    f"No period given - comparing the latest week ({current.describe()}) "
                    "with the week before.")
            if len(windows) > 1:
                baseline = windows[1]
            elif current.days < 7:
                # Same weekday(s) one week earlier - avoids weekday/weekend effects.
                baseline = TimeWindow(start=current.start - timedelta(days=7),
                                      end=current.end - timedelta(days=7),
                                      label="same days, previous week")
                assumptions.append(f"Compared against the same weekday(s) one week earlier "
                                   f"({baseline.describe()}) to avoid weekday effects.")
            else:
                baseline = self._preceding(current, "previous period")
                if windows and current.label != "latest week":
                    assumptions.append(
                        f"Compared against the preceding {current.days}-day period "
                        f"({baseline.describe()}).")
            if current.end <= baseline.start:
                current, baseline = baseline, current
            current = current.model_copy(update={"label": current.label or "current period"})
            return Plan(
                intent="explain_change", metric=metric, filters=filters,
                dimensions=dims or [d for d in DEFAULT_ANALYSIS_DIMENSIONS
                                    if d not in filtered_dims],
                current=current, baseline=baseline, assumptions=assumptions, planner="rules",
            )

        if is_trend:
            grain = "week" if re.search(r"\bweekly\b|week by week|\bweeks\b", q) else "day"
            if windows:
                window = windows[0]
            else:
                end = monday(self.as_of_day) + timedelta(days=7)
                window = TimeWindow(start=end - timedelta(days=28 if grain == "day" else 56),
                                    end=end, label="last 4 weeks" if grain == "day" else "last 8 weeks")
                assumptions.append(f"No period given - showing {window.describe()}.")
            return Plan(intent="trend", metric=metric, filters=filters, dimensions=dims[:1],
                        current=window, grain=grain, assumptions=assumptions, planner="rules")

        window = windows[0] if windows else self._latest_week()
        if not windows:
            assumptions.append(f"No period given - using the latest week ({window.describe()}).")
        return Plan(intent="breakdown", metric=metric, filters=filters,
                    dimensions=dims[:1] if is_breakdown or dims else [], current=window,
                    top_n=top_n, assumptions=assumptions, planner="rules")

    def _phrase(self, windows: list[TimeWindow]) -> str:
        return f"in {windows[0].describe()}" if windows else "last week"
