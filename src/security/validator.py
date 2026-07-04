"""
The security guard in `User -> LLM -> SQL -> Validator -> Safe Execution`.

We never execute what a local LLM produces just because it looks like
SQL. Every generated statement passes through this validator, which
enforces several independent layers of defense (defense-in-depth: any
one of these failing to catch an attack doesn't mean the others do too):

  1. Structural sanity   -- exactly one statement, must parse as SQL.
  2. Statement allow-list -- only SELECT is ever permitted. Everything
     else (INSERT/UPDATE/DELETE/DROP/ALTER/ATTACH/PRAGMA/...) is
     rejected outright, regardless of intent.
  3. Keyword blocklist    -- a defense-in-depth regex pass that catches
     dangerous keywords even if they show up somewhere the parser-based
     check doesn't expect (belt-and-suspenders, not the primary
     defense).
  4. Schema allow-list    -- every table referenced must exist in the
     schema we explicitly exposed to the model. This is what stops
     attempts to read `sqlite_master`, hidden tables, or anything
     outside the sanctioned dataset.
  5. Row-limit enforcement -- a LIMIT clause is injected or clamped so
     a single query can never pull back an unbounded result set.

The validator returns a normalized, re-serialized SQL string (produced
by re-emitting the parsed AST) rather than the original text. That
re-serialization step is itself a defense: inline comments, stray
semicolons, and other textual tricks are dropped because they were
never part of the parsed AST in the first place.
"""

from dataclasses import dataclass
from typing import Dict, List, Set

import sqlglot
from sqlglot import exp

# Statement types that are never allowed, no matter how they're phrased.
# The primary defense is the "only exp.Select is allowed" check below;
# this list is a secondary, human-readable guard for keyword scanning.
FORBIDDEN_KEYWORDS = {
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "attach", "detach", "pragma", "vacuum", "reindex", "grant", "revoke",
    "replace", "exec", "execute", "load_extension", "into outfile",
    "into dumpfile",
}


class SQLValidationError(Exception):
    """Raised whenever a generated query fails any security check."""


@dataclass
class ValidationResult:
    original_sql: str
    safe_sql: str
    tables_used: List[str]
    limit_applied: int


def _check_forbidden_keywords(raw_sql: str) -> None:
    lowered = raw_sql.lower()
    for keyword in FORBIDDEN_KEYWORDS:
        if keyword in lowered:
            raise SQLValidationError(
                f"Query contains a forbidden keyword: '{keyword}'"
            )


def _parse_single_statement(raw_sql: str) -> exp.Expression:
    if ";" in raw_sql.strip().rstrip(";"):
        # A semicolon anywhere but the very end means multiple/stacked
        # statements were attempted (e.g. "SELECT 1; DROP TABLE x;").
        raise SQLValidationError("Multiple/stacked SQL statements are not allowed")

    try:
        statements = sqlglot.parse(raw_sql, read="sqlite")
    except Exception as e:
        raise SQLValidationError(f"Query failed to parse: {e}")

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SQLValidationError(
            f"Expected exactly one SQL statement, found {len(statements)}"
        )

    return statements[0]


def _ensure_select_only(statement: exp.Expression) -> None:
    if not isinstance(statement, exp.Select):
        raise SQLValidationError(
            f"Only SELECT statements are allowed, got: {type(statement).__name__}"
        )


def _extract_tables(statement: exp.Expression) -> Set[str]:
    return {table.name.lower() for table in statement.find_all(exp.Table)}


def _ensure_tables_allowed(tables: Set[str], allowed_schema: Dict[str, List[str]]) -> None:
    allowed = {t.lower() for t in allowed_schema.keys()}
    disallowed = tables - allowed
    if disallowed:
        raise SQLValidationError(
            f"Query references table(s) outside the allowed schema: {sorted(disallowed)}"
        )
    if not tables:
        raise SQLValidationError("Query does not reference any known table")


def _enforce_row_limit(statement: exp.Select, max_rows: int, default_rows: int) -> int:
    existing_limit = statement.args.get("limit")

    if existing_limit is None:
        statement.set("limit", exp.Limit(expression=exp.Literal.number(default_rows)))
        return default_rows

    try:
        requested = int(existing_limit.expression.this)
    except (AttributeError, ValueError, TypeError):
        requested = max_rows

    if requested > max_rows:
        statement.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return max_rows

    return requested


def validate_sql(
    raw_sql: str,
    allowed_schema: Dict[str, List[str]],
    max_rows: int = 100,
    default_rows: int = 50,
) -> ValidationResult:
    """
    Run every safety check against a candidate SQL string.

    Raises SQLValidationError with a human-readable reason on the first
    check that fails. Returns a ValidationResult with the sanitized,
    re-serialized SQL on success.
    """
    if not raw_sql or not raw_sql.strip():
        raise SQLValidationError("Empty SQL query")

    _check_forbidden_keywords(raw_sql)

    statement = _parse_single_statement(raw_sql)
    _ensure_select_only(statement)

    tables = _extract_tables(statement)
    _ensure_tables_allowed(tables, allowed_schema)

    limit_applied = _enforce_row_limit(statement, max_rows, default_rows)

    safe_sql = statement.sql(dialect="sqlite")

    return ValidationResult(
        original_sql=raw_sql,
        safe_sql=safe_sql,
        tables_used=sorted(tables),
        limit_applied=limit_applied,
    )
