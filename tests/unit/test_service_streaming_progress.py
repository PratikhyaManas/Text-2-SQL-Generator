from src.llm.mock_client import MockLLMClient
from src.security.audit import AuditLogger
from src.services.text2sql_service import TextToSQLService


def build_service(sample_db, tmp_path):
    return TextToSQLService(
        llm_client=MockLLMClient(),
        db_path=sample_db,
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        max_result_rows=10,
        default_row_limit=5,
        query_timeout_seconds=5,
    )


def test_progress_callback_emits_expected_stages(sample_db, tmp_path):
    service = build_service(sample_db, tmp_path)
    stages = []

    def on_progress(stage, details):
        stages.append(stage)

    outcome = service.answer(
        "how many customers do we have",
        user_id="demo",
        progress_callback=on_progress,
    )

    assert outcome.status == "success"
    assert "generating" in stages
    assert "validating" in stages
    assert "executing" in stages
    assert "formatting" in stages
    assert stages[-1] == "done"


def test_resolve_database_target_allows_urls(sample_db, tmp_path):
    service = build_service(sample_db, tmp_path)

    target = service._resolve_database_path("postgresql://user:pass@localhost:5432/appdb")

    assert target == "postgresql://user:pass@localhost:5432/appdb"
