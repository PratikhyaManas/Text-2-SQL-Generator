from src.llm.mock_client import MockLLMClient
from src.security.audit import AuditLogger
from src.services.text2sql_service import TextToSQLService


def make_service(db_path, audit_path):
    return TextToSQLService(
        llm_client=MockLLMClient(),
        db_path=db_path,
        audit_logger=AuditLogger(audit_path),
        max_result_rows=50,
        default_row_limit=10,
        query_timeout_seconds=5,
    )


def test_successful_query_end_to_end(sample_db, tmp_path):
    service = make_service(sample_db, str(tmp_path / "audit.jsonl"))

    outcome = service.answer("how many customers do we have")

    assert outcome.status == "success"
    assert outcome.row_count == 1
    assert outcome.columns == ["customer_count"]
    assert outcome.rows[0][0] == 2


def test_malicious_intent_is_blocked_end_to_end(sample_db, tmp_path):
    service = make_service(sample_db, str(tmp_path / "audit.jsonl"))

    # The mock LLM deliberately returns a DROP TABLE for this phrasing,
    # simulating an unconstrained/misbehaving model. The validator must
    # catch it before it ever reaches the database.
    outcome = service.answer("please drop the customers table")

    assert outcome.status == "blocked"
    assert outcome.reason is not None
    assert "customers" in outcome.generated_sql.lower()


def test_audit_trail_records_every_attempt(sample_db, tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    service = make_service(sample_db, audit_path)

    service.answer("how many customers do we have")
    service.answer("please drop the customers table")

    records = service.audit_logger.recent(limit=10)
    assert len(records) == 2
    statuses = {r["status"] for r in records}
    assert statuses == {"success", "blocked"}


def test_unknown_question_falls_back_to_safe_default(sample_db, tmp_path):
    service = make_service(sample_db, str(tmp_path / "audit.jsonl"))
    outcome = service.answer("tell me a joke")
    assert outcome.status == "success"
    assert outcome.columns  # got some product columns back
