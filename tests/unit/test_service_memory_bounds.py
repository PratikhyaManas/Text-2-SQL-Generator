from src.llm.mock_client import MockLLMClient
from src.security.audit import AuditLogger
from src.services.text2sql_service import TextToSQLService


def build_service(sample_db, tmp_path, **kwargs):
    return TextToSQLService(
        llm_client=MockLLMClient(),
        db_path=sample_db,
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        max_result_rows=10,
        default_row_limit=5,
        query_timeout_seconds=5,
        **kwargs,
    )


def test_cache_entries_are_bounded(sample_db, tmp_path):
    service = build_service(sample_db, tmp_path, cache_enabled=True, max_cache_entries=1)

    service.answer("show all products")
    service.answer("show products again")

    assert len(service.cache) == 1


def test_history_is_bounded(sample_db, tmp_path):
    service = build_service(sample_db, tmp_path, max_history_entries=2)

    service.answer("show all products")
    service.answer("show products again")
    service.answer("show products one more time")

    assert len(service.history) == 2
    assert len(service.get_history(limit=10)) == 2


def test_rate_limit_user_tracking_is_bounded(sample_db, tmp_path):
    service = build_service(sample_db, tmp_path, rate_limit_per_minute=100, max_rate_limit_users=1)

    service.answer("show products", user_id="user_a")
    service.answer("show products", user_id="user_b")

    assert len(service.rate_limit_history) == 1
    assert "user_b" in service.rate_limit_history
