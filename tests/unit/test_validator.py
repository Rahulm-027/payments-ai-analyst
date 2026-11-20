import pytest

from analyst.core.errors import AccessDenied, UnsafeQuery
from analyst.core.semantic import get_user, load_semantic_layer
from analyst.stages.validator import SqlValidator


@pytest.fixture(scope="module")
def validator():
    return SqlValidator(load_semantic_layer(), max_rows=5000)


ADMIN, ANALYST, NORTH = get_user("asha"), get_user("rohit"), get_user("neha")


# ----------------------------------------------------------------- allowed
def test_simple_select_passes_and_gets_limit(validator):
    vq = validator.validate("SELECT status, COUNT(*) FROM transactions GROUP BY 1", ADMIN)
    assert "LIMIT 5001" in vq.sql
    assert vq.tables == ["transactions"]


def test_cte_with_join_passes(validator):
    sql = """
        WITH t AS (SELECT DISTINCT * FROM transactions)
        SELECT b.bank_name, COUNT(*) FROM t LEFT JOIN banks b ON b.bank_id = t.bank_id GROUP BY 1
    """
    vq = validator.validate(sql, ADMIN)
    assert set(vq.tables) == {"transactions", "banks"}


def test_large_limit_is_capped(validator):
    vq = validator.validate("SELECT * FROM banks LIMIT 999999", ADMIN)
    assert "LIMIT 5001" in vq.sql
    assert any("capped" in c for c in vq.applied_controls)


def test_small_limit_kept(validator):
    vq = validator.validate("SELECT * FROM banks LIMIT 3", ADMIN)
    assert "LIMIT 3" in vq.sql


# ----------------------------------------------------------------- unsafe
@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE transactions",
        "DELETE FROM transactions",
        "UPDATE transactions SET amount = 0",
        "INSERT INTO banks VALUES ('X', 'Y', 'Z')",
        "CREATE TABLE x AS SELECT * FROM transactions",
        "SELECT 1; DROP TABLE transactions",
        "PRAGMA database_list",
        "ATTACH 'other.db'",
        "COPY transactions TO 'out.csv'",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_parquet('s3://bucket/x.parquet')",
        "SELECT * FROM glob('*')",
        "SELECT getenv('HOME')",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM duckdb_settings()",
        "SELECT * FROM secret_table",
        "SELECT * FROM transactions a, transactions b",
        "SELECT * FROM transactions CROSS JOIN merchants",
        "SELECT 1",
        "",
        "this is not sql",
    ],
)
def test_unsafe_queries_rejected(validator, sql):
    with pytest.raises((UnsafeQuery, AccessDenied)):
        validator.validate(sql, ADMIN)


# ----------------------------------------------------------------- RBAC
def test_analyst_cannot_select_card_bin(validator):
    with pytest.raises(AccessDenied, match="card_bin"):
        validator.validate("SELECT card_bin, COUNT(*) FROM transactions GROUP BY 1", ANALYST)


def test_analyst_star_hides_blocked_columns(validator):
    vq = validator.validate("SELECT * FROM transactions", ANALYST)
    assert "card_bin" not in vq.sql and "customer_id" not in vq.sql


def test_admin_can_read_card_bin(validator):
    vq = validator.validate("SELECT card_bin FROM transactions LIMIT 5", ADMIN)
    assert "card_bin" in vq.sql


def test_north_manager_gets_row_scope_on_transactions(validator):
    vq = validator.validate("SELECT COUNT(*) FROM transactions", NORTH)
    assert "region IN ('North')" in vq.sql
    assert any("row scope" in c for c in vq.applied_controls)


def test_north_manager_scope_survives_alias_and_cte(validator):
    sql = """
        WITH x AS (SELECT * FROM transactions AS tx)
        SELECT COUNT(*) FROM x
    """
    vq = validator.validate(sql, NORTH)
    assert "region IN ('North')" in vq.sql


def test_north_manager_explicit_south_is_denied(validator):
    sql = """
        SELECT COUNT(*) FROM transactions t JOIN merchants m ON m.merchant_id = t.merchant_id
        WHERE m.region = 'South'
    """
    with pytest.raises(AccessDenied, match="South"):
        validator.validate(sql, NORTH)


def test_north_manager_in_list_with_outside_region_denied(validator):
    sql = "SELECT * FROM merchants WHERE region IN ('North', 'West')"
    with pytest.raises(AccessDenied, match="West"):
        validator.validate(sql, NORTH)


def test_north_manager_can_filter_on_north(validator):
    validator.validate("SELECT * FROM merchants WHERE region = 'North'", NORTH)


def test_north_manager_cannot_read_settlements(validator):
    with pytest.raises(AccessDenied, match="settlements"):
        validator.validate("SELECT * FROM settlements", NORTH)
