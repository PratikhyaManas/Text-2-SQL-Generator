"""FastAPI routes: thin HTTP layer over the TextToSQLService."""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.core.logger import logger

router = APIRouter()


class QueryRequest(BaseModel):
    question: str


class QueryResponse(BaseModel):
    question: str
    status: str
    generated_sql: str | None = None
    safe_sql: str | None = None
    reason: str | None = None
    columns: list[str] = []
    rows: list[list] = []
    row_count: int = 0
    truncated: bool = False
    execution_time_ms: float | None = None


def get_service():
    # Imported lazily so `src.main` controls construction/config wiring
    # and this module stays free of import-time side effects.
    from src.main import service

    return service


@router.get("/health")
async def health_check():
    return {"status": "ok"}


@router.get("/schema")
async def schema_route():
    service = get_service()
    return service.get_allowed_schema()


@router.post("/query", response_model=QueryResponse)
async def query_route(request: QueryRequest):
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")

    service = get_service()
    outcome = service.answer(request.question)

    return QueryResponse(
        question=outcome.question,
        status=outcome.status,
        generated_sql=outcome.generated_sql,
        safe_sql=outcome.safe_sql,
        reason=outcome.reason,
        columns=outcome.columns,
        rows=outcome.rows,
        row_count=outcome.row_count,
        truncated=outcome.truncated,
        execution_time_ms=outcome.execution_time_ms,
    )


@router.get("/audit")
async def audit_route(limit: int = Query(default=20, ge=1, le=200)):
    service = get_service()
    return service.audit_logger.recent(limit=limit)
