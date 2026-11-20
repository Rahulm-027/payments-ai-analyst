from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import analyst.api.main as api
from analyst.core.pipeline import AnalystPipeline
from config.settings import settings


@pytest.fixture(scope="module")
def client(db_path):
    pipe = AnalystPipeline(replace(settings, db_path=db_path), use_llm_from_settings=False)
    api.state["pipeline"] = pipe
    # Bypass lifespan so the test pipeline (temp DB) is used.
    api.app.router.lifespan_context = _noop_lifespan
    with TestClient(api.app) as c:
        yield c
    pipe.executor.close()


from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _noop_lifespan(app):
    yield


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["mode"] == "offline"


def test_ask_requires_known_user(client):
    assert client.post("/ask", json={"question": "why did SR drop"}).status_code == 401
    assert client.post("/ask", json={"question": "why", "user": "mallory"}).status_code == 401


def test_ask_via_header(client):
    r = client.post("/ask", json={"question": "Why did success rate drop last week?"},
                    headers={"X-User": "rohit"})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "answered"
    assert body["queries"] and body["facts"] and r.headers["X-Request-ID"]


def test_schema_hides_blocked_columns(client):
    cols = client.get("/schema", headers={"X-User": "rohit"}).json()["tables"]["transactions"]
    assert "card_bin" not in cols
    assert "settlements" not in client.get("/schema", headers={"X-User": "neha"}).json()["tables"]


def test_sql_endpoint_blocks_writes(client):
    r = client.post("/sql", json={"sql": "DELETE FROM banks", "user": "asha"})
    assert r.json()["status"] == "blocked"
