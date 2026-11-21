"""Runs the golden-question set and writes eval/results.md.

    python -m eval.run_eval                 # offline (rule planner, deterministic)
    LLM_MODE=live python -m eval.run_eval   # with the Groq planner/narrator

Reference values are computed with independent, hand-written SQL directly against DuckDB,
not through the pipeline's query builder.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from analyst.core.models import Answer  # noqa: E402
from analyst.core.pipeline import AnalystPipeline  # noqa: E402

GOLDEN = Path(__file__).with_name("golden_questions.yaml")


@dataclass
class CaseResult:
    id: str
    category: str
    passed: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    status: str = ""
    seconds: float = 0.0


def fmt(value: float, kind: str) -> str:
    if kind == "percent":
        return f"{value:.2%}"
    if kind == "currency":
        return f"₹{value:,.0f}"
    return f"{value:,.0f}"


def evaluate_case(case: dict, answer: Answer, reference_sql: Callable[[str], float]) -> list:
    exp = case["expect"]
    checks: list[tuple[str, bool, str]] = []
    plan = answer.plan
    text = " ".join([answer.summary, *(f.statement for f in answer.facts)])

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    if "status" in exp:
        add("status", answer.status == exp["status"], f"got {answer.status}")
    if "intent" in exp:
        add("intent", plan is not None and plan.intent == exp["intent"],
            f"got {plan.intent if plan else None}")
    if "metric" in exp:
        add("metric", plan is not None and plan.metric == exp["metric"],
            f"got {plan.metric if plan else None}")
    if "dimensions" in exp:
        got = plan.dimensions if plan else []
        add("dimensions", got[: len(exp["dimensions"])] == exp["dimensions"], f"got {got}")
    for dim, values in (exp.get("filters") or {}).items():
        got = {f.dimension: f.values for f in (plan.filters if plan else [])}
        add(f"filter:{dim}", got.get(dim) == values, f"got {got}")
    # Graded on summary + facts: facts are computed deterministically; in live mode the
    # summary is an LLM rewording that may phrase the same finding differently.
    for s in exp.get("contains", []):
        add(f"contains:{s}", s in text)
    for s in exp.get("not_contains", []):
        add(f"not_contains:{s}", s not in text)
    fired = {w.check for w in answer.quality_warnings}
    for q in exp.get("quality", []):
        add(f"quality:{q}", q in fired, f"fired {sorted(fired)}")
    for ref in exp.get("reference", []):
        value = reference_sql(ref["sql"])
        expected = fmt(float(value), ref["format"])
        add(f"reference:{expected}", expected in text, "independent SQL value in answer")
    if exp.get("read_only"):
        writes = [q.sql for q in answer.queries
                  if not q.sql.lstrip().upper().startswith(("SELECT", "WITH"))]
        add("read_only", not writes, f"{len(answer.queries)} queries, {len(writes)} non-SELECT")
    for col, values in (exp.get("table_values") or {}).items():
        rows = answer.tables[0].rows if answer.tables else []
        got = sorted({str(r.get(col)) for r in rows})
        add(f"table:{col}", got == sorted(values), f"got {got}")
    return checks


def run(pipeline: AnalystPipeline, cases: list[dict]) -> list[CaseResult]:
    """Reference SQL runs on the pipeline's read-only connection but bypasses the
    planner, query builder and analyzer entirely."""

    def reference_sql(sql: str) -> float:
        return float(pipeline.executor.run(sql, use_cache=False).data.iloc[0, 0])

    results = []
    for case in cases:
        start = time.perf_counter()
        if "sql" in case:
            answer = pipeline.run_sql(case["sql"], case["user"])
        else:
            answer = pipeline.ask(case["question"], case["user"])
        checks = evaluate_case(case, answer, reference_sql)
        results.append(CaseResult(
            id=case["id"], category=case["category"], checks=checks, status=answer.status,
            passed=all(ok for _, ok, _ in checks),
            seconds=round(time.perf_counter() - start, 2)))
    return results


def write_report(results: list[CaseResult], mode: str, path: Path) -> str:
    n_pass = sum(r.passed for r in results)
    n_checks = sum(len(r.checks) for r in results)
    n_checks_ok = sum(ok for r in results for _, ok, _ in r.checks)
    cats: dict[str, list[CaseResult]] = {}
    for r in results:
        cats.setdefault(r.category, []).append(r)
    lines = [
        "# Evaluation results", "",
        f"Mode: `{mode}` · cases passed: **{n_pass}/{len(results)}** · "
        f"checks passed: **{n_checks_ok}/{n_checks}**", "",
        "| Category | Cases passed |", "|---|---|",
        *(f"| {c} | {sum(r.passed for r in rs)}/{len(rs)} |" for c, rs in cats.items()),
        "", "| Case | Category | Status | Result | Time (s) | Failed checks |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        failed = "; ".join(f"{n} ({d})" for n, ok, d in r.checks if not ok) or "-"
        lines.append(f"| {r.id} | {r.category} | {r.status} | {'✅' if r.passed else '❌'} | "
                     f"{r.seconds} | {failed} |")
    report = "\n".join(lines) + "\n"
    path.write_text(report, encoding="utf-8")
    return report


def main() -> int:
    # Windows consoles default to cp1252, which can't print ✅/❌/₹.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.md")))
    args = parser.parse_args()
    cases = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))["cases"]
    pipeline = AnalystPipeline()
    results = run(pipeline, cases)
    report = write_report(results, pipeline.mode, Path(args.out))
    print(report)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
