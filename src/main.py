"""
Application entrypoint.

Wires together config, the chosen LLM client (real local Ollama model
or the offline mock), the audit logger, and the FastAPI app.
"""

import uvicorn
from fastapi import FastAPI

from src.api.routes import router
from src.core.config import settings
from src.core.logger import logger
from src.llm.mock_client import MockLLMClient
from src.llm.ollama_client import OllamaClient
from src.security.audit import AuditLogger
from src.services.text2sql_service import TextToSQLService


def build_llm_client():
    if settings.llm_provider == "ollama":
        logger.info(f"Using Ollama at {settings.ollama_host} (model={settings.ollama_model})")
        return OllamaClient(
            host=settings.ollama_host,
            model=settings.ollama_model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    logger.info("Using mock LLM client (offline demo mode)")
    return MockLLMClient()


audit_logger = AuditLogger(settings.audit_log_path)
service = TextToSQLService(
    llm_client=build_llm_client(),
    db_path=settings.db_path,
    audit_logger=audit_logger,
    max_result_rows=settings.max_result_rows,
    default_row_limit=settings.default_row_limit,
    query_timeout_seconds=settings.query_timeout_seconds,
    max_retries=settings.max_retries,
    row_filters=settings.row_filters,
    database_paths=settings.database_paths,
    redaction_enabled=settings.redaction_enabled,
    cache_enabled=settings.cache_enabled,
    cache_ttl_seconds=settings.cache_ttl_seconds,
    rate_limit_per_minute=settings.rate_limit_per_minute,
    conversation_history_limit=settings.conversation_history_limit,
    include_examples_in_prompt=settings.include_examples_in_prompt,
)

app = FastAPI(
    title="Secure Text-to-SQL API",
    description="User -> LLM -> SQL -> Validator -> Safe Execution -> Results",
    version="0.1.0",
)
app.include_router(router)


def main() -> None:
    logger.info(f"Starting server on {settings.api_host}:{settings.api_port}")
    uvicorn.run(
        "src.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
