"""HTTP API.

Authentication is mocked: the caller picks a demo user (body field or X-User header). In a
real deployment this would come from the identity provider; everything downstream already
works off the resolved role.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from analyst.core.models import Answer
from analyst.core.pipeline import AnalystPipeline
from analyst.core.semantic import load_roles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("analyst.api")

state: dict[str, AnalystPipeline] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["pipeline"] = AnalystPipeline()
    log.info("pipeline ready (mode=%s)", state["pipeline"].mode)
    yield
    state["pipeline"].executor.close()


app = FastAPI(
    title="Payments AI Business Analyst",
    description="Natural-language analysis of payment performance with evidence and caveats.",
    version="0.1.0",
    lifespan=lifespan,
)


def pipeline() -> AnalystPipeline:
    return state["pipeline"]


@app.middleware("http")
async def request_log(request: Request, call_next):
    rid = request.headers.get("X-Request-ID", uuid.uuid4().hex[:8])
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    log.info("rid=%s %s %s -> %s %.0fms", rid, request.method, request.url.path,
             response.status_code, (time.perf_counter() - start) * 1000)
    return response


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    user: str | None = None


class SqlRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)
    user: str | None = None


def _resolve_user(body_user: str | None, header_user: str | None) -> str:
    username = body_user or header_user
    _, users = load_roles()
    if not username:
        raise HTTPException(status_code=401, detail="Provide a user (body 'user' or X-User).")
    if username not in users:
        raise HTTPException(status_code=401, detail=f"Unknown user '{username}'.")
    return username


@app.get("/health")
def health() -> dict:
    p = pipeline()
    return {"status": "ok", "mode": p.mode, "data_loaded_through": p.catalog.as_of.isoformat()}


@app.get("/users")
def users() -> list[dict]:
    _, us = load_roles()
    return [{"username": u.username, "display": u.display, "role": u.role.name,
             "description": u.role.description} for u in us.values()]


@app.get("/schema")
def schema(x_user: str | None = Header(default=None), user: str | None = None) -> dict:
    username = _resolve_user(user, x_user)
    p = pipeline()
    role = load_roles()[1][username].role
    tables = {
        t: [c for c in cols if c not in role.blocked_columns.get(t, frozenset())]
        for t, cols in p.semantic.allowed_tables.items() if t in role.allowed_tables
    }
    return {
        "role": role.name,
        "tables": tables,
        "metrics": {m.name: {"label": m.label, "description": m.description}
                    for m in p.semantic.metrics.values()},
        "dimensions": list(p.semantic.dimensions),
        "data_loaded_through": p.catalog.as_of.isoformat(),
    }


@app.post("/ask", response_model=Answer)
def ask(body: AskRequest, x_user: str | None = Header(default=None)) -> Answer:
    return pipeline().ask(body.question, _resolve_user(body.user, x_user))


@app.post("/sql", response_model=Answer)
def run_sql(body: SqlRequest, x_user: str | None = Header(default=None)) -> Answer:
    return pipeline().run_sql(body.sql, _resolve_user(body.user, x_user))
