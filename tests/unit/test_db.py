import pytest

from src.db.executor import QueryExecutionError, execute_readonly
from src.db.schema import format_schema_for_prompt, get_schema


def test_get_schema_returns_tables_and_columns(sample_db):
    schema = get_schema(sample_db)
    assert set(schema.keys()) == {"customers", "products"}
    assert "email" in schema["customers"]
    assert "price" in schema["products"]


def test_format_schema_for_prompt(sample_db):
    schema = get_schema(sample_db)
    text = format_schema_for_prompt(schema)
    assert "customers(" in text
    assert "products(" in text


def test_execute_readonly_returns_rows(sample_db):
    result = execute_readonly(sample_db, "SELECT * FROM customers")
    assert result.row_count == 2
    assert "customer_id" in result.columns
    assert not result.truncated


def test_execute_readonly_respects_max_rows(sample_db):
    result = execute_readonly(sample_db, "SELECT * FROM customers", max_rows=1)
    assert result.row_count == 1
    assert result.truncated is True


def test_execute_readonly_rejects_writes_at_the_db_layer(sample_db):
    # Even if something upstream failed to block a write statement, the
    # read-only connection itself must refuse to execute it.
    with pytest.raises(QueryExecutionError):
        execute_readonly(sample_db, "DELETE FROM customers")
