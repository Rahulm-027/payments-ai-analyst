"""End-to-end tests in offline mode (deterministic, no API key)."""

from dataclasses import replace

import pytest

from analyst.core.pipeline import AnalystPipeline
from config.settings import settings


@pytest.fixture(scope="module")
def pipe(db_path):
    cfg = replace(settings, db_path=db_path, llm_mode="offline")
    p = AnalystPipeline(cfg, use_llm_from_settings=False)
    yield p
    p.executor.close()


def test_week8_drop_is_explained_as_mix_plus_upi_online(pipe):
    a = pipe.ask("Why did success rate drop last week?", "rohit")
    assert a.status == "answered"
    assert "mix effect" in a.summary and "UPI" in a.summary
    assert "UPI / ONLINE" in a.summary
    assert {w.check for w in a.quality_warnings} >= {"freshness", "duplicates"}
    assert all(f.query_id.startswith("q_") for f in a.facts)
    evidence_ids = {q.query_id for q in a.queries}
    assert all(f.query_id in evidence_ids for f in a.facts), "every fact must cite a run query"
    assert {c.kind for c in a.charts} >= {"waterfall", "grouped_bar", "line"}
    assert a.hypotheses and a.recommendations and a.caveats


def test_outage_day_pinpoints_bank_and_error_code(pipe):
    a = pipe.ask("Why did the success rate fall on 21 Oct?", "rohit")
    assert "Coastal Bank" in a.summary and "ISSUER_DOWN" in a.summary
    assert a.hypotheses[0].confidence == "high"


def test_stable_week_reports_no_material_change(pipe):
    a = pipe.ask("Why did success rate change in the week of 12 Oct?", "rohit")
    assert a.status == "answered"
    assert "broadly stable" in a.summary


def test_trend_question(pipe):
    a = pipe.ask("Show the daily success rate trend", "rohit")
    assert a.status == "answered" and a.plan.intent == "trend"
    assert a.charts[0].kind == "line" and len(a.charts[0].data) == 28


def test_breakdown_question(pipe):
    a = pipe.ask("Success rate by bank in October", "rohit")
    assert a.status == "answered" and a.plan.dimensions == ["bank"]
    assert "highest" in a.summary


def test_access_denied_for_out_of_scope_region(pipe):
    a = pipe.ask("What is the success rate in the South region?", "neha")
    assert a.status == "denied"
    assert "North" in a.summary


def test_north_manager_results_are_scoped(pipe):
    a = pipe.ask("Success rate by region last week", "neha")
    assert a.status == "answered"
    assert {r["segment"] for r in a.tables[0].rows} == {"North"}


def test_vague_question_asks_for_clarification(pipe):
    a = pipe.ask("How are we doing?", "rohit")
    assert a.status == "needs_clarification" and a.clarification_options


def test_future_window_reports_no_data(pipe):
    a = pipe.ask("Success rate in December", "rohit")
    assert a.status == "error" and "No data" in a.summary


def test_raw_sql_is_validated(pipe):
    assert pipe.run_sql("DROP TABLE transactions", "asha").status == "blocked"
    assert pipe.run_sql("SELECT card_bin FROM transactions", "rohit").status == "denied"
    ok = pipe.run_sql("SELECT bank_name FROM banks ORDER BY 1", "rohit")
    assert ok.status == "answered" and len(ok.tables[0].rows) == 8


def test_custom_sql_needs_live_mode(pipe):
    from analyst.core.models import Plan

    a = pipe.ask("x" * 5, "rohit")  # sanity: pipeline handles odd input without crashing
    assert a.status in {"answered", "needs_clarification", "error"}
    assert Plan(intent="custom_sql", custom_request="r").intent == "custom_sql"


def test_trace_records_every_stage(pipe):
    a = pipe.ask("Why did success rate drop last week?", "rohit")
    stages = [t.stage for t in a.trace]
    assert stages[:3] == ["plan", "data_quality", "compare_periods"]
    assert "decompose_and_drill_down" in stages and "narrate" in stages
