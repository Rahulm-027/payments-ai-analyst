"""LLM provider interface. Pipeline stages depend on this, never on a vendor SDK."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from analyst.core.errors import LLMError


@dataclass
class LLMUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    failures: int = 0
    log: list[dict] = field(default_factory=list)

    def record(self, purpose: str, prompt: int, completion: int, ms: float, ok: bool) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.latency_ms += ms
        self.failures += 0 if ok else 1
        self.log.append({"purpose": purpose, "prompt_tokens": prompt,
                         "completion_tokens": completion, "ms": round(ms, 1), "ok": ok})


class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, user: str, *, json_mode: bool = False,
                 purpose: str = "", usage: LLMUsage | None = None) -> str: ...


def parse_json_object(text: str) -> dict:
    """Parse a JSON object from model output, tolerating ```json fences."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            raise LLMError("model did not return JSON") from None
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise LLMError(f"malformed JSON from model: {e}") from e
    if not isinstance(obj, dict):
        raise LLMError("model returned JSON that is not an object")
    return obj
