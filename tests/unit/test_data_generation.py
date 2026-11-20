"""The planted scenarios are the demo's answer key - these tests keep them intact."""

from data.generate import (
    DUPLICATE_DAY,
    LAST_DAY,
    NULL_BANK_DAYS,
    OUTAGE_BANK_ID,
    OUTAGE_DAYS,
    SETTLEMENT_MISMATCH_COUNT,
    generate,
)

SR = (
    "100.0 * COUNT(*) FILTER (WHERE status = 'SUCCESS') "
    "/ COUNT(*) FILTER (WHERE status <> 'USER_CANCELLED')"
)


def scalar(con, sql):
    return con.execute(sql).fetchone()[0]


def test_generation_is_deterministic():
    a, b = generate(seed=7), generate(seed=7)
    assert a.transactions.equals(b.transactions)


def test_all_tables_present(con):
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert {"transactions", "merchants", "banks", "settlements", "data_freshness"} <= tables


def test_week8_success_rate_drops_with_upi_mix_shift(con):
    w7 = scalar(con, f"SELECT {SR} FROM (SELECT DISTINCT * FROM transactions) "
                     "WHERE created_at >= '2026-11-02' AND created_at < '2026-11-09'")
    w8 = scalar(con, f"SELECT {SR} FROM (SELECT DISTINCT * FROM transactions) "
                     "WHERE created_at >= '2026-11-09'")
    upi_w8 = scalar(con, "SELECT AVG((payment_method = 'UPI')::INT) FROM transactions "
                         "WHERE created_at >= '2026-11-09'")
    assert w7 - w8 > 1.2, "overall success rate should fall noticeably in week 8"
    assert upi_w8 > 0.5


def test_card_success_rate_flat_across_weeks(con):
    """Per-segment rates stay flat for most segments -> the drop is mostly mix."""
    rows = con.execute(f"""
        SELECT created_at >= '2026-11-09' AS w8, {SR}
        FROM (SELECT DISTINCT * FROM transactions)
        WHERE payment_method = 'CARD' AND created_at >= '2026-11-02'
        GROUP BY 1
    """).fetchall()
    rates = dict(rows)
    assert abs(rates[True] - rates[False]) < 0.6


def test_bank_outage(con):
    for day in OUTAGE_DAYS:
        sr = scalar(con, f"SELECT {SR} FROM transactions WHERE bank_id = '{OUTAGE_BANK_ID}' "
                         f"AND payment_method = 'NETBANKING' AND created_at::DATE = '{day}'")
        assert sr < 50


def test_duplicates_only_on_planted_day(con):
    rows = con.execute("""
        SELECT created_at::DATE, COUNT(*) - COUNT(DISTINCT txn_id)
        FROM transactions GROUP BY 1 HAVING COUNT(*) > COUNT(DISTINCT txn_id)
    """).fetchall()
    assert [r[0] for r in rows] == [DUPLICATE_DAY]
    assert rows[0][1] > 100


def test_missing_bank_ids_only_on_planted_days(con):
    days = {r[0] for r in con.execute("""
        SELECT DISTINCT created_at::DATE FROM transactions
        WHERE bank_id IS NULL AND payment_method <> 'WALLET'
    """).fetchall()}
    assert days == set(NULL_BANK_DAYS)


def test_last_day_is_partial(con):
    last_n = scalar(con, f"SELECT COUNT(*) FROM transactions WHERE created_at::DATE = '{LAST_DAY}'")
    prev_n = scalar(con, f"SELECT COUNT(*) FROM transactions "
                         f"WHERE created_at::DATE = DATE '{LAST_DAY}' - 1")
    assert last_n < 0.25 * prev_n


def test_settlement_mismatches(con):
    n = scalar(con, """
        WITH t AS (
            SELECT merchant_id, created_at::DATE AS d, COUNT(*) AS c
            FROM (SELECT DISTINCT * FROM transactions) WHERE status = 'SUCCESS' GROUP BY 1, 2
        )
        SELECT COUNT(*) FROM t JOIN settlements s
          ON s.merchant_id = t.merchant_id AND s.settlement_date = t.d
        WHERE s.txn_count <> t.c
    """)
    assert n == SETTLEMENT_MISMATCH_COUNT
