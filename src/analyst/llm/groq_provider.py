"""Groq chat-completions provider (OpenAI-compatible API)."""

from __future__ import annotations

import time

from analyst.core.errors import LLMError
from analyst.llm.base import LLMUsage


class GroqProvider:
    name = "groq"

    def __init__(self, api_key: str, model: str, timeout_seconds: float = 30.0,
                 max_retries: int = 1):
        if not api_key:
            raise LLMError("GROQ_API_KEY is not set")
        try:
            from groq import Groq
        except ImportError as e:  # pragma: no cover - dependency is in pyproject
            raise LLMError("groq package not installed") from e
        self.model = model
        self.max_retries = max_retries
        self._client = Groq(api_key=api_key, timeout=timeout_seconds)

    def complete(self, system: str, user: str, *, json_mode: bool = False,
                 purpose: str = "", usage: LLMUsage | None = None) -> str:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            start = time.perf_counter()
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    temperature=0,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                    response_format={"type": "json_object"} if json_mode else None,
                )
                ms = (time.perf_counter() - start) * 1000
                if usage is not None and resp.usage is not None:
                    usage.record(purpose, resp.usage.prompt_tokens,
                                 resp.usage.completion_tokens, ms, True)
                content = resp.choices[0].message.content or ""
                if not content.strip():
                    raise LLMError("empty completion")
                return content
            except Exception as e:  # network, rate limit, provider outage, empty output
                last_error = e
                if usage is not None:
                    usage.record(purpose, 0, 0, (time.perf_counter() - start) * 1000, False)
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"Groq call failed after {self.max_retries + 1} attempts: {last_error}")
