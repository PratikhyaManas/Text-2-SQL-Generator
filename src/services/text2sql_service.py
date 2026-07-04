"""
Orchestrates the full pipeline described in the article:

    User -> LLM -> SQL -> Validator -> Safe Execution -> Results

Every stage is logged and every attempt (successful, blocked, or
errored) is written to the audit trail, regardless of outcome.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.core.logger import logger
from src.db.executor import QueryExecutionError, execute_readonly
from src.db.schema import format_schema_for_prompt, get_schema
from src.security.audit import AuditLogger
from src.security.validator import SQLValidationError, validate_sql


@dataclass
class QueryOutcome:
    question: str
    status: str  # "success" | "blocked" | "error"
    generated_sql: Optional[str] = None
    safe_sql: Optional[str] = None
    reason: Optional[str] = None
    columns: List[str] = field(default_factory=list)
    rows: List[List[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    execution_time_ms: Optional[float] = None


class TextToSQLService:
    def __init__(
        self,
        llm_client,
        db_path: str,
        audit_logger: AuditLogger,
        max_result_rows: int = 100,
        default_row_limit: int = 50,
        query_timeout_seconds: int = 5,
    ):
        self.llm_client = llm_client
        self.db_path = db_path
        self.audit_logger = audit_logger
        self.max_result_rows = max_result_rows
        self.default_row_limit = default_row_limit
        self.query_timeout_seconds = query_timeout_seconds

    def get_allowed_schema(self) -> Dict[str, List[str]]:
        return get_schema(self.db_path)

    def answer(self, question: str) -> QueryOutcome:
        schema = self.get_allowed_schema()
        schema_text = format_schema_for_prompt(schema)

        # --- Stage 1: LLM generates candidate SQL -------------------------
        logger.info(f"Generating SQL for question: {question!r}")
        try:
            generated_sql = self.llm_client.generate_sql(question, schema_text)
        except Exception as e:
            logger.error(f"LLM generation failed: {e}")
            self.audit_logger.record(
                question=question,
                generated_sql=None,
                safe_sql=None,
                status="error",
                reason=f"LLM generation failed: {e}",
            )
            return QueryOutcome(question=question, status="error", reason=str(e))

        logger.info(f"LLM produced SQL: {generated_sql!r}")

        # --- Stage 2: Validate before ever touching the database ----------
        try:
            result = validate_sql(
                generated_sql,
                allowed_schema=schema,
                max_rows=self.max_result_rows,
                default_rows=self.default_row_limit,
            )
        except SQLValidationError as e:
            logger.warning(f"Blocked unsafe SQL: {e}")
            self.audit_logger.record(
                question=question,
                generated_sql=generated_sql,
                safe_sql=None,
                status="blocked",
                reason=str(e),
            )
            return QueryOutcome(
                question=question,
                status="blocked",
                generated_sql=generated_sql,
                reason=str(e),
            )

        # --- Stage 3: Safe, read-only execution ----------------------------
        try:
            exec_result = execute_readonly(
                self.db_path,
                result.safe_sql,
                max_rows=self.max_result_rows,
                timeout_seconds=self.query_timeout_seconds,
            )
        except QueryExecutionError as e:
            logger.error(f"Execution failed: {e}")
            self.audit_logger.record(
                question=question,
                generated_sql=generated_sql,
                safe_sql=result.safe_sql,
                status="error",
                reason=str(e),
            )
            return QueryOutcome(
                question=question,
                status="error",
                generated_sql=generated_sql,
                safe_sql=result.safe_sql,
                reason=str(e),
            )

        # --- Stage 4: Audit + return ----------------------------------------
        self.audit_logger.record(
            question=question,
            generated_sql=generated_sql,
            safe_sql=result.safe_sql,
            status="success",
            row_count=exec_result.row_count,
            execution_time_ms=exec_result.execution_time_ms,
        )

        return QueryOutcome(
            question=question,
            status="success",
            generated_sql=generated_sql,
            safe_sql=result.safe_sql,
            columns=exec_result.columns,
            rows=exec_result.rows,
            row_count=exec_result.row_count,
            truncated=exec_result.truncated,
            execution_time_ms=exec_result.execution_time_ms,
        )
