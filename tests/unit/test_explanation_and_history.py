from src.llm.mock_client import MockLLMClient
from src.security.audit import AuditLogger
from src.services.text2sql_service import TextToSQLService


def test_answer_returns_explanation_and_confidence(sample_db, tmp_path):
    service = TextToSQLService(
        llm_client=MockLLMClient(),
        db_path=sample_db,
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        max_result_rows=10,
        default_row_limit=5,
        query_timeout_seconds=5,
    )

    outcome = service.answer("how many customers do we have")

    assert outcome.status == "success"
    assert outcome.explanation is not None
    assert 0.0 <= outcome.confidence <= 1.0


def test_history_can_be_exported_to_json_and_csv(sample_db, tmp_path):
    service = TextToSQLService(
        llm_client=MockLLMClient(),
        db_path=sample_db,
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        max_result_rows=10,
        default_row_limit=5,
        query_timeout_seconds=5,
    )

    service.answer("how many customers do we have")

    history = service.get_history(limit=5)
    assert len(history) == 1
    assert history[0]["status"] == "success"

    json_export = service.export_history("json")
    assert '"question"' in json_export

    csv_export = service.export_history("csv")
    assert "question,status" in csv_export
