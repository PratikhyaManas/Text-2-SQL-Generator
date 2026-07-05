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
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.core.logger import logger
from src.db.executor import QueryExecutionError, execute_readonly, explain_readonly
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
    result_warnings: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)


@dataclass
class QueryPreview:
    question: str
    status: str  # "ready" | "blocked" | "error"
    generated_sql: Optional[str] = None
    safe_sql: Optional[str] = None
    reason: Optional[str] = None
    plan_rows: List[List[Any]] = field(default_factory=list)
    plan_warnings: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    confidence_band: Optional[str] = None
    auto_blocked: bool = False


@dataclass
class ClarificationOutcome:
    question: str
    status: str  # "needs_clarification" | "ready"
    clarification_questions: List[str] = field(default_factory=list)
    safe_sql: Optional[str] = None
    reason: Optional[str] = None


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
        max_cache_entries: int = 512,
        max_history_entries: int = 1000,
        max_rate_limit_users: int = 1000,
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
        self.max_cache_entries = max_cache_entries
        self.max_history_entries = max_history_entries
        self.max_rate_limit_users = max_rate_limit_users
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
        self.cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self.rate_limit_history: Dict[str, List[float]] = {}
        self.database_paths = database_paths or {}
        self.conversation_history: Dict[str, List[Dict[str, str]]] = {}
        self.history: List[Dict[str, Any]] = []
        self.user_histories: Dict[str, List[Dict[str, Any]]] = {}
        self._schema_cache: Dict[str, Dict[str, Any]] = {}

    def get_allowed_schema(self, db_path: Optional[str] = None) -> Dict[str, List[str]]:
        resolved_db_path = db_path or self.db_path
        try:
            mtime = os.path.getmtime(resolved_db_path)
        except OSError:
            mtime = None

        cached = self._schema_cache.get(resolved_db_path)
        if cached and cached.get("mtime") == mtime:
            return cached["schema"]

        schema = get_schema(resolved_db_path)
        self._schema_cache[resolved_db_path] = {"mtime": mtime, "schema": schema}
        return schema

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

        if user_id not in self.rate_limit_history and self.max_rate_limit_users > 0:
            while len(self.rate_limit_history) >= self.max_rate_limit_users:
                oldest_user = next(iter(self.rate_limit_history), None)
                if oldest_user is None:
                    break
                self.rate_limit_history.pop(oldest_user, None)

        now = time.monotonic()
        recent = [timestamp for timestamp in self.rate_limit_history.get(user_id, []) if now - timestamp < 60]
        self.rate_limit_history[user_id] = recent
        if len(recent) >= self.rate_limit_per_minute:
            raise RateLimitExceededError("Rate limit exceeded for this user")
        recent.append(now)
        self.rate_limit_history[user_id] = recent

    def _append_history_entry(self, user_id: str, entry: Dict[str, Any]) -> None:
        self.history.append(entry)
        if self.max_history_entries > 0 and len(self.history) > self.max_history_entries:
            self.history = self.history[-self.max_history_entries:]

        user_history = self.user_histories.setdefault(user_id, [])
        user_history.append(entry)
        if self.max_history_entries > 0 and len(user_history) > self.max_history_entries:
            self.user_histories[user_id] = user_history[-self.max_history_entries:]

    def _cleanup_expired_cache(self, now: float) -> None:
        if not self.cache_enabled:
            return

        expired_keys = [
            key
            for key, value in self.cache.items()
            if now - value["timestamp"] >= self.cache_ttl_seconds
        ]
        for key in expired_keys:
            self.cache.pop(key, None)

    def _upsert_cache(self, cache_key: str, outcome: QueryOutcome, now: float) -> None:
        if not self.cache_enabled:
            return

        if cache_key in self.cache:
            self.cache.pop(cache_key, None)

        self.cache[cache_key] = {"timestamp": now, "outcome": copy.deepcopy(outcome)}

        if self.max_cache_entries > 0:
            while len(self.cache) > self.max_cache_entries:
                self.cache.popitem(last=False)

    def _normalize_user_id(self, user_id: Optional[str]) -> str:
        return (user_id or "anonymous").strip() or "anonymous"

    def _resolve_database_path(self, database_name: Optional[str]) -> str:
        if not database_name:
            return self.db_path

        if "://" in database_name:
            return database_name

        if database_name in self.database_paths:
            return self.database_paths[database_name]

        if os.path.exists(database_name):
            return database_name

        candidate = os.path.abspath(os.path.join(os.path.dirname(self.db_path), f"{database_name}.db"))
        if os.path.exists(candidate):
            return candidate

        return self.db_path

    @staticmethod
    def _emit_progress(
        progress_callback: Optional[Callable[[str, Dict[str, Any]], None]],
        stage: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback(stage, details or {})

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

    @staticmethod
    def _build_result_quality(columns: List[str], rows: List[List[Any]], row_count: int, truncated: bool) -> tuple[List[str], Dict[str, Any]]:
        warnings: List[str] = []
        column_count = len(columns)
        displayed_rows = len(rows)

        if row_count == 0:
            warnings.append("No rows were returned for this query.")
        if truncated:
            warnings.append("Results were truncated to the configured row limit.")
        if column_count >= 12:
            warnings.append("Result has many columns. Consider selecting only key fields.")

        total_cells = max(displayed_rows * max(column_count, 1), 1)
        null_cells = 0
        for row in rows:
            for value in row[:column_count]:
                if value is None:
                    null_cells += 1

        null_ratio_overall = round(null_cells / total_cells, 3)
        if displayed_rows > 0 and null_ratio_overall >= 0.5:
            warnings.append("Result contains a high share of null values.")

        stats: Dict[str, Any] = {
            "returned_rows": row_count,
            "displayed_rows": displayed_rows,
            "column_count": column_count,
            "null_ratio_overall": null_ratio_overall,
        }
        return warnings, stats

    @staticmethod
    def _confidence_band(confidence: Optional[float]) -> Optional[str]:
        if confidence is None:
            return None
        if confidence >= 0.85:
            return "high"
        if confidence >= 0.6:
            return "medium"
        return "low"

    @staticmethod
    def _needs_clarification(question: str) -> List[str]:
        text = question.lower()
        if not text.strip():
            return []

        ambiguous_signals = ["best", "top", "latest", "trend", "performance", "growth"]
        has_ambiguous_word = any(token in text for token in ambiguous_signals)
        has_metric_hint = any(token in text for token in ["revenue", "quantity", "count", "sold", "sales"])
        has_time_hint = any(
            token in text
            for token in [
                "today",
                "yesterday",
                "week",
                "month",
                "quarter",
                "year",
                "last",
                "between",
                "from",
                "to",
            ]
        )

        needs_metric = has_ambiguous_word and not has_metric_hint
        needs_timeframe = has_ambiguous_word and not has_time_hint

        questions: List[str] = []
        if needs_timeframe:
            questions.append("Which time range should I use (for example: last 30 days, last quarter)?")
        if needs_metric:
            questions.append("How should I measure this (for example: revenue, quantity sold, or count)?")
        return questions

    def clarify(
        self,
        question: str,
        user_id: Optional[str] = None,
        database_name: Optional[str] = None,
    ) -> ClarificationOutcome:
        clarification_questions = self._needs_clarification(question)
        if clarification_questions:
            return ClarificationOutcome(
                question=question,
                status="needs_clarification",
                clarification_questions=clarification_questions,
                reason="Question is ambiguous and needs clarification before SQL generation.",
            )

        preview = self.preview(question, user_id=user_id, database_name=database_name)
        return ClarificationOutcome(
            question=question,
            status="ready",
            safe_sql=preview.safe_sql,
            reason=preview.reason,
        )

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

    def preview(self, question: str, user_id: Optional[str] = None, database_name: Optional[str] = None) -> QueryPreview:
        normalized_user = self._normalize_user_id(user_id)
        clarification_questions = self._needs_clarification(question)
        if clarification_questions:
            return QueryPreview(
                question=question,
                status="blocked",
                reason="Low confidence due to ambiguous question. Clarification required.",
                confidence=0.4,
                confidence_band="low",
                auto_blocked=True,
            )

        resolved_db_path = self._resolve_database_path(database_name)
        schema = self.get_allowed_schema(resolved_db_path)
        schema_text = format_schema_for_prompt(schema)
        prompt_question = self._build_prompt_question(question, user_id=normalized_user)

        try:
            generated_sql = self.llm_client.generate_sql(prompt_question, schema_text)
            self._record_metric("llm_calls")
        except Exception as e:
            return QueryPreview(
                question=question,
                status="error",
                reason=f"LLM generation failed: {e}",
                confidence=0.0,
                confidence_band="low",
                auto_blocked=True,
            )

        try:
            validated = validate_sql(
                generated_sql,
                allowed_schema=schema,
                max_rows=self.max_result_rows,
                default_rows=self.default_row_limit,
                row_filters=self.row_filters,
            )
        except SQLValidationError as e:
            self._record_metric("validation_failures")
            return QueryPreview(
                question=question,
                status="blocked",
                generated_sql=generated_sql,
                reason=str(e),
                confidence=0.0,
                confidence_band="low",
                auto_blocked=True,
            )

        try:
            explain = explain_readonly(
                resolved_db_path,
                validated.safe_sql,
                timeout_seconds=self.query_timeout_seconds,
            )
        except QueryExecutionError as e:
            return QueryPreview(
                question=question,
                status="error",
                generated_sql=generated_sql,
                safe_sql=validated.safe_sql,
                reason=f"EXPLAIN failed: {e}",
                confidence=0.2,
                confidence_band="low",
                auto_blocked=True,
            )

        preview_confidence = 0.9 if validated.safe_sql and validated.safe_sql.lower() != generated_sql.lower() else 0.8
        preview_band = self._confidence_band(preview_confidence)
        auto_blocked = preview_band == "low"
        preview_reason = None
        if auto_blocked:
            preview_reason = "Low confidence query preview. Please clarify your question."

        return QueryPreview(
            question=question,
            status="ready",
            generated_sql=generated_sql,
            safe_sql=validated.safe_sql,
            reason=preview_reason,
            plan_rows=explain.plan_rows,
            plan_warnings=explain.warnings,
            confidence=preview_confidence,
            confidence_band=preview_band,
            auto_blocked=auto_blocked,
        )

    def execute_approved(
        self,
        question: str,
        safe_sql: str,
        user_id: Optional[str] = None,
        database_name: Optional[str] = None,
    ) -> QueryOutcome:
        self._record_metric("queries_total")
        normalized_user = self._normalize_user_id(user_id)
        resolved_db_path = self._resolve_database_path(database_name)

        schema = self.get_allowed_schema(resolved_db_path)
        try:
            validated = validate_sql(
                safe_sql,
                allowed_schema=schema,
                max_rows=self.max_result_rows,
                default_rows=self.default_row_limit,
                row_filters=self.row_filters,
            )
        except SQLValidationError as e:
            self._record_metric("validation_failures")
            self._record_metric("queries_blocked")
            explanation = self._build_explanation("blocked", safe_sql, None, str(e))
            confidence = self._build_confidence("blocked", safe_sql, None)
            self.audit_logger.record(
                question=question,
                generated_sql=safe_sql,
                safe_sql=None,
                status="blocked",
                reason=str(e),
                user_id=normalized_user,
                database_name=database_name,
            )
            return QueryOutcome(
                question=question,
                status="blocked",
                generated_sql=safe_sql,
                reason=str(e),
                explanation=explanation,
                confidence=confidence,
            )

        try:
            exec_result = execute_readonly(
                resolved_db_path,
                validated.safe_sql,
                max_rows=self.max_result_rows,
                timeout_seconds=self.query_timeout_seconds,
            )
        except QueryExecutionError as e:
            self._record_metric("execution_failures")
            self._record_metric("queries_error")
            explanation = self._build_explanation("error", safe_sql, validated.safe_sql, str(e))
            confidence = self._build_confidence("error", safe_sql, validated.safe_sql)
            self.audit_logger.record(
                question=question,
                generated_sql=safe_sql,
                safe_sql=validated.safe_sql,
                status="error",
                reason=str(e),
                user_id=normalized_user,
                database_name=database_name,
            )
            return QueryOutcome(
                question=question,
                status="error",
                generated_sql=safe_sql,
                safe_sql=validated.safe_sql,
                reason=str(e),
                explanation=explanation,
                confidence=confidence,
            )

        redacted_rows = self._redact_rows(exec_result.rows)
        summary = self._summarize_result(exec_result.columns, redacted_rows, exec_result.row_count)
        self._record_metric("queries_success")
        self.audit_logger.record(
            question=question,
            generated_sql=safe_sql,
            safe_sql=validated.safe_sql,
            status="success",
            row_count=exec_result.row_count,
            execution_time_ms=exec_result.execution_time_ms,
            user_id=normalized_user,
            database_name=database_name,
        )

        explanation = self._build_explanation("success", safe_sql, validated.safe_sql, None)
        confidence = self._build_confidence("success", safe_sql, validated.safe_sql)
        result_warnings, stats = self._build_result_quality(
            exec_result.columns,
            redacted_rows,
            exec_result.row_count,
            exec_result.truncated,
        )
        outcome = QueryOutcome(
            question=question,
            status="success",
            generated_sql=safe_sql,
            safe_sql=validated.safe_sql,
            columns=exec_result.columns,
            rows=redacted_rows,
            row_count=exec_result.row_count,
            truncated=exec_result.truncated,
            execution_time_ms=exec_result.execution_time_ms,
            summary=summary,
            explanation=explanation,
            confidence=confidence,
            result_warnings=result_warnings,
            stats=stats,
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
        self._append_history_entry(normalized_user, entry)
        return outcome

    def answer(
        self,
        question: str,
        user_id: Optional[str] = None,
        database_name: Optional[str] = None,
        progress_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> QueryOutcome:
        self._record_metric("queries_total")
        normalized_user = self._normalize_user_id(user_id)
        resolved_db_path = self._resolve_database_path(database_name)
        now = time.monotonic()
        try:
            self._check_rate_limit(normalized_user)
        except RateLimitExceededError as exc:
            self._record_metric("queries_blocked")
            return QueryOutcome(question=question, status="blocked", reason=str(exc))

        cache_key = f"{question.strip().lower()}::{normalized_user}::{database_name or resolved_db_path}"
        self._cleanup_expired_cache(now)
        if self.cache_enabled and cache_key in self.cache:
            cached = self.cache.pop(cache_key)
            if now - cached["timestamp"] < self.cache_ttl_seconds:
                self.cache[cache_key] = cached
                self._record_metric("cache_hits")
                cached_outcome = copy.deepcopy(cached["outcome"])
                cached_outcome.summary = cached_outcome.summary or "Cached answer"
                return cached_outcome
        if self.cache_enabled:
            self._record_metric("cache_misses")

        schema = self.get_allowed_schema(resolved_db_path)
        schema_text = format_schema_for_prompt(schema)
        prompt_question = self._build_prompt_question(question, user_id=normalized_user)

        for attempt in range(self.max_retries + 1):
            self._emit_progress(progress_callback, "generating", {"attempt": attempt + 1})
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
                self._emit_progress(progress_callback, "error", {"reason": str(e)})
                return QueryOutcome(question=question, status="error", reason=str(e))

            logger.info(f"LLM produced SQL: {generated_sql!r}")

            try:
                self._emit_progress(progress_callback, "validating", {})
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
                self._append_history_entry(normalized_user, entry)
                self._emit_progress(progress_callback, "blocked", {"reason": str(e)})
                return QueryOutcome(
                    question=question,
                    status="blocked",
                    generated_sql=generated_sql,
                    reason=str(e),
                    explanation=explanation,
                    confidence=confidence,
                )

            try:
                self._emit_progress(progress_callback, "executing", {})
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
                self._append_history_entry(normalized_user, entry)
                self._emit_progress(progress_callback, "error", {"reason": str(e)})
                return QueryOutcome(
                    question=question,
                    status="error",
                    generated_sql=generated_sql,
                    safe_sql=result.safe_sql,
                    reason=str(e),
                    explanation=explanation,
                    confidence=confidence,
                )

            self._emit_progress(progress_callback, "formatting", {})
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
            result_warnings, stats = self._build_result_quality(
                exec_result.columns,
                redacted_rows,
                exec_result.row_count,
                exec_result.truncated,
            )
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
                result_warnings=result_warnings,
                stats=stats,
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
            self._append_history_entry(normalized_user, entry)

            self._upsert_cache(cache_key, outcome, time.monotonic())
            self._emit_progress(progress_callback, "done", {"status": outcome.status})
            return outcome

        self._emit_progress(progress_callback, "error", {"reason": "Unexpected service failure"})
        return QueryOutcome(question=question, status="error", reason="Unexpected service failure")
