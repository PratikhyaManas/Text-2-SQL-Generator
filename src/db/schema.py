"""
Database schema introspection.

The schema pulled here serves two purposes at once:

  1. It is formatted into the LLM prompt so the model only ever "sees"
     the tables/columns it's allowed to use.
  2. It is used by the SQL validator as an allow-list -- any table the
     generated SQL references that isn't in this schema is rejected,
     regardless of what the LLM produced.

Keeping a single source of truth for "what the model may touch" avoids
the classic drift between documentation/prompt and actual enforcement.
"""

import sqlite3
from typing import Dict, List


def get_schema(db_path: str) -> Dict[str, List[str]]:
    """Introspect a SQLite database and return {table_name: [columns]}."""
    schema: Dict[str, List[str]] = {}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = [row[0] for row in cursor.fetchall()]

        for table in tables:
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [row[1] for row in cursor.fetchall()]
            schema[table] = columns
    finally:
        conn.close()

    return schema


def format_schema_for_prompt(schema: Dict[str, List[str]]) -> str:
    """Render the schema as compact DDL-like text for the LLM prompt."""
    lines = []
    for table, columns in schema.items():
        lines.append(f"{table}({', '.join(columns)})")
    return "\n".join(lines)
