"""Ingest atuin shell history.

atuin's ``history.db`` is the source of truth. We open it read-only via the
SQLite URI flag — atuin uses WAL and the filesystem permissions usually keep
it user-only, but we never write either way.

Cursor: max ``timestamp`` (nanoseconds since epoch) processed so far. The
``UNIQUE(source, external_id)`` constraint on ``commands`` makes re-ingest
trivially safe — duplicates collapse via ``INSERT OR IGNORE``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import sanitize, store

ATUIN_SOURCE = "atuin"

# Atuin's daemon holds history.db and uses WAL; sporadic
# `OperationalError: unable to open database file` shows up around
# checkpoints and macOS DarkWake transitions when launchd fires gather
# during a not-fully-awake window. Retry-with-backoff swallows both.
_OPEN_ATTEMPTS = 3
_OPEN_BACKOFF_SECONDS = 1.0


@dataclass
class ShellIngestStats:
    rows_seen: int = 0
    rows_inserted: int = 0
    rows_dropped: int = 0
    last_ts_ns: int = 0


def _read_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT position FROM cursors WHERE source = ?", (ATUIN_SOURCE,)
    ).fetchone()
    if not row:
        return 0
    try:
        pos = json.loads(row["position"])
        return int(pos.get("max_ts_ns", 0))
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0


def _write_cursor(conn: sqlite3.Connection, max_ts_ns: int) -> None:
    conn.execute(
        """INSERT INTO cursors(source, position, updated_at)
              VALUES (?, ?, ?)
           ON CONFLICT(source) DO UPDATE SET
              position   = excluded.position,
              updated_at = excluded.updated_at""",
        (ATUIN_SOURCE, json.dumps({"max_ts_ns": max_ts_ns}), store.now_ms()),
    )


def ingest(
    conn: sqlite3.Connection,
    atuin_db_path: Path,
    *,
    fail_loud: bool = True,
) -> ShellIngestStats:
    stats = ShellIngestStats()
    if not atuin_db_path.exists():
        return stats

    cursor_ns = _read_cursor(conn)
    stats.last_ts_ns = cursor_ns

    rows = _read_atuin_rows_with_retry(atuin_db_path, cursor_ns)

    max_ts_ns = cursor_ns
    for row in rows:
        stats.rows_seen += 1
        ts_ns = int(row["timestamp"]) if row["timestamp"] is not None else 0
        ts_ms = ts_ns // 1_000_000
        duration_ms = (int(row["duration"]) // 1_000_000) if row["duration"] else None
        command = row["command"] or ""

        try:
            r = sanitize.redact_command(command)
            sanitized = r.text
        except Exception as e:
            store.log_redaction_failure(conn, "atuin", f"{type(e).__name__}: {e}")
            stats.rows_dropped += 1
            if fail_loud:
                continue
            sanitized = sanitize.REDACTED

        if not sanitized.strip():
            stats.rows_dropped += 1
            continue

        # Hostname is stored as "host:user" by atuin; the user part isn't a
        # secret but isn't useful either. Keep raw — paths/usernames are not
        # secrets in our threat model.
        row_keys = row.keys()
        author = row["author"] if "author" in row_keys else None
        intent = row["intent"] if "intent" in row_keys else None
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO commands
                       (source, external_id, ts, shell, cwd, hostname,
                        command, exit_status, duration_ms, author, intent)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ATUIN_SOURCE,
                    row["id"],
                    ts_ms,
                    None,  # atuin doesn't record shell name distinctly
                    row["cwd"],
                    row["hostname"],
                    sanitized,
                    int(row["exit"]) if row["exit"] is not None else None,
                    duration_ms,
                    author or None,
                    intent or None,
                ),
            )
            if cur.rowcount > 0:
                stats.rows_inserted += 1
        except sqlite3.IntegrityError as e:
            store.log_redaction_failure(conn, "atuin", f"insert: {e}")
            stats.rows_dropped += 1

        if ts_ns > max_ts_ns:
            max_ts_ns = ts_ns

    if max_ts_ns > cursor_ns:
        _write_cursor(conn, max_ts_ns)
        stats.last_ts_ns = max_ts_ns

    return stats


def _read_atuin_rows_with_retry(
    atuin_db_path: Path, cursor_ns: int
) -> list[sqlite3.Row]:
    """Open atuin's history.db read-only and pull rows newer than cursor_ns.

    Retries a small number of times on ``OperationalError`` so transient
    "unable to open database file" failures (atuin daemon mid-checkpoint,
    DarkWake fs hand-off) don't surface as a noisy gather traceback.
    """
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(_OPEN_ATTEMPTS):
        try:
            uri = f"file:{atuin_db_path}?mode=ro"
            src = sqlite3.connect(uri, uri=True)
            src.row_factory = sqlite3.Row
            try:
                cols = {row[1] for row in src.execute("PRAGMA table_info(history)")}
                select_cols = (
                    "id, timestamp, duration, exit, command, cwd, session, hostname"
                )
                if "author" in cols:
                    select_cols += ", author"
                if "intent" in cols:
                    select_cols += ", intent"
                return src.execute(
                    f"""SELECT {select_cols}
                         FROM history
                        WHERE deleted_at IS NULL
                          AND timestamp > ?
                        ORDER BY timestamp ASC""",
                    (cursor_ns,),
                ).fetchall()
            finally:
                src.close()
        except sqlite3.OperationalError as e:
            last_exc = e
            if attempt < _OPEN_ATTEMPTS - 1:
                delay = _OPEN_BACKOFF_SECONDS * (2 ** attempt)
                print(
                    f"undrudge: atuin open failed ({e}); "
                    f"retry {attempt + 1}/{_OPEN_ATTEMPTS - 1} in {delay:.0f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc
