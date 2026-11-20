import pytest

from analyst.core.errors import QueryExecutionError, QueryTimeout, QueryTooExpensive
from analyst.core.semantic import get_user, load_semantic_layer
from analyst.stages.executor import QueryExecutor, ResultCache
from analyst.stages.validator import SqlValidator


@pytest.fixture()
def executor(db_path):
    ex = QueryExecutor(db_path, timeout_seconds=5, max_rows=50)
    yield ex
    ex.close()


def test_runs_query_and_returns_dataframe(executor):
    r = executor.run("SELECT bank_id, bank_name FROM banks ORDER BY bank_id")
    assert r.row_count == 8
    assert list(r.data.columns) == ["bank_id", "bank_name"]
    assert not r.truncated


def test_truncation_flag(executor):
    r = executor.run("SELECT txn_id FROM transactions")
    assert r.row_count == 50 and r.truncated


def test_cache_hit_on_repeat(executor):
    sql = "SELECT COUNT(*) AS n FROM merchants"
    assert executor.run(sql).cached is False
    second = executor.run(sql)
    assert second.cached is True
    assert second.data.equals(executor.run(sql, use_cache=False).data)


def test_cache_lru_eviction():
    cache = ResultCache(max_entries=2)
    import pandas as pd

    from analyst.core.models import QueryResult

    def res(q):
        return QueryResult(query_id=q, sql=q, data=pd.DataFrame(), row_count=0,
                           truncated=False, elapsed_ms=0)

    cache.put("a", res("a"))
    cache.put("b", res("b"))
    cache.get("a")
    cache.put("c", res("c"))
    assert cache.get("b") is None and cache.get("a") is not None


def test_cross_product_refused(executor):
    with pytest.raises(QueryTooExpensive):
        executor.run("SELECT * FROM transactions a, banks b", use_cache=False)


def test_exploding_self_join_refused(executor):
    with pytest.raises(QueryTooExpensive):
        executor.run(
            "SELECT COUNT(*) FROM transactions a JOIN transactions b "
            "ON a.merchant_id = b.merchant_id",
            use_cache=False,
        )


def test_timeout(db_path):
    ex = QueryExecutor(db_path, timeout_seconds=0.3, max_estimated_rows=10**12)
    try:
        with pytest.raises(QueryTimeout):
            ex.run("SELECT SUM(i * j) FROM range(200000) a(i), range(200000) b(j) "
                   "WHERE (i + j) % 7 = 3", use_cache=False)
    finally:
        ex.close()


def test_engine_blocks_file_access_even_without_validator(executor):
    with pytest.raises(QueryExecutionError):
        executor.run("SELECT * FROM read_csv('/etc/passwd')", use_cache=False)


def test_connection_is_read_only(executor):
    with pytest.raises(QueryExecutionError):
        executor.run("DELETE FROM banks", use_cache=False)


def test_validated_scoped_query_only_returns_north(executor):
    v = SqlValidator(load_semantic_layer(), max_rows=50)
    vq = v.validate(
        "SELECT DISTINCT m.region FROM transactions t "
        "JOIN merchants m ON m.merchant_id = t.merchant_id",
        get_user("neha"),
    )
    r = executor.run(vq)
    assert set(r.data["region"]) == {"North"}
