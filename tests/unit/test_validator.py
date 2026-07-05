import pytest

from src.security.validator import SQLValidationError, validate_sql

SCHEMA = {
    "customers": ["customer_id", "name", "email", "signup_date"],
    "products": ["product_id", "name", "category", "price"],
    "orders": ["order_id", "customer_id", "order_date"],
    "order_items": ["order_item_id", "order_id", "product_id", "quantity"],
}


def test_benign_select_passes():
    result = validate_sql("SELECT name, email FROM customers", SCHEMA)
    assert "customers" in result.tables_used
    assert "SELECT" in result.safe_sql.upper()


def test_missing_limit_gets_default_injected():
    result = validate_sql("SELECT * FROM customers", SCHEMA, max_rows=100, default_rows=25)
    assert result.limit_applied == 25
    assert "LIMIT" in result.safe_sql.upper()


def test_oversized_limit_gets_clamped():
    result = validate_sql(
        "SELECT * FROM customers LIMIT 999999", SCHEMA, max_rows=100, default_rows=25
    )
    assert result.limit_applied == 100


def test_reasonable_limit_is_preserved():
    result = validate_sql(
        "SELECT * FROM customers LIMIT 10", SCHEMA, max_rows=100, default_rows=25
    )
    assert result.limit_applied == 10


@pytest.mark.parametrize(
    "malicious_sql",
    [
        "DROP TABLE customers",
        "DELETE FROM customers WHERE 1=1",
        "UPDATE customers SET email='hacked@evil.com'",
        "INSERT INTO customers VALUES (99, 'x', 'x', 'x')",
        "ALTER TABLE customers ADD COLUMN hacked TEXT",
        "CREATE TABLE evil (id INT)",
        "ATTACH DATABASE '/etc/passwd' AS pwn",
        "PRAGMA table_info(customers)",
    ],
)
def test_non_select_statements_are_blocked(malicious_sql):
    with pytest.raises(SQLValidationError):
        validate_sql(malicious_sql, SCHEMA)


def test_stacked_queries_are_blocked():
    with pytest.raises(SQLValidationError):
        validate_sql(
            "SELECT * FROM customers; DROP TABLE customers;", SCHEMA
        )


def test_tables_outside_allowed_schema_are_blocked():
    with pytest.raises(SQLValidationError):
        validate_sql("SELECT * FROM sqlite_master", SCHEMA)

    with pytest.raises(SQLValidationError):
        validate_sql("SELECT * FROM secret_admin_table", SCHEMA)


def test_join_across_allowed_tables_is_fine():
    result = validate_sql(
        "SELECT o.order_id, c.name FROM orders o "
        "JOIN customers c ON c.customer_id = o.customer_id",
        SCHEMA,
    )
    assert set(result.tables_used) == {"orders", "customers"}


def test_join_pulling_in_a_disallowed_table_is_blocked():
    with pytest.raises(SQLValidationError):
        validate_sql(
            "SELECT o.order_id, u.password FROM orders o "
            "JOIN users u ON u.id = o.customer_id",
            SCHEMA,
        )


def test_empty_query_is_blocked():
    with pytest.raises(SQLValidationError):
        validate_sql("", SCHEMA)
    with pytest.raises(SQLValidationError):
        validate_sql("   ", SCHEMA)


def test_comment_based_injection_attempt_is_blocked_or_stripped():
    # Even if a comment-smuggling attempt parses, the re-serialized SQL
    # must never carry the comment through, and any attempt to hide a
    # second statement behind it must be rejected outright.
    with pytest.raises(SQLValidationError):
        validate_sql(
            "SELECT * FROM customers; -- DROP TABLE customers",
            SCHEMA,
        )


def test_column_level_allow_list_blocks_disallowed_columns():
    schema = {"customers": ["name"]}
    with pytest.raises(SQLValidationError):
        validate_sql("SELECT email FROM customers", schema)


def test_row_filter_is_injected_when_requested():
    result = validate_sql(
        "SELECT name FROM customers",
        {"customers": ["name"]},
        row_filters={"customers": "tenant_id = 1"},
    )
    assert "WHERE" in result.safe_sql.upper()
    assert "TENANT_ID = 1" in result.safe_sql.upper()


def test_select_alias_in_order_by_is_allowed():
    result = validate_sql(
        "SELECT p.name, SUM(oi.quantity) AS total_sold "
        "FROM order_items oi "
        "JOIN products p ON p.product_id = oi.product_id "
        "GROUP BY p.name "
        "ORDER BY total_sold DESC LIMIT 5",
        SCHEMA,
    )
    assert "ORDER BY TOTAL_SOLD DESC" in result.safe_sql.upper()
