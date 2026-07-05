"""FastAPI routes: thin HTTP layer over the TextToSQLService."""

import asyncio
import json
import threading
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from src.core.config import settings

router = APIRouter()


class QueryRequest(BaseModel):
    question: str
    user_id: str | None = None
    database_name: str | None = None


class QueryApprovalRequest(QueryRequest):
    safe_sql: str


class QueryResponse(BaseModel):
    question: str
    status: str
    generated_sql: str | None = None
    safe_sql: str | None = None
    reason: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    execution_time_ms: float | None = None
    summary: str | None = None
    explanation: str | None = None
    confidence: float | None = None
    result_warnings: list[str] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)


class QueryPreviewResponse(BaseModel):
    question: str
    status: str
    generated_sql: str | None = None
    safe_sql: str | None = None
    reason: str | None = None
    plan_rows: list[list] = Field(default_factory=list)
    plan_warnings: list[str] = Field(default_factory=list)
    confidence: float | None = None
    confidence_band: str | None = None
    auto_blocked: bool = False


class QueryClarifyResponse(BaseModel):
    question: str
    status: str
    clarification_questions: list[str] = Field(default_factory=list)
    safe_sql: str | None = None
    reason: str | None = None


def get_service():
    # Imported lazily so `src.main` controls construction/config wiring
    # and this module stays free of import-time side effects.
    from src.main import service

    return service


@router.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "text2sql-secure",
        "version": "0.1.0",
        "environment": settings.environment,
    }


@router.get("/schema")
async def schema_route():
    service = get_service()
    return service.get_allowed_schema()


@router.get("/databases")
async def databases_route():
    service = get_service()
    return {"status": "ok", "databases": service.get_available_databases()}


@router.post("/query", response_model=QueryResponse)
async def query_route(request: QueryRequest):
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")

    service = get_service()
    outcome = service.answer(request.question, user_id=request.user_id, database_name=request.database_name)

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
        summary=outcome.summary,
        explanation=outcome.explanation,
        confidence=outcome.confidence,
        result_warnings=outcome.result_warnings,
        stats=outcome.stats,
    )


@router.post("/query/approved", response_model=QueryResponse)
async def query_approved_route(request: QueryApprovalRequest):
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")
    if not request.safe_sql or not request.safe_sql.strip():
        raise HTTPException(status_code=400, detail="safe_sql must not be empty")

    service = get_service()
    outcome = service.execute_approved(
        question=request.question,
        safe_sql=request.safe_sql,
        user_id=request.user_id,
        database_name=request.database_name,
    )

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
        summary=outcome.summary,
        explanation=outcome.explanation,
        confidence=outcome.confidence,
        result_warnings=outcome.result_warnings,
        stats=outcome.stats,
    )


@router.post("/query/preview", response_model=QueryPreviewResponse)
async def query_preview_route(request: QueryRequest):
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")

    service = get_service()
    preview = service.preview(request.question, user_id=request.user_id, database_name=request.database_name)

    return QueryPreviewResponse(
        question=preview.question,
        status=preview.status,
        generated_sql=preview.generated_sql,
        safe_sql=preview.safe_sql,
        reason=preview.reason,
        plan_rows=preview.plan_rows,
        plan_warnings=preview.plan_warnings,
        confidence=preview.confidence,
        confidence_band=preview.confidence_band,
        auto_blocked=preview.auto_blocked,
    )


@router.post("/query/clarify", response_model=QueryClarifyResponse)
async def query_clarify_route(request: QueryRequest):
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")

    service = get_service()
    clarification = service.clarify(request.question, user_id=request.user_id, database_name=request.database_name)

    return QueryClarifyResponse(
        question=clarification.question,
        status=clarification.status,
        clarification_questions=clarification.clarification_questions,
        safe_sql=clarification.safe_sql,
        reason=clarification.reason,
    )


@router.post("/query/stream")
async def query_stream_route(request: QueryRequest):
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")

    service = get_service()
    loop = asyncio.get_running_loop()
    events: asyncio.Queue[dict] = asyncio.Queue()

    def emit(stage: str, details: dict):
        payload = {"type": "progress", "stage": stage, "details": details}
        loop.call_soon_threadsafe(events.put_nowait, payload)

    def run_query() -> None:
        try:
            outcome = service.answer(
                request.question,
                user_id=request.user_id,
                database_name=request.database_name,
                progress_callback=emit,
            )
            loop.call_soon_threadsafe(
                events.put_nowait,
                {"type": "result", "data": asdict(outcome)},
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            loop.call_soon_threadsafe(
                events.put_nowait,
                {"type": "error", "message": str(exc)},
            )
        finally:
            loop.call_soon_threadsafe(events.put_nowait, {"type": "done"})

    threading.Thread(target=run_query, daemon=True).start()

    async def generator():
        while True:
            event = await events.get()
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "done":
                break

    return StreamingResponse(generator(), media_type="text/event-stream")


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
    return Response(content=service.get_metrics_text(), media_type="text/plain; version=0.0.4")


@router.get("/history")
async def history_route(limit: int = Query(default=20, ge=1, le=200), user_id: str | None = None):
    service = get_service()
    return {"status": "ok", "history": service.get_history(limit=limit, user_id=user_id)}


@router.get("/history/export")
async def history_export_route(format: str = Query(default="json"), user_id: str | None = None):
    service = get_service()
    payload = service.export_history(format=format, user_id=user_id)
    media_type = "application/json" if format.lower() == "json" else "text/csv"
    return Response(content=payload, media_type=media_type)


@router.get("/ui", response_class=HTMLResponse)
async def ui_route():
    return HTMLResponse(
        """
        <html>
        <head>
            <style>
                body { font-family: Arial, sans-serif; margin: 0; padding: 2rem; background: #f4f7fb; color: #223; }
                .card { background: white; border-radius: 12px; padding: 1rem 1.25rem; box-shadow: 0 8px 24px rgba(0,0,0,0.05); }
                .session-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0.75rem; margin-bottom: 1rem; }
                .session-grid label { display:flex; flex-direction:column; gap:0.25rem; font-size:0.95rem; }
                input, select, button, textarea { padding: 0.7rem; border-radius: 8px; border: 1px solid #c9d3e3; font-size: 0.95rem; }
                button { background: #2563eb; color: white; border: none; cursor: pointer; }
                button.secondary { background: #64748b; }
                .row { display:flex; gap:0.5rem; align-items:flex-end; flex-wrap:wrap; }
                pre { background:#f8fafc; padding:1rem; border:1px solid #e2e8f0; border-radius:8px; white-space:pre-wrap; }
                table { width:100%; border-collapse:collapse; }
                th, td { text-align:left; padding:0.6rem; border-bottom:1px solid #e2e8f0; }
                .muted { color:#64748b; font-size:0.9rem; }
            </style>
        </head>
        <body>
            <div class='card'>
                <h2>Text-to-SQL demo</h2>
                <p class='muted'>Welcome back. Choose a role, pick a database, and start asking questions.</p>
                <div class='session-grid'>
                    <label>Login
                        <select id='user-select'>
                            <option value='demo'>demo — Demo user</option>
                            <option value='analyst'>analyst — Data analyst</option>
                            <option value='viewer'>viewer — Read-only viewer</option>
                        </select>
                    </label>
                    <label>Database
                        <select id='database-select'></select>
                    </label>
                    <label>Session
                        <button id='apply-session' type='button'>Save session</button>
                    </label>
                </div>
                <form id='query-form' class='row'>
                    <textarea name='question' rows='3' style='min-width:60%;flex:1;' placeholder='Ask a question about the data'></textarea>
                    <button type='submit'>Ask</button>
                </form>
                <div style='margin-top:1rem;display:grid;gap:1rem;'>
                    <div><strong>Progress</strong><pre id='progress'></pre></div>
                    <div><strong>Explanation</strong><pre id='explanation'></pre></div>
                    <div><strong>Confidence</strong><pre id='confidence'></pre></div>
                    <div>
                        <strong>Generated SQL</strong>
                        <div class='row' style='margin-top:0.25rem;'>
                            <pre id='sql' style='flex:1;'></pre>
                            <button id='copy-sql' class='secondary' type='button'>Copy SQL</button>
                        </div>
                    </div>
                    <div><strong>Result</strong><pre id='result'></pre></div>
                    <div>
                        <strong>Recent history</strong>
                        <div id='history' class='card' style='margin-top:0.5rem;padding:0.5rem;'></div>
                    </div>
                </div>
            </div>
            <script>
                const storageKey = 'text2sql-session';
                const userSelect = document.getElementById('user-select');
                const databaseSelect = document.getElementById('database-select');

                function loadSession() {
                    const stored = localStorage.getItem(storageKey);
                    if (!stored) return { userId: 'demo', databaseName: 'default' };
                    try { return JSON.parse(stored); } catch { return { userId: 'demo', databaseName: 'default' }; }
                }

                function saveSession(userId, databaseName) {
                    localStorage.setItem(storageKey, JSON.stringify({ userId, databaseName }));
                }

                async function loadDatabases() {
                    const response = await fetch('/databases');
                    const data = await response.json();
                    const options = Object.entries(data.databases || {});
                    databaseSelect.innerHTML = options.map(([name, path]) => `<option value="${name}">${name} (${path})</option>`).join('');
                    const session = loadSession();
                    const selectedName = options.some(([name]) => name === session.databaseName) ? session.databaseName : (options[0] ? options[0][0] : 'default');
                    databaseSelect.value = selectedName;
                    const storedUser = session.userId || 'demo';
                    if ([...userSelect.options].some(option => option.value === storedUser)) {
                        userSelect.value = storedUser;
                    } else {
                        userSelect.value = 'demo';
                    }
                }

                async function refreshHistory() {
                    const userId = userSelect.value.trim();
                    const params = new URLSearchParams({limit: '5'});
                    if (userId) params.set('user_id', userId);
                    const response = await fetch(`/history?${params.toString()}`);
                    const data = await response.json();
                    const container = document.getElementById('history');
                    if (!data.history || data.history.length === 0) {
                        container.innerHTML = '<div style="padding:1rem;">No history yet.</div>';
                        return;
                    }
                    const rows = data.history.map(item => `
                        <tr>
                            <td>${(item.question || '').replace(/</g,'&lt;')}</td>
                            <td>${(item.status || '').replace(/</g,'&lt;')}</td>
                            <td>${(item.summary || '').replace(/</g,'&lt;')}</td>
                        </tr>
                    `).join('');
                    container.innerHTML = `<table><thead><tr><th>Question</th><th>Status</th><th>Summary</th></tr></thead><tbody>${rows}</tbody></table>`;
                }

                document.getElementById('apply-session').onclick = () => {
                    const userId = userSelect.value.trim() || 'demo';
                    const databaseName = databaseSelect.value || 'default';
                    saveSession(userId, databaseName);
                    refreshHistory();
                };

                document.getElementById('query-form').onsubmit = async (event) => {
                    event.preventDefault();
                    const form = new FormData(event.target);
                    const question = form.get('question');
                    const userId = userSelect.value.trim() || 'demo';
                    const databaseName = databaseSelect.value || 'default';
                    saveSession(userId, databaseName);
                    const progressEl = document.getElementById('progress');
                    progressEl.textContent = 'starting...';

                    const response = await fetch('/query/stream', {
                        method:'POST',
                        headers:{'Content-Type':'application/json', 'Accept': 'text/event-stream'},
                        body: JSON.stringify({question, user_id: userId, database_name: databaseName})
                    });

                    const reader = response.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';
                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        buffer += decoder.decode(value, {stream: true});

                        const events = buffer.split('\n\n');
                        buffer = events.pop() || '';
                        for (const event of events) {
                            const line = event.split('\n').find((entry) => entry.startsWith('data: '));
                            if (!line) continue;
                            const payload = JSON.parse(line.slice(6));
                            if (payload.type === 'progress') {
                                const stage = payload.stage || 'working';
                                progressEl.textContent += `\n${stage}`;
                            }
                            if (payload.type === 'result') {
                                const data = payload.data || {};
                                document.getElementById('explanation').textContent = data.explanation || '';
                                document.getElementById('confidence').textContent = data.confidence ?? 'n/a';
                                document.getElementById('sql').textContent = data.safe_sql || data.generated_sql || '';
                                document.getElementById('result').textContent = JSON.stringify({status: data.status, rows: data.rows, rowCount: data.row_count, summary: data.summary}, null, 2);
                            }
                            if (payload.type === 'error') {
                                progressEl.textContent += `\nerror: ${payload.message || 'unexpected error'}`;
                            }
                        }
                    }
                    await refreshHistory();
                };

                document.getElementById('copy-sql').onclick = async () => {
                    const sql = document.getElementById('sql').textContent;
                    if (!sql) return;
                    await navigator.clipboard.writeText(sql);
                    document.getElementById('copy-sql').textContent = 'Copied!';
                    setTimeout(() => document.getElementById('copy-sql').textContent = 'Copy SQL', 1200);
                };

                loadDatabases().then(refreshHistory);
            </script>
        </body>
        </html>
        """
    )


@router.get("/stream")
async def stream_route():
    async def generator():
        yield "data: ready\n\n"
    return StreamingResponse(generator(), media_type="text/event-stream")
