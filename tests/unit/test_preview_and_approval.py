from src.llm.mock_client import MockLLMClient
from src.security.audit import AuditLogger
from src.services.text2sql_service import TextToSQLService


class FailingLLM:
    def generate_sql(self, question, schema_text):
        raise RuntimeError("LLM should not be called")


def build_service(sample_db, tmp_path, llm_client=None):
    return TextToSQLService(
        llm_client=llm_client or MockLLMClient(),
        db_path=sample_db,
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        max_result_rows=20,
        default_row_limit=10,
        query_timeout_seconds=5,
    )


def test_preview_returns_ready_with_plan(sample_db, tmp_path):
    service = build_service(sample_db, tmp_path)

    preview = service.preview("how many customers do we have", user_id="demo")

    assert preview.status == "ready"
    assert preview.safe_sql is not None
    assert isinstance(preview.plan_rows, list)
    assert isinstance(preview.plan_warnings, list)


def test_execute_approved_runs_without_llm(sample_db, tmp_path):
    service = build_service(sample_db, tmp_path, llm_client=FailingLLM())

    outcome = service.execute_approved(
        question="approved question",
        safe_sql="SELECT COUNT(*) AS customer_count FROM customers LIMIT 5",
        user_id="demo",
    )

    assert outcome.status == "success"
    assert outcome.columns == ["customer_count"]
    assert outcome.rows[0][0] == 2
