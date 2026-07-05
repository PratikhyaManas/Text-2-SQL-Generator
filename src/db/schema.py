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

from typing import Dict, List

from src.db.connectors import DatabaseConnectorError, create_connection


def get_schema(db_path: str) -> Dict[str, List[str]]:
    """Introspect a configured database and return {table_name: [columns]}."""
    schema: Dict[str, List[str]] = {}

    conn, backend = create_connection(db_path)
    try:
        cursor = conn.cursor()
        if backend == "sqlite":
            cursor.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            tables = [row[0] for row in cursor.fetchall()]

            for table in tables:
                cursor.execute(f"PRAGMA table_info({table})")
                columns = [row[1] for row in cursor.fetchall()]
                schema[table] = columns
        elif backend == "postgresql":
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
            tables = [row[0] for row in cursor.fetchall()]
            for table in tables:
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s "
                    "ORDER BY ordinal_position",
                    (table,),
                )
                schema[table] = [row[0] for row in cursor.fetchall()]
        elif backend == "mysql":
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"
            )
            tables = [row[0] for row in cursor.fetchall()]
            for table in tables:
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = %s "
                    "ORDER BY ordinal_position",
                    (table,),
                )
                schema[table] = [row[0] for row in cursor.fetchall()]
        else:
            raise DatabaseConnectorError(f"Unsupported backend for schema introspection: {backend}")
    finally:
        conn.close()

    return schema


def format_schema_for_prompt(schema: Dict[str, List[str]]) -> str:
    """Render the schema as compact DDL-like text for the LLM prompt."""
    lines = []
    for table, columns in schema.items():
        lines.append(f"{table}({', '.join(columns)})")
    return "\n".join(lines)
