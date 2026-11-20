"""Runs validated SQL against DuckDB with defence in depth.

- Read-only connection with external file/network access disabled at the engine level,
  so even a query that slipped past the validator cannot touch the filesystem.
- Pre-flight cost check using DuckDB's EXPLAIN plan (cartesian products and huge
  intermediate results are refused before execution).
- Hard timeout via connection interrupt.
- Row cap with truncation flag.
- In-memory LRU result cache keyed on the exact SQL.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path

import duckdb
import pandas as pd

from analyst.core.errors import QueryExecutionError, QueryTimeout, QueryTooExpensive
from analyst.core.models import QueryResult, ValidatedQuery


class ResultCache:
    def __init__(self, max_entries: int = 256, ttl_seconds: float = 900):
        self.max_entries = max_entries
        self.ttl = ttl_seconds
        self._data: OrderedDict[str, tuple[float, QueryResult]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> QueryResult | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            stored_at, result = item
            if time.monotonic() - stored_at > self.ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return result

    def put(self, key: str, result: QueryResult) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), result)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def query_id_for(sql: str) -> str:
    return "q_" + hashlib.sha256(sql.encode()).hexdigest()[:10]


class QueryExecutor:
    def __init__(
        self,
        db_path: Path | str,
        timeout_seconds: float = 10.0,
        max_rows: int = 5000,
        max_estimated_rows: int = 20_000_000,
        cache: ResultCache | None = None,
    ):
        self.db_path = str(db_path)
        self.timeout_seconds = timeout_seconds
        self.max_rows = max_rows
        self.max_estimated_rows = max_estimated_rows
        self.cache = cache if cache is not None else ResultCache()
        self._con = duckdb.connect(
            self.db_path,
            read_only=True,
            config={"enable_external_access": False, "lock_configuration": True},
        )

    def close(self) -> None:
        self._con.close()

    # ---------------------------------------------------------------- cost
    def estimate_cost(self, sql: str) -> int:
        """Largest estimated cardinality of any operator in the plan."""
        cur = self._con.cursor()
        try:
            rows = cur.execute(f"EXPLAIN (FORMAT JSON) {sql}").fetchall()
        except duckdb.Error as e:
            raise QueryExecutionError(str(e)) from e
        finally:
            cur.close()
        plan = json.loads(rows[0][1])
        max_card = 0

        def walk(node: dict) -> None:
            nonlocal max_card
            if node.get("name") == "CROSS_PRODUCT":
                raise QueryTooExpensive("plan contains a CROSS_PRODUCT")
            card = (node.get("extra_info") or {}).get("Estimated Cardinality")
            if card is not None:
                try:
                    max_card = max(max_card, int(str(card).lstrip("~")))
                except ValueError:
                    pass
            for child in node.get("children", []):
                walk(child)

        for root in plan:
            walk(root)
        if max_card > self.max_estimated_rows:
            raise QueryTooExpensive(
                f"estimated {max_card:,} rows > limit {self.max_estimated_rows:,}"
            )
        return max_card

    # ---------------------------------------------------------------- run
    def run(self, query: ValidatedQuery | str, *, use_cache: bool = True) -> QueryResult:
        sql = query.sql if isinstance(query, ValidatedQuery) else query
        qid = query_id_for(sql)
        if use_cache and (hit := self.cache.get(qid)) is not None:
            return hit.model_copy(update={"cached": True})

        self.estimate_cost(sql)

        cur = self._con.cursor()
        timer = threading.Timer(self.timeout_seconds, cur.interrupt)
        start = time.perf_counter()
        timer.start()
        try:
            cur.execute(sql)
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(self.max_rows + 1)
        except duckdb.InterruptException as e:
            raise QueryTimeout(f"exceeded {self.timeout_seconds}s") from e
        except duckdb.Error as e:
            raise QueryExecutionError(str(e)) from e
        finally:
            timer.cancel()
            cur.close()
        elapsed_ms = (time.perf_counter() - start) * 1000

        truncated = len(rows) > self.max_rows
        df = pd.DataFrame(rows[: self.max_rows], columns=columns)
        result = QueryResult(
            query_id=qid,
            sql=sql,
            data=df,
            row_count=len(df),
            truncated=truncated,
            elapsed_ms=round(elapsed_ms, 1),
        )
        if use_cache:
            self.cache.put(qid, result)
        return result
