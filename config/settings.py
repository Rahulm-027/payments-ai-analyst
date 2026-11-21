"""Runtime settings loaded from environment variables (.env supported)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional at runtime
    pass

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


@dataclass(frozen=True)
class Settings:
    llm_mode: str = os.getenv("LLM_MODE", "offline")
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    db_path: Path = ROOT / os.getenv("DB_PATH", "data/payments.duckdb")
    query_timeout_seconds: float = float(os.getenv("QUERY_TIMEOUT_SECONDS", "10"))
    max_result_rows: int = int(os.getenv("MAX_RESULT_ROWS", "5000"))
    metrics_path: Path = CONFIG_DIR / "metrics.yaml"
    roles_path: Path = CONFIG_DIR / "roles.yaml"


settings = Settings()
