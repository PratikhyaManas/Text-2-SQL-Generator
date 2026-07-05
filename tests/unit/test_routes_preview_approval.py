from fastapi.testclient import TestClient

from src.api import routes
from src.main import app
from src.llm.mock_client import MockLLMClient
from src.security.audit import AuditLogger
from src.services.text2sql_service import TextToSQLService


def build_service(sample_db, tmp_path):
    return TextToSQLService(
        llm_client=MockLLMClient(),
        db_path=sample_db,
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        max_result_rows=20,
        default_row_limit=10,
        query_timeout_seconds=5,
    )


def test_query_preview_route_returns_ready(sample_db, tmp_path, monkeypatch):
    service = build_service(sample_db, tmp_path)
    monkeypatch.setattr(routes, "get_service", lambda: service)

    client = TestClient(app)
    response = client.post("/query/preview", json={"question": "how many customers do we have"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["safe_sql"] is not None


def test_query_approved_route_executes_provided_sql(sample_db, tmp_path, monkeypatch):
    service = build_service(sample_db, tmp_path)
    monkeypatch.setattr(routes, "get_service", lambda: service)

    client = TestClient(app)
    response = client.post(
        "/query/approved",
        json={
            "question": "approved execution",
            "safe_sql": "SELECT COUNT(*) AS customer_count FROM customers LIMIT 5",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["rows"][0][0] == 2
