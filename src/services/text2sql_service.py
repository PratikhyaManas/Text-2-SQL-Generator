"""
Orchestrates the full pipeline described in the article:

    User -> LLM -> SQL -> Validator -> Safe Execution -> Results

Every stage is logged and every attempt (successful, blocked, or
errored) is written to the audit trail, regardless of outcome.
"""

import copy
import re
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
    summary: Optional[str] = None


class RateLimitExceededError(Exception):
    pass


class TextToSQLService:
    def __init__(
        self,
        llm_client,
        db_path: str,
        audit_logger: AuditLogger,
        max_result_rows: int = 100,
        default_row_limit: int = 50,
        query_timeout_seconds: int = 5,
        max_retries: int = 1,
        row_filters: Optional[Dict[str, str]] = None,
        redaction_enabled: bool = True,
        cache_enabled: bool = True,
        cache_ttl_seconds: int = 300,
        rate_limit_per_minute: int = 60,
        conversation_history_limit: int = 3,
        include_examples_in_prompt: bool = True,
    ):
        self.llm_client = llm_client
        self.db_path = db_path
        self.audit_logger = audit_logger
        self.max_result_rows = max_result_rows
        self.default_row_limit = default_row_limit
        self.query_timeout_seconds = query_timeout_seconds
        self.max_retries = max_retries
        self.row_filters = row_filters or {}
        self.redaction_enabled = redaction_enabled
        self.cache_enabled = cache_enabled
        self.cache_ttl_seconds = cache_ttl_seconds
        self.rate_limit_per_minute = rate_limit_per_minute
        self.conversation_history_limit = conversation_history_limit
        self.include_examples_in_prompt = include_examples_in_prompt
        self.metrics = {
            "queries_total": 0,
            "queries_success": 0,
            "queries_blocked": 0,
            "queries_error": 0,
            "llm_calls": 0,
            "validation_failures": 0,
            "execution_failures": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        }
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.rate_limit_history: Dict[str, List[float]] = {}
        self.conversation_history: List[Dict[str, str]] = []

    def get_allowed_schema(self) -> Dict[str, List[str]]:
        return get_schema(self.db_path)

    def _record_metric(self, key: str, value: int = 1) -> None:
        self.metrics[key] = self.metrics.get(key, 0) + value

    def get_metrics(self) -> Dict[str, int]:
        return dict(self.metrics)

    def get_metrics_text(self) -> str:
        lines = ["# HELP text2sql_queries_total Total number of queries handled"]
        lines.append("# TYPE text2sql_queries_total counter")
        for key, value in self.metrics.items():
            lines.append(f"text2sql_{key} {value}")
        return "\n".join(lines)

    def _check_rate_limit(self, user_id: Optional[str]) -> None:
        if not user_id or self.rate_limit_per_minute <= 0:
            return
        now = time.monotonic()
        recent = [timestamp for timestamp in self.rate_limit_history.get(user_id, []) if now - timestamp < 60]
        self.rate_limit_history[user_id] = recent
        if len(recent) >= self.rate_limit_per_minute:
            raise RateLimitExceededError("Rate limit exceeded for this user")
        recent.append(now)
        self.rate_limit_history[user_id] = recent

    def _build_prompt_question(self, question: str) -> str:
        lowered = question.lower()
        if any(token in lowered for token in ["drop", "delete", "update", "insert", "alter", "create", "attach", "pragma"]):
            return question

        if not self.include_examples_in_prompt:
            return question

        context_lines = []
        for item in self.conversation_history[-self.conversation_history_limit:]:
            context_lines.append(f"Q: {item['question']}\nA: {item['answer']}")

        if not context_lines:
            return question

        return (
            f"{question}\n\nRecent conversation context:\n"
            + "\n\n".join(context_lines)
        )

    @staticmethod
    def _redact_value(value: Any) -> Any:
        if not isinstance(value, str):
            return value

        redacted = re.sub(
            r"\b([A-Za-z0-9._%+-]{1,2})[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
            r"\1***@\2",
            value,
        )
        redacted = re.sub(
            r"\b(\d{4})[- ]?(\d{4})[- ]?(\d{4})[- ]?(\d{4})\b",
            r"****-****-****-\4",
            redacted,
        )
        return redacted

    def _redact_rows(self, rows: List[List[Any]]) -> List[List[Any]]:
        if not self.redaction_enabled:
            return rows
        return [[self._redact_value(cell) for cell in row] for row in rows]

    def _summarize_result(self, columns: List[str], rows: List[List[Any]], row_count: int) -> Optional[str]:
        if not rows:
            return "No rows were returned."
        if len(columns) == 1 and row_count == 1:
            return str(rows[0][0])
        if len(rows) == 1 and len(columns) <= 2:
            values = ", ".join(str(value) for value in rows[0])
            return f"The result is {values}."
        return f"Returned {row_count} row(s) with columns {', '.join(columns)}."

    def answer(self, question: str, user_id: Optional[str] = None) -> QueryOutcome:
        self._record_metric("queries_total")
        try:
            self._check_rate_limit(user_id)
        except RateLimitExceededError as exc:
            self._record_metric("queries_blocked")
            return QueryOutcome(question=question, status="blocked", reason=str(exc))

        cache_key = question.strip().lower()
        if self.cache_enabled and cache_key in self.cache:
            cached = self.cache[cache_key]
            if time.monotonic() - cached["timestamp"] < self.cache_ttl_seconds:
                self._record_metric("cache_hits")
                cached_outcome = copy.deepcopy(cached["outcome"])
                cached_outcome.summary = cached_outcome.summary or "Cached answer"
                return cached_outcome

        self._record_metric("cache_misses")
        schema = self.get_allowed_schema()
        schema_text = format_schema_for_prompt(schema)
        prompt_question = self._build_prompt_question(question)

        for attempt in range(self.max_retries + 1):
            logger.info(f"Generating SQL for question: {prompt_question!r}")
            try:
                generated_sql = self.llm_client.generate_sql(prompt_question, schema_text)
                self._record_metric("llm_calls")
            except Exception as e:
                logger.error(f"LLM generation failed: {e}")
                self._record_metric("queries_error")
                self.audit_logger.record(
                    question=question,
                    generated_sql=None,
                    safe_sql=None,
                    status="error",
                    reason=f"LLM generation failed: {e}",
                )
                return QueryOutcome(question=question, status="error", reason=str(e))

            logger.info(f"LLM produced SQL: {generated_sql!r}")

            try:
                result = validate_sql(
                    generated_sql,
                    allowed_schema=schema,
                    max_rows=self.max_result_rows,
                    default_rows=self.default_row_limit,
                    row_filters=self.row_filters,
                )
            except SQLValidationError as e:
                self._record_metric("validation_failures")
                logger.warning(f"Blocked unsafe SQL: {e}")
                if attempt < self.max_retries:
                    prompt_question = f"{question}\nPrevious attempt failed: {e}"
                    continue
                self._record_metric("queries_blocked")
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

            try:
                exec_result = execute_readonly(
                    self.db_path,
                    result.safe_sql,
                    max_rows=self.max_result_rows,
                    timeout_seconds=self.query_timeout_seconds,
                )
            except QueryExecutionError as e:
                self._record_metric("execution_failures")
                logger.error(f"Execution failed: {e}")
                if attempt < self.max_retries:
                    prompt_question = f"{question}\nPrevious attempt failed: {e}"
                    continue
                self._record_metric("queries_error")
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

            redacted_rows = self._redact_rows(exec_result.rows)
            summary = self._summarize_result(exec_result.columns, redacted_rows, exec_result.row_count)
            self._record_metric("queries_success")
            self.audit_logger.record(
                question=question,
                generated_sql=generated_sql,
                safe_sql=result.safe_sql,
                status="success",
                row_count=exec_result.row_count,
                execution_time_ms=exec_result.execution_time_ms,
            )
            self.conversation_history.append({"question": question, "answer": summary or "Done"})
            self.conversation_history = self.conversation_history[-self.conversation_history_limit:]

            outcome = QueryOutcome(
                question=question,
                status="success",
                generated_sql=generated_sql,
                safe_sql=result.safe_sql,
                columns=exec_result.columns,
                rows=redacted_rows,
                row_count=exec_result.row_count,
                truncated=exec_result.truncated,
                execution_time_ms=exec_result.execution_time_ms,
                summary=summary,
            )

            if self.cache_enabled:
                self.cache[cache_key] = {"timestamp": time.monotonic(), "outcome": copy.deepcopy(outcome)}
            return outcome

        return QueryOutcome(question=question, status="error", reason="Unexpected service failure")
