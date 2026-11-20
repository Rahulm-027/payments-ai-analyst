from pathlib import Path

import duckdb
import pytest

from data.generate import generate, write_duckdb


@pytest.fixture(scope="session")
def db_path(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("db") / "payments.duckdb"
    write_duckdb(generate(seed=42), path)
    return path


@pytest.fixture()
def con(db_path):
    c = duckdb.connect(str(db_path), read_only=True)
    yield c
    c.close()
