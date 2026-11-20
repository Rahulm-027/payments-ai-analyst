"""LLM behaviour with a scripted fake provider - no network, fully deterministic."""

import json
from dataclasses import replace

import pytest

from analyst.core.errors import LLMError
from analyst.core.pipeline import AnalystPipeline
from analyst.stages.narrator import ungrounded_numbers
from config.settings import settings


class FakeLLM:
    name = "fake"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, user, *, json_mode=False, purpose="", usage=None):
        self.calls.append(purpose)
        if usage is not None:
            usage.record(purpose, 100, 50, 5.0, True)
        if not self.responses:
            raise LLMError("no scripted response left")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)


@pytest.fixture()
def make_pipe(db_path):
    pipes = []

    def _make(responses):
        cfg = replace(settings, db_path=db_path, llm_mode="live")
        p = AnalystPipeline(cfg, llm=FakeLLM(responses))
        pipes.append(p)
        return p

    yield _make
    for p in pipes:
        p.executor.close()


WEEK8_PLAN = {
    "intent": "explain_change", "metric": "success_rate",
    "dimensions": ["payment_method", "channel", "bank"],
    "filters": [],
    "current": {"start": "2026-11-09", "end": "2026-11-16", "label": "last week"},
    "baseline": {"start": "2026-11-02", "end": "2026-11-09", "label": "week before"},
    "assumptions": ["'Last week' = 9-15 Nov"],
}


def test_valid_llm_plan_is_used(make_pipe):
    p = make_pipe([WEEK8_PLAN, "Success rate fell from 92.79% to 90.84%, mostly mix."])
    a = p.ask("why did SR drop last week", "rohit")
    assert a.status == "answered" and a.plan.planner == "llm"
    assert a.summary == "Success rate fell from 92.79% to 90.84%, mostly mix."


def test_malformed_plan_retries_then_falls_back_to_rules(make_pipe):
    p = make_pipe(["not json at all", '{"intent": "explain_change", "metric": "nope"}',
                   "summary"])
    a = p.ask("Why did success rate drop last week?", "rohit")
    assert a.status == "answered" and a.plan.planner == "rules"
    plan_step = next(t for t in a.trace if t.stage == "plan")
    assert plan_step.status == "warning" and "fell back" in plan_step.detail


def test_invalid_filter_value_rejected(make_pipe):
    bad = {**WEEK8_PLAN, "filters": [{"dimension": "bank", "values": ["Atlantis Bank"]}]}
    p = make_pipe([bad, WEEK8_PLAN, "ok summary"])
    a = p.ask("why did SR drop", "rohit")
    assert a.plan.planner == "llm"
    assert "Atlantis" in next(t for t in a.trace if t.stage == "plan").detail


def test_provider_outage_degrades_to_offline_behaviour(make_pipe):
    p = make_pipe([LLMError("503 provider unavailable")] * 4)
    a = p.ask("Why did success rate drop last week?", "rohit")
    assert a.status == "answered"
    assert "mix effect" in a.summary  # template summary kept
    assert any(t.stage == "narrate_llm" and t.status == "warning" for t in a.trace)


def test_ungrounded_summary_is_rejected(make_pipe):
    p = make_pipe([WEEK8_PLAN, "Success rate collapsed by 7.5 pp because of a 40% UPI outage."])
    a = p.ask("why did SR drop last week", "rohit")
    assert "7.5" not in a.summary and "mix effect" in a.summary
    step = next(t for t in a.trace if t.stage == "narrate_llm")
    assert step.status == "warning" and "ungrounded" in step.detail


def test_custom_sql_unsafe_first_attempt_is_corrected(make_pipe):
    plan = {"intent": "custom_sql", "metric": "success_rate",
            "custom_request": "count settlements per merchant"}
    p = make_pipe([
        plan,
        {"sql": "DROP TABLE settlements", "explanation": "x"},
        {"sql": "SELECT merchant_id, COUNT(*) AS n FROM settlements GROUP BY 1 ORDER BY 2 DESC",
         "explanation": "Settlement records per merchant."},
    ])
    a = p.ask("how many settlement records does each merchant have?", "rohit")
    assert a.status == "answered"
    assert a.tables and a.tables[0].rows
    gen = next(t for t in a.trace if t.stage == "generate_sql")
    assert gen.status == "warning" and "2 attempt" in gen.detail


def test_custom_sql_never_retries_around_access_denied(make_pipe):
    plan = {"intent": "custom_sql", "metric": "success_rate", "custom_request": "card bins"}
    p = make_pipe([plan, {"sql": "SELECT card_bin FROM transactions", "explanation": "x"}])
    a = p.ask("list card bins", "rohit")
    assert a.status == "denied"


def test_custom_sql_fails_closed_after_retries(make_pipe):
    plan = {"intent": "custom_sql", "metric": "success_rate", "custom_request": "r"}
    p = make_pipe([plan] + [{"sql": "DELETE FROM banks", "explanation": "x"}] * 3)
    a = p.ask("delete the banks", "rohit")
    assert a.status == "blocked"


def test_llm_usage_is_tracked(make_pipe):
    p = make_pipe([WEEK8_PLAN, "Success rate fell from 92.79% to 90.84%."])
    a = p.ask("why did SR drop last week", "rohit")
    usage = next(t for t in a.trace if t.stage == "llm_usage")
    assert "2 calls" in usage.detail


@pytest.mark.parametrize("text,ok", [
    ("Rate fell from 92.79% to 90.84% (-1.95 pp).", True),
    ("Rate fell by 3.2 pp.", False),
    ("Two drivers explain it.", True),
])
def test_grounding_check(text, ok):
    source = "Payment success rate fell from 92.79% to 90.84% (-1.95 pp)."
    assert (ungrounded_numbers(text, source) == []) is ok
