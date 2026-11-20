from datetime import date

import pytest

from analyst.core.models import Plan, TimeWindow
from analyst.core.semantic import get_user, load_semantic_layer
from analyst.stages.executor import QueryExecutor
from analyst.stages.quality import DataQualityChecker
from analyst.stages.validator import SqlValidator


@pytest.fixture()
def checker(db_path):
    ex = QueryExecutor(db_path)
    yield DataQualityChecker(SqlValidator(load_semantic_layer()), ex)
    ex.close()


def plan(cur, base=None, metric="success_rate"):
    return Plan(
        intent="explain_change", metric=metric,
        current=TimeWindow(start=cur[0], end=cur[1]),
        baseline=TimeWindow(start=base[0], end=base[1]) if base else None,
    )


WEEK7 = (date(2026, 11, 2), date(2026, 11, 9))
WEEK8 = (date(2026, 11, 9), date(2026, 11, 16))


def checks(report):
    return {w.check for w in report.warnings}


def test_week8_flags_partial_window_and_duplicates(checker):
    r = checker.check(plan(WEEK8, WEEK7), get_user("rohit"))
    assert {"freshness", "duplicates"} <= checks(r)
    assert r.effective_days["baseline"] == 7
    assert 6 < r.effective_days["current"] < 7
    assert not r.blocking


def test_future_window_is_blocking(checker):
    r = checker.check(plan((date(2026, 12, 1), date(2026, 12, 8))), get_user("rohit"))
    assert r.blocking
    assert any(w.severity == "critical" for w in r.warnings)


def test_missing_bank_ids_flagged(checker):
    r = checker.check(plan((date(2026, 10, 5), date(2026, 10, 12))), get_user("rohit"))
    assert "missing_values" in checks(r)


def test_clean_week_has_no_warnings(checker):
    r = checker.check(plan((date(2026, 9, 28), date(2026, 10, 5))), get_user("rohit"))
    assert r.warnings == []


def test_settlement_mismatch_flagged_for_gmv(checker):
    r = checker.check(plan((date(2026, 10, 26), date(2026, 11, 2)), metric="gmv"),
                      get_user("rohit"))
    msg = [w.message for w in r.warnings if w.check == "reconciliation"]
    assert msg and "12 merchant-days" in msg[0]


def test_settlement_check_skipped_without_access(checker):
    r = checker.check(plan((date(2026, 10, 26), date(2026, 11, 2)), metric="gmv"),
                      get_user("neha"))
    rec = [w for w in r.warnings if w.check == "reconciliation"]
    assert rec and rec[0].severity == "info"
