"""
Orchestrates the full pipeline described in the article:

    User -> LLM -> SQL -> Validator -> Safe Execution -> Results

Every stage is logged and every attempt (successful, blocked, or
errored) is written to the audit trail, regardless of outcome.
"""

import copy
import os
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
    explanation: Optional[str] = None
    confidence: Optional[float] = None


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
        database_paths: Optional[Dict[str, str]] = None,
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
            "uptime_seconds": 0,
        }
        self.started_at = time.monotonic()
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.rate_limit_history: Dict[str, List[float]] = {}
        self.database_paths = database_paths or {}
        self.conversation_history: Dict[str, List[Dict[str, str]]] = {}
        self.history: List[Dict[str, Any]] = []
        self.user_histories: Dict[str, List[Dict[str, Any]]] = {}

    def get_allowed_schema(self, db_path: Optional[str] = None) -> Dict[str, List[str]]:
        return get_schema(db_path or self.db_path)

    def get_available_databases(self) -> Dict[str, str]:
        databases = {"default": self.db_path}
        databases.update(self.database_paths)
        return databases

    def _record_metric(self, key: str, value: int = 1) -> None:
        self.metrics[key] = self.metrics.get(key, 0) + value

    def get_metrics(self) -> Dict[str, int]:
        metrics = dict(self.metrics)
        metrics["uptime_seconds"] = int(time.monotonic() - self.started_at)
        return metrics

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

    def _normalize_user_id(self, user_id: Optional[str]) -> str:
        return (user_id or "anonymous").strip() or "anonymous"

    def _resolve_database_path(self, database_name: Optional[str]) -> str:
        if not database_name:
            return self.db_path

        if database_name in self.database_paths:
            return self.database_paths[database_name]

        if os.path.exists(database_name):
            return database_name

        candidate = os.path.abspath(os.path.join(os.path.dirname(self.db_path), f"{database_name}.db"))
        if os.path.exists(candidate):
            return candidate

        return self.db_path

    def _get_user_conversation_history(self, user_id: Optional[str]) -> List[Dict[str, str]]:
        normalized_user = self._normalize_user_id(user_id)
        if normalized_user not in self.conversation_history:
            self.conversation_history[normalized_user] = []
        return self.conversation_history[normalized_user]

    def _build_prompt_question(self, question: str, user_id: Optional[str] = None) -> str:
        lowered = question.lower()
        if any(token in lowered for token in ["drop", "delete", "update", "insert", "alter", "create", "attach", "pragma"]):
            return question

        if not self.include_examples_in_prompt:
            return question

        context_lines = []
        for item in self._get_user_conversation_history(user_id)[-self.conversation_history_limit:]:
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

    def _build_explanation(self, status: str, generated_sql: Optional[str], safe_sql: Optional[str], reason: Optional[str]) -> Optional[str]:
        if status == "success":
            return (
                f"The query was validated and executed safely. "
                f"Generated SQL: {safe_sql or generated_sql}"
            )
        if status == "blocked":
            return f"The query was blocked by the validator: {reason}"
        return f"The query could not be completed: {reason}"

    def _build_confidence(self, status: str, generated_sql: Optional[str], safe_sql: Optional[str]) -> float:
        if status != "success":
            return 0.0
        if not generated_sql:
            return 0.1
        if safe_sql and safe_sql.lower() != generated_sql.lower():
            return 0.9
        return 0.8

    def get_history(self, limit: int = 20, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if user_id is not None:
            normalized_user = self._normalize_user_id(user_id)
            return list(self.user_histories.get(normalized_user, [])[-limit:])
        return list(self.history[-limit:])

    def export_history(self, format: str = "json", user_id: Optional[str] = None) -> str:
        history = self.get_history(limit=1000, user_id=user_id)
        if format.lower() == "csv":
            headers = ["question", "status", "safe_sql", "summary", "explanation", "confidence"]
            rows = []
            for item in history:
                rows.append(
                    [
                        item.get("question", ""),
                        item.get("status", ""),
                        item.get("safe_sql", ""),
                        item.get("summary", ""),
                        item.get("explanation", ""),
                        item.get("confidence", ""),
                    ]
                )
            import csv
            import io

            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(headers)
            writer.writerows(rows)
            return output.getvalue()

        import json

        return json.dumps(history, indent=2, default=str)

    def answer(self, question: str, user_id: Optional[str] = None, database_name: Optional[str] = None) -> QueryOutcome:
        self._record_metric("queries_total")
        normalized_user = self._normalize_user_id(user_id)
        resolved_db_path = self._resolve_database_path(database_name)
        try:
            self._check_rate_limit(normalized_user)
        except RateLimitExceededError as exc:
            self._record_metric("queries_blocked")
            return QueryOutcome(question=question, status="blocked", reason=str(exc))

        cache_key = f"{question.strip().lower()}::{normalized_user}::{database_name or resolved_db_path}"
        if self.cache_enabled and cache_key in self.cache:
            cached = self.cache[cache_key]
            if time.monotonic() - cached["timestamp"] < self.cache_ttl_seconds:
                self._record_metric("cache_hits")
                cached_outcome = copy.deepcopy(cached["outcome"])
                cached_outcome.summary = cached_outcome.summary or "Cached answer"
                return cached_outcome

        self._record_metric("cache_misses")
        schema = self.get_allowed_schema(resolved_db_path)
        schema_text = format_schema_for_prompt(schema)
        prompt_question = self._build_prompt_question(question, user_id=normalized_user)

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
                    user_id=normalized_user,
                    database_name=database_name,
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
                    user_id=normalized_user,
                    database_name=database_name,
                )
                explanation = self._build_explanation("blocked", generated_sql, None, str(e))
                confidence = self._build_confidence("blocked", generated_sql, None)
                entry = {
                    "question": question,
                    "status": "blocked",
                    "generated_sql": generated_sql,
                    "safe_sql": None,
                    "summary": None,
                    "explanation": explanation,
                    "confidence": confidence,
                    "user_id": normalized_user,
                    "database_name": database_name,
                }
                self.history.append(entry)
                self.user_histories.setdefault(normalized_user, []).append(entry)
                return QueryOutcome(
                    question=question,
                    status="blocked",
                    generated_sql=generated_sql,
                    reason=str(e),
                    explanation=explanation,
                    confidence=confidence,
                )

            try:
                exec_result = execute_readonly(
                    resolved_db_path,
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
                    user_id=normalized_user,
                    database_name=database_name,
                )
                explanation = self._build_explanation("error", generated_sql, result.safe_sql, str(e))
                confidence = self._build_confidence("error", generated_sql, result.safe_sql)
                entry = {
                    "question": question,
                    "status": "error",
                    "generated_sql": generated_sql,
                    "safe_sql": result.safe_sql,
                    "summary": None,
                    "explanation": explanation,
                    "confidence": confidence,
                    "user_id": normalized_user,
                    "database_name": database_name,
                }
                self.history.append(entry)
                self.user_histories.setdefault(normalized_user, []).append(entry)
                return QueryOutcome(
                    question=question,
                    status="error",
                    generated_sql=generated_sql,
                    safe_sql=result.safe_sql,
                    reason=str(e),
                    explanation=explanation,
                    confidence=confidence,
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
                user_id=normalized_user,
                database_name=database_name,
            )
            user_conversation = self._get_user_conversation_history(normalized_user)
            user_conversation.append({"question": question, "answer": summary or "Done"})
            self.conversation_history[normalized_user] = user_conversation[-self.conversation_history_limit:]

            explanation = self._build_explanation("success", generated_sql, result.safe_sql, None)
            confidence = self._build_confidence("success", generated_sql, result.safe_sql)
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
                explanation=explanation,
                confidence=confidence,
            )
            entry = {
                "question": question,
                "status": outcome.status,
                "generated_sql": outcome.generated_sql,
                "safe_sql": outcome.safe_sql,
                "summary": outcome.summary,
                "explanation": outcome.explanation,
                "confidence": outcome.confidence,
                "user_id": normalized_user,
                "database_name": database_name,
            }
            self.history.append(entry)
            self.user_histories.setdefault(normalized_user, []).append(entry)

            if self.cache_enabled:
                self.cache[cache_key] = {"timestamp": time.monotonic(), "outcome": copy.deepcopy(outcome)}
            return outcome

        return QueryOutcome(question=question, status="error", reason="Unexpected service failure")
