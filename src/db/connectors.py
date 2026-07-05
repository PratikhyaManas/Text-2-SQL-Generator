"""Database connector helpers for SQLite, PostgreSQL, and MySQL."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


SUPPORTED_BACKENDS = {"sqlite", "postgresql", "mysql"}


class DatabaseConnectorError(Exception):
    pass


@dataclass
class DatabaseTarget:
    backend: str
    target: str


def resolve_database_target(db_target: str) -> DatabaseTarget:
    if "://" in db_target:
        parsed = urlparse(db_target)
        scheme = parsed.scheme.lower()
        if scheme in {"postgres", "postgresql"}:
            return DatabaseTarget(backend="postgresql", target=db_target)
        if scheme in {"mysql", "mysql+pymysql"}:
            return DatabaseTarget(backend="mysql", target=db_target)
        if scheme == "sqlite":
            return DatabaseTarget(backend="sqlite", target=_sqlite_path_from_url(db_target))
        raise DatabaseConnectorError(f"Unsupported database URL scheme: {parsed.scheme}")

    return DatabaseTarget(backend="sqlite", target=db_target)


def _sqlite_path_from_url(db_url: str) -> str:
    parsed = urlparse(db_url)
    if parsed.netloc in {"", "localhost"}:
        path = unquote(parsed.path)
        if path.startswith("/") and os.name == "nt" and len(path) > 2 and path[2] == ":":
            return path.lstrip("/")
        return path
    raise DatabaseConnectorError("SQLite URL must be local file path (sqlite:///...) ")


def create_connection(db_target: str) -> tuple[Any, str]:
    resolved = resolve_database_target(db_target)

    if resolved.backend == "sqlite":
        conn = sqlite3.connect(f"file:{resolved.target}?mode=ro", uri=True)
        return conn, "sqlite"

    if resolved.backend == "postgresql":
        try:
            import psycopg  # type: ignore
        except ImportError as exc:
            raise DatabaseConnectorError(
                "PostgreSQL support requires psycopg. Install with: pip install 'psycopg[binary]'"
            ) from exc

        conn = psycopg.connect(resolved.target)
        setattr(conn, "autocommit", True)
        return conn, "postgresql"

    if resolved.backend == "mysql":
        try:
            import pymysql  # type: ignore
        except ImportError as exc:
            raise DatabaseConnectorError(
                "MySQL support requires pymysql. Install with: pip install pymysql"
            ) from exc

        parsed = urlparse(resolved.target)
        params = parse_qs(parsed.query)

        conn = pymysql.connect(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            database=(parsed.path or "/").lstrip("/") or None,
            charset=params.get("charset", ["utf8mb4"])[0],
            autocommit=True,
        )
        return conn, "mysql"

    raise DatabaseConnectorError(f"Unsupported backend: {resolved.backend}")
