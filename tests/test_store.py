"""Schema application and additive migrations.

The DB is a query cache, but it accumulates state (recs, statuses,
reasons) that must survive a tool upgrade. ``apply_schema`` runs
``CREATE TABLE IF NOT EXISTS`` (a no-op on existing tables) followed by
``_migrate``, which ALTER-TABLEs in any columns added since the file was
created. These tests pin that bridge.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from undrudge import store


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_migrate_adds_reason_column_to_existing_recommendations(tmp_path: Path):
    """A DB created before the ``reason`` column must gain it on the next
    apply_schema, without disturbing existing rows."""
    p = tmp_path / "old.sqlite"
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE recommendations (
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, title TEXT NOT NULL,
          signature TEXT NOT NULL, body_path TEXT NOT NULL, evidence TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'logged', created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        );
        INSERT INTO recommendations
          VALUES ('id1','daily','t','s','/tmp/x.md','[]','logged',1,1);
        """
    )
    conn.commit()
    conn.close()

    assert "reason" not in _cols(sqlite3.connect(p), "recommendations")

    conn = store.open_db(p)
    store.apply_schema(conn)

    assert "reason" in _cols(conn, "recommendations")
    row = conn.execute(
        "SELECT id, status, reason FROM recommendations"
    ).fetchone()
    assert (row["id"], row["status"], row["reason"]) == ("id1", "logged", None)


def test_apply_schema_is_idempotent(tmp_path: Path):
    """Applying the schema twice is a no-op the second time (no dup-column
    error from the migration)."""
    conn = store.init(tmp_path / "u.sqlite")
    store.apply_schema(conn)  # second application must not raise
    assert "reason" in _cols(conn, "recommendations")


def test_fresh_db_has_reason_column(tmp_path: Path):
    conn = store.init(tmp_path / "fresh.sqlite")
    assert "reason" in _cols(conn, "recommendations")
