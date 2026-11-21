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
                kwargs: dict = {
                    "model": self.model,
                    "temperature": 0,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                }
                if json_mode:  # only send response_format when needed
                    kwargs["response_format"] = {"type": "json_object"}
                resp = self._client.chat.completions.create(**kwargs)
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
                status = getattr(e, "status_code", None)
                if status in (400, 401, 403, 404):
                    break  # bad key / model / request: retrying won't help
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"Groq call failed ({_describe(last_error)})")


def _describe(e: Exception | None) -> str:
    """Short, readable error: status code first, then the provider's message."""
    if e is None:
        return "unknown error"
    status = getattr(e, "status_code", None)
    body = getattr(e, "body", None)
    msg = ""
    if isinstance(body, dict):
        err = body.get("error", body)
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
    msg = msg or str(e)
    hint = {401: " - check GROQ_API_KEY", 404: " - check GROQ_MODEL in the Groq console"}.get(
        status, "")
    return f"HTTP {status}: {msg[:200]}{hint}" if status else f"{type(e).__name__}: {msg[:200]}"
