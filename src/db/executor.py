"""
The "Safe Execution" stage: run already-validated SQL defensively.

Even after validation, execution itself is hardened:

  - The connection is opened read-only at the SQLite level
    (`mode=ro`), so even a bug in the validator can't result in a
    write -- the OS/DB layer refuses it outright.
  - A progress handler aborts any query that runs too long, protecting
    against expensive/DoS-style queries (e.g. cartesian-product joins).
  - Results are capped defensively a second time here, independent of
    the LIMIT already injected by the validator.
"""

import sqlite3
import time
from dataclasses import dataclass
from typing import Any, List

from src.db.connectors import DatabaseConnectorError, create_connection


class QueryExecutionError(Exception):
    pass


class QueryTimeoutError(QueryExecutionError):
    pass


@dataclass
class ExecutionResult:
    columns: List[str]
    rows: List[List[Any]]
    row_count: int
    truncated: bool
    execution_time_ms: float


@dataclass
class ExplainResult:
    plan_rows: List[List[Any]]
    warnings: List[str]


def execute_readonly(
    db_path: str,
    sql: str,
    max_rows: int = 100,
    timeout_seconds: int = 5,
) -> ExecutionResult:
    start = time.monotonic()

    try:
        conn, backend = create_connection(db_path)
    except DatabaseConnectorError as exc:
        raise QueryExecutionError(str(exc)) from exc

    deadline = start + timeout_seconds

    if backend == "sqlite":
        # Abort the query if it runs too long. SQLite calls this handler
        # periodically during execution; returning non-zero cancels the query.
        def _progress_handler() -> int:
            return 1 if time.monotonic() > deadline else 0

        conn.set_progress_handler(_progress_handler, 1000)

    try:
        cursor = conn.cursor()
        if backend == "postgresql":
            cursor.execute("SET default_transaction_read_only = on")
            cursor.execute(f"SET statement_timeout = {int(timeout_seconds * 1000)}")
        elif backend == "mysql":
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_seconds * 1000)}")

        cursor.execute(sql)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []

        rows = cursor.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]

        elapsed_ms = (time.monotonic() - start) * 1000
        return ExecutionResult(
            columns=columns,
            rows=[list(row) for row in rows],
            row_count=len(rows),
            truncated=truncated,
            execution_time_ms=elapsed_ms,
        )
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e).lower():
            raise QueryTimeoutError(
                f"Query exceeded {timeout_seconds}s timeout and was aborted"
            )
        raise QueryExecutionError(str(e))
    except sqlite3.Error as e:
        raise QueryExecutionError(str(e))
    except Exception as e:
        message = str(e).lower()
        if "timeout" in message or "statement timeout" in message or "max_execution_time" in message:
            raise QueryTimeoutError(f"Query exceeded {timeout_seconds}s timeout and was aborted")
        raise QueryExecutionError(str(e))
    finally:
        conn.close()


def explain_readonly(
    db_path: str,
    sql: str,
    timeout_seconds: int = 5,
) -> ExplainResult:
    try:
        conn, backend = create_connection(db_path)
    except DatabaseConnectorError as exc:
        raise QueryExecutionError(str(exc)) from exc

    try:
        cursor = conn.cursor()
        if backend == "postgresql":
            cursor.execute("SET default_transaction_read_only = on")
            cursor.execute(f"SET statement_timeout = {int(timeout_seconds * 1000)}")
            cursor.execute(f"EXPLAIN {sql}")
            rows = [list(row) for row in cursor.fetchall()]
            plan_text = " ".join(str(cell) for row in rows for cell in row).lower()
        elif backend == "mysql":
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_seconds * 1000)}")
            cursor.execute(f"EXPLAIN {sql}")
            rows = [list(row) for row in cursor.fetchall()]
            plan_text = " ".join(str(cell) for row in rows for cell in row).lower()
        else:
            cursor.execute(f"EXPLAIN QUERY PLAN {sql}")
            rows = [list(row) for row in cursor.fetchall()]
            plan_text = " ".join(str(cell) for row in rows for cell in row).lower()

        warnings: List[str] = []
        if "scan" in plan_text and "index" not in plan_text:
            warnings.append("Query plan indicates a table scan without index usage.")
        if "temporary" in plan_text:
            warnings.append("Query plan uses temporary structures; review performance for large datasets.")
        if "filesort" in plan_text:
            warnings.append("Query plan uses filesort; consider indexing ORDER BY columns.")

        return ExplainResult(plan_rows=rows, warnings=warnings)
    except Exception as e:
        raise QueryExecutionError(str(e))
    finally:
        conn.close()
