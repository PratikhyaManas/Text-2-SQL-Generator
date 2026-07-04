"""FastAPI routes: thin HTTP layer over the TextToSQLService."""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
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


@router.get("/metrics")
async def metrics_route():
    service = get_service()
    return {"status": "ok", "metrics": service.get_metrics()}


@router.get("/metrics-text")
async def metrics_text_route():
    service = get_service()
    return {"status": "ok", "content": service.get_metrics_text()}


@router.get("/ui", response_class=HTMLResponse)
async def ui_route():
    return HTMLResponse(
        """
        <html><body style='font-family:Arial,sans-serif;padding:2rem;'>
            <h2>Text-to-SQL demo</h2>
            <form id='query-form'>
                <input name='question' style='width:70%;padding:0.5rem;' placeholder='Ask a question about the data' />
                <button type='submit'>Ask</button>
            </form>
            <pre id='result' style='background:#f4f4f4;padding:1rem;'></pre>
            <script>
                document.getElementById('query-form').onsubmit = async (event) => {
                    event.preventDefault();
                    const form = new FormData(event.target);
                    const question = form.get('question');
                    const response = await fetch('/query', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({question})});
                    const data = await response.json();
                    document.getElementById('result').textContent = JSON.stringify(data, null, 2);
                };
            </script>
        </body></html>
        """
    )


@router.get("/stream")
async def stream_route():
    async def generator():
        yield "data: ready\n\n"
    return StreamingResponse(generator(), media_type="text/event-stream")
