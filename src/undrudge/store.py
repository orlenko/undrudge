"""SQLite store. Thin wrapper around stdlib ``sqlite3``.

The DB is a query cache, not a system of record — the source of truth lives
in Claude/Codex JSONL files and atuin's history.db. If anything ever corrupts
here, delete and re-ingest.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path


def schema_sql() -> str:
    return (files("undrudge") / "schema.sql").read_text()


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(schema_sql())
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Light additive migrations for existing DBs.

    SQLite ``CREATE TABLE IF NOT EXISTS`` is a no-op when the table is
    already there, which means new columns we add to the schema never
    land on existing files. Tiny ALTER TABLE bridge per added column.
    """
    _ensure_columns(conn, "sessions", {
        "source": "ALTER TABLE sessions ADD COLUMN source TEXT NOT NULL DEFAULT 'claude'",
    })
    _ensure_columns(conn, "commands", {
        "author": "ALTER TABLE commands ADD COLUMN author TEXT",
        "intent": "ALTER TABLE commands ADD COLUMN intent TEXT",
    })
    _ensure_columns(conn, "messages", {
        "origin": "ALTER TABLE messages ADD COLUMN origin TEXT",
    })
    _ensure_columns(conn, "recommendations", {
        "reason": "ALTER TABLE recommendations ADD COLUMN reason TEXT",
    })


def _ensure_columns(
    conn: sqlite3.Connection, table: str, ddl_by_col: dict[str, str]
) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, ddl in ddl_by_col.items():
        if col not in existing:
            conn.execute(ddl)


def init(path: Path) -> sqlite3.Connection:
    conn = open_db(path)
    apply_schema(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def now_ms() -> int:
    return int(time.time() * 1000)


def log_redaction_failure(conn: sqlite3.Connection, source: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO redaction_failures(ts, source, reason) VALUES (?, ?, ?)",
        (now_ms(), source, reason[:500]),
    )
