import sqlite3

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


def test_database_selection_uses_requested_database(sample_db, tmp_path):
    alt_db = str(tmp_path / "alt.db")
    conn = sqlite3.connect(alt_db)
    conn.execute("CREATE TABLE inventory (item_id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute("INSERT INTO inventory VALUES (1, 'Keyboard')")
    conn.commit()
    conn.close()

    class CustomLLMClient(MockLLMClient):
        def generate_sql(self, question, schema_text):
            return "SELECT * FROM inventory LIMIT 10;"

    service = TextToSQLService(
        llm_client=CustomLLMClient(),
        db_path=sample_db,
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        max_result_rows=50,
        default_row_limit=10,
        query_timeout_seconds=5,
    )

    outcome = service.answer("show inventory", database_name="alt", user_id="demo")

    assert outcome.status == "success"
    assert outcome.rows[0][1] == "Keyboard"


def test_available_databases_include_default_and_configured_options(sample_db, tmp_path):
    alt_path = str(tmp_path / "alt.db")
    service = TextToSQLService(
        llm_client=MockLLMClient(),
        db_path=sample_db,
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        max_result_rows=50,
        default_row_limit=10,
        query_timeout_seconds=5,
        database_paths={"alt": alt_path},
    )

    databases = service.get_available_databases()
    assert databases["default"] == sample_db
    assert databases["alt"] == alt_path


def test_conversation_context_is_kept_per_user(sample_db, tmp_path):
    class RecordingLLMClient(MockLLMClient):
        def __init__(self):
            self.prompts = []

        def generate_sql(self, question, schema_text):
            self.prompts.append(question)
            return "SELECT customer_id FROM customers LIMIT 1;"

    llm_client = RecordingLLMClient()
    service = TextToSQLService(
        llm_client=llm_client,
        db_path=sample_db,
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        max_result_rows=50,
        default_row_limit=10,
        query_timeout_seconds=5,
    )

    service.answer("show me customers", user_id="alice")
    service.answer("show me more", user_id="alice")

    assert len(llm_client.prompts) == 2
    assert "Recent conversation context" in llm_client.prompts[1]
    assert "show me customers" in llm_client.prompts[1]
