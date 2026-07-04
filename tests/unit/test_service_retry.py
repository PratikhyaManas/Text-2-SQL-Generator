from src.services.text2sql_service import TextToSQLService


class FakeAuditLogger:
    def __init__(self):
        self.records = []

    def record(self, **kwargs):
        self.records.append(kwargs)


class FakeLLMClient:
    def __init__(self):
        self.calls = 0

    def generate_sql(self, question, schema_text):
        self.calls += 1
        if self.calls == 1:
            return "SELECT bad_column FROM customers"
        return "SELECT name FROM customers"


def test_service_retries_once_after_validation_failure(sample_db):
    llm_client = FakeLLMClient()
    service = TextToSQLService(
        llm_client=llm_client,
        db_path=sample_db,
        audit_logger=FakeAuditLogger(),
        max_result_rows=10,
        default_row_limit=5,
        query_timeout_seconds=5,
    )

    outcome = service.answer("show customers")

    assert outcome.status == "success"
    assert llm_client.calls == 2
    assert outcome.safe_sql is not None
