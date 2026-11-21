"""The golden-question set must stay green in offline mode."""

from dataclasses import replace
from pathlib import Path

import yaml

from analyst.core.pipeline import AnalystPipeline
from config.settings import settings
from eval.run_eval import GOLDEN, run


def test_golden_questions_pass_offline(db_path, tmp_path: Path):
    pipe = AnalystPipeline(replace(settings, db_path=db_path), use_llm_from_settings=False)
    try:
        cases = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))["cases"]
        results = run(pipe, cases)
    finally:
        pipe.executor.close()
    failed = {r.id: [c for c in r.checks if not c[1]] for r in results if not r.passed}
    assert not failed, failed
