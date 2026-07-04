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
from typing import Any, List, Optional


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


def execute_readonly(
    db_path: str,
    sql: str,
    max_rows: int = 100,
    timeout_seconds: int = 5,
) -> ExecutionResult:
    start = time.monotonic()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    # Abort the query if it runs too long. SQLite calls this handler
    # periodically during execution; returning non-zero cancels the query.
    deadline = start + timeout_seconds

    def _progress_handler() -> int:
        return 1 if time.monotonic() > deadline else 0

    conn.set_progress_handler(_progress_handler, 1000)

    try:
        cursor = conn.cursor()
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
    finally:
        conn.close()
