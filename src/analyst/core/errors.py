"""Typed errors. Each carries a stable `code` so the API and UI can react precisely."""

from __future__ import annotations


class AnalystError(Exception):
    code = "analyst_error"
    user_message = "Something went wrong while analysing your question."

    def __init__(self, detail: str = "", *, user_message: str | None = None):
        super().__init__(detail or self.user_message)
        self.detail = detail
        if user_message:
            self.user_message = user_message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.user_message, "detail": self.detail}


class UnsafeQuery(AnalystError):
    code = "unsafe_query"
    user_message = "The generated query was blocked by the safety checker."


class AccessDenied(AnalystError):
    code = "access_denied"
    user_message = "You don't have access to the data this question needs."


class QueryTooExpensive(AnalystError):
    code = "query_too_expensive"
    user_message = "The query would scan too much data and was not run."


class QueryTimeout(AnalystError):
    code = "query_timeout"
    user_message = "The query took too long and was stopped."


class QueryExecutionError(AnalystError):
    code = "query_failed"
    user_message = "The query failed to run."


class AmbiguousQuestion(AnalystError):
    code = "ambiguous_question"
    user_message = "The question is ambiguous - please clarify."


class LLMError(AnalystError):
    code = "llm_error"
    user_message = "The language model returned an unusable response."
