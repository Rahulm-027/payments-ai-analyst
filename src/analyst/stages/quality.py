"""Data-quality checks that run before every analysis.

They don't block the answer (except when there is no data at all); they become explicit
warnings and caveats so the reader knows how far to trust the numbers. All checks go
through the same validator, so they respect the user's access scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from analyst.core.errors import AccessDenied
from analyst.core.models import Plan, QualityWarning, QueryResult, TimeWindow
from analyst.core.semantic import User
from analyst.stages.executor import QueryExecutor
from analyst.stages.validator import SqlValidator

DUPLICATE_WARN_SHARE = 0.0  # any duplicate is worth stating
MISSING_BANK_WARN_SHARE = 0.005
SETTLEMENT_METRICS = {"gmv", "successful_txns", "attempts", "avg_ticket_size"}


@dataclass
class QualityReport:
    warnings: list[QualityWarning] = field(default_factory=list)
    queries: list[tuple[str, QueryResult, list[str], str]] = field(default_factory=list)
    effective_days: dict[str, float] = field(default_factory=dict)  # window label -> days
    loaded_through: datetime | None = None
    blocking: bool = False


def _dt(d) -> datetime:
    return datetime.combine(d, datetime.min.time())


class DataQualityChecker:
    def __init__(self, validator: SqlValidator, executor: QueryExecutor):
        self.validator = validator
        self.executor = executor

    def _run(self, report: QualityReport, purpose: str, sql: str, user: User) -> QueryResult:
        vq = self.validator.validate(sql, user)
        result = self.executor.run(vq)
        report.queries.append((purpose, result, vq.applied_controls, vq.original_sql))
        return result

    def check(self, plan: Plan, user: User) -> QualityReport:
        report = QualityReport()
        windows: list[tuple[str, TimeWindow]] = []
        if plan.current:
            windows.append(("current", plan.current))
        if plan.baseline:
            windows.append(("baseline", plan.baseline))
        if not windows:
            return report

        self._freshness(report, windows, user)
        if report.blocking:
            return report
        self._duplicates(report, windows, user)
        self._missing_bank(report, windows, user, plan)
        if plan.metric in SETTLEMENT_METRICS:
            self._settlement_reconciliation(report, windows, user)
        return report

    # ---------------------------------------------------------------- checks
    def _freshness(self, report, windows, user) -> None:
        r = self._run(
            report, "Data freshness",
            "SELECT loaded_through FROM data_freshness WHERE table_name = 'transactions'", user,
        )
        loaded = r.data["loaded_through"].iloc[0]
        loaded = loaded.to_pydatetime() if hasattr(loaded, "to_pydatetime") else loaded
        report.loaded_through = loaded
        for name, w in windows:
            start, end = _dt(w.start), _dt(w.end)
            covered = max(timedelta(0), min(end, loaded) - start)
            days = covered.total_seconds() / 86400
            report.effective_days[name] = days
            if days <= 0:
                report.blocking = True
                report.warnings.append(QualityWarning(
                    check="freshness", severity="critical", query_id=r.query_id,
                    message=(f"No data loaded for the {name} window {w.describe()}: "
                             f"transactions are only loaded through {loaded:%d %b %Y %H:%M}."),
                ))
            elif days < w.days:
                report.warnings.append(QualityWarning(
                    check="freshness", severity="warning", query_id=r.query_id,
                    message=(f"The {name} window {w.describe()} is incomplete: data is loaded "
                             f"only through {loaded:%d %b %Y %H:%M} ({days:.1f} of {w.days} days). "
                             "Rates remain comparable; volumes are compared as daily averages."),
                ))

    def _window_case(self, windows) -> tuple[str, str]:
        cases, preds = [], []
        for name, w in windows:
            pred = (f"(created_at >= DATE '{w.start}' AND created_at < DATE '{w.end}')")
            cases.append(f"WHEN {pred} THEN '{name}'")
            preds.append(pred)
        return f"CASE {' '.join(cases)} END", " OR ".join(preds)

    def _duplicates(self, report, windows, user) -> None:
        case, where = self._window_case(windows)
        r = self._run(report, "Duplicate check", f"""
SELECT {case} AS window_name,
       COUNT(*) AS raw_rows,
       COUNT(*) - COUNT(DISTINCT txn_id) AS duplicate_rows
FROM transactions
WHERE {where}
GROUP BY 1""", user)
        for _, row in r.data.iterrows():
            dupes, raw = int(row["duplicate_rows"]), int(row["raw_rows"])
            if dupes > 0 and dupes / raw > DUPLICATE_WARN_SHARE:
                day_r = self._run(report, "Duplicate rows by day", f"""
SELECT CAST(created_at AS DATE) AS day, COUNT(*) - COUNT(DISTINCT txn_id) AS duplicate_rows
FROM transactions
WHERE {where}
GROUP BY 1
HAVING COUNT(*) > COUNT(DISTINCT txn_id)
ORDER BY 1""", user)
                days = ", ".join(f"{d:%d %b}" for d in day_r.data["day"])
                report.warnings.append(QualityWarning(
                    check="duplicates", severity="warning", query_id=day_r.query_id,
                    message=(f"{dupes:,} duplicate rows ({dupes / raw:.1%}) in the "
                             f"{row['window_name']} window (on {days}) - same txn_id ingested "
                             "twice. All metrics use de-duplicated transactions; raw counts would "
                             "overstate volume."),
                ))

    def _missing_bank(self, report, windows, user, plan) -> None:
        case, where = self._window_case(windows)
        r = self._run(report, "Missing bank_id check", f"""
SELECT {case} AS window_name,
       COUNT(*) FILTER (WHERE payment_method <> 'WALLET') AS non_wallet_rows,
       COUNT(*) FILTER (WHERE bank_id IS NULL AND payment_method <> 'WALLET') AS missing_bank
FROM (SELECT DISTINCT * FROM transactions)
WHERE {where}
GROUP BY 1""", user)
        uses_bank = any(d in ("bank", "bank_type") for d in plan.dimensions) or plan.intent in (
            "explain_change",
        )
        for _, row in r.data.iterrows():
            missing, total = int(row["missing_bank"]), int(row["non_wallet_rows"])
            if total and missing / total > MISSING_BANK_WARN_SHARE:
                report.warnings.append(QualityWarning(
                    check="missing_values", severity="warning" if uses_bank else "info",
                    query_id=r.query_id,
                    message=(f"{missing:,} card/UPI/netbanking transactions "
                             f"({missing / total:.1%}) "
                             f"in the {row['window_name']} window have no bank_id. Bank-level "
                             "results show them as '(none)'. (Wallet payments have no bank by "
                             "design and are not counted here.)"),
                ))

    def _settlement_reconciliation(self, report, windows, user) -> None:
        case, where = self._window_case(windows)
        settle_where = where.replace("created_at", "settlement_date")
        sql = f"""
WITH txn AS (
  SELECT merchant_id, CAST(created_at AS DATE) AS day, COUNT(*) AS txn_count,
         SUM(amount) AS gross
  FROM (SELECT DISTINCT * FROM transactions)
  WHERE status = 'SUCCESS' AND ({where})
  GROUP BY 1, 2
), st AS (
  SELECT merchant_id, settlement_date AS day, txn_count, gross_amount
  FROM settlements
  WHERE {settle_where}
)
SELECT txn.merchant_id, txn.day, txn.txn_count AS txn_table_count,
       st.txn_count AS settlement_count, txn.gross AS txn_table_gross,
       st.gross_amount AS settlement_gross
FROM txn JOIN st ON st.merchant_id = txn.merchant_id AND st.day = txn.day
WHERE txn.txn_count <> st.txn_count
ORDER BY txn.day, txn.merchant_id"""
        try:
            r = self._run(report, "Settlement reconciliation", sql, user)
        except AccessDenied:
            report.warnings.append(QualityWarning(
                check="reconciliation", severity="info",
                message="Settlement reconciliation skipped: your role cannot read settlements.",
            ))
            return
        if r.row_count:
            gap = (r.data["txn_table_gross"].astype(float)
                   - r.data["settlement_gross"].astype(float)).sum()
            days = sorted({f"{d:%d %b}" for d in r.data["day"]})
            report.warnings.append(QualityWarning(
                check="reconciliation", severity="warning", query_id=r.query_id,
                message=(f"Sources disagree: {r.row_count} merchant-days ({', '.join(days)}) have "
                         "different successful-transaction counts in transactions vs settlements "
                         f"(gross gap ₹{gap:,.0f}). This analysis uses the transactions table; "
                         "settlement figures for these days need review."),
            ))
