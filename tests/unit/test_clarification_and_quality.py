from src.security.audit import AuditLogger
from src.services.text2sql_service import TextToSQLService


class FixedSQLLLMClient:
    def __init__(self, sql: str):
        self.sql = sql

    def generate_sql(self, question, schema_text):
        return self.sql


def test_clarify_requests_questions_for_ambiguous_prompt(sample_db, tmp_path):
    service = TextToSQLService(
        llm_client=FixedSQLLLMClient("SELECT * FROM products LIMIT 5;"),
        db_path=sample_db,
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        max_result_rows=10,
        default_row_limit=5,
        query_timeout_seconds=5,
    )

    outcome = service.clarify("best customers")

    assert outcome.status == "needs_clarification"
    assert len(outcome.clarification_questions) >= 1


def test_clarify_ready_for_specific_prompt(sample_db, tmp_path):
    service = TextToSQLService(
        llm_client=FixedSQLLLMClient("SELECT COUNT(*) AS customer_count FROM customers;"),
        db_path=sample_db,
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        max_result_rows=10,
        default_row_limit=5,
        query_timeout_seconds=5,
    )

    outcome = service.clarify("how many customers do we have")

    assert outcome.status == "ready"
    assert outcome.safe_sql is not None


def test_preview_marks_ambiguous_prompt_as_auto_blocked(sample_db, tmp_path):
    service = TextToSQLService(
        llm_client=FixedSQLLLMClient("SELECT * FROM products LIMIT 5;"),
        db_path=sample_db,
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        max_result_rows=10,
        default_row_limit=5,
        query_timeout_seconds=5,
    )

    preview = service.preview("top customers")

    assert preview.status == "blocked"
    assert preview.auto_blocked is True
    assert preview.confidence_band == "low"


def test_answer_contains_result_quality_warnings_and_stats(sample_db, tmp_path):
    service = TextToSQLService(
        llm_client=FixedSQLLLMClient("SELECT name, NULL AS optional_field FROM customers LIMIT 10;"),
        db_path=sample_db,
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        max_result_rows=1,
        default_row_limit=1,
        query_timeout_seconds=5,
    )

    outcome = service.answer("show nullable customer view")

    assert outcome.status == "success"
    assert isinstance(outcome.stats, dict)
    assert outcome.stats.get("displayed_rows") == 1
    assert outcome.stats.get("column_count") == 2
    assert outcome.stats.get("null_ratio_overall") == 0.5
    assert any("null" in item.lower() for item in outcome.result_warnings)
