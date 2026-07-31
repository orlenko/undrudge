"""Retention — drop aged rows from the query cache.

The SQLite file is a rebuildable cache over Claude/Codex JSONL and atuin's
history.db, and every consumer of it (digest, analyze) only ever looks at a
trailing 24h/7d window. Rows older than a few weeks therefore buy nothing and
cost disk — a few months of dogfooding puts message text past a gigabyte.

Pruning is deliberately dumber than ingest: delete `messages` and `commands`
older than a cutoff, then drop `sessions` rows left with no messages at all.
Three properties matter:

- **Cursors are never touched.** Byte offsets stay where they are, so a prune
  can't cause a re-ingest of the history files it just pruned rows from.
- **Row identity is unaffected.** Message ids are ``<uuid>#<idx>``, and
  ``seq`` is derived from ``MAX(seq)`` of what remains — pruning the *oldest*
  rows of a session can't collide with the newest.
- **Deletion is batched.** Each batch is its own transaction so a long first
  prune stays interruptible and the WAL stays bounded; ``max_rows`` caps the
  work a single (hourly, unattended) gather is willing to do.

Recommendations are never pruned. They're the output of the whole system, they
are tiny, and their markdown bodies on disk are the durable artifact.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

from . import store

# Rows per transaction. Big enough that the per-statement overhead disappears,
# small enough that an interrupted prune loses little and the WAL stays small.
DEFAULT_BATCH = 5_000

# What a single unattended gather is willing to delete. The first prune after
# enabling retention on an old DB can face hundreds of thousands of rows;
# capping it keeps the hourly job quick and lets the backlog drain over a few
# runs instead of stalling one. `undrudge prune` runs uncapped by default.
GATHER_MAX_ROWS = 50_000

_DELETE_MESSAGES = """\
DELETE FROM messages
 WHERE rowid IN (SELECT rowid FROM messages WHERE ts < ? LIMIT ?)"""

_DELETE_COMMANDS = """\
DELETE FROM commands
 WHERE id IN (SELECT id FROM commands WHERE ts < ? LIMIT ?)"""

# Only sessions with *nothing* left. Using the strict "no messages at all"
# predicate (rather than "no messages newer than the cutoff") keeps the delete
# safe when max_rows truncated the message pass and aged rows still reference
# the session — a foreign key violation would otherwise abort the batch.
_DELETE_SESSIONS = """\
DELETE FROM sessions
 WHERE COALESCE(last_seen, 0) < ?
   AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id = sessions.id)"""

_COUNT_SESSIONS = """\
SELECT COUNT(*) FROM sessions s
 WHERE COALESCE(s.last_seen, 0) < ?
   AND NOT EXISTS (
         SELECT 1 FROM messages m WHERE m.session_id = s.id AND m.ts >= ?)"""


@dataclass
class PruneStats:
    cutoff_ms: int
    messages: int = 0
    commands: int = 0
    sessions: int = 0
    # True when max_rows stopped the run with aged rows still in the table.
    truncated: bool = False

    @property
    def rows(self) -> int:
        return self.messages + self.commands


def cutoff_for_days(days: int, *, now_ms: int | None = None) -> int:
    """Millisecond timestamp `days` before now."""
    return (now_ms if now_ms is not None else store.now_ms()) - days * 86_400_000


def count_aged(conn, cutoff_ms: int) -> tuple[int, int]:
    """(messages, commands) older than the cutoff."""
    msgs = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE ts < ?", (cutoff_ms,)
    ).fetchone()[0]
    cmds = conn.execute(
        "SELECT COUNT(*) FROM commands WHERE ts < ?", (cutoff_ms,)
    ).fetchone()[0]
    return int(msgs), int(cmds)


def oldest_message_ms(conn) -> int | None:
    row = conn.execute("SELECT MIN(ts) FROM messages").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def run(
    conn,
    *,
    cutoff_ms: int,
    dry_run: bool = False,
    max_rows: int | None = None,
    batch: int = DEFAULT_BATCH,
) -> PruneStats:
    """Delete rows older than ``cutoff_ms``. Returns what was (or would be) cut."""
    stats = PruneStats(cutoff_ms=cutoff_ms)

    if dry_run:
        stats.messages, stats.commands = count_aged(conn, cutoff_ms)
        stats.sessions = int(
            conn.execute(_COUNT_SESSIONS, (cutoff_ms, cutoff_ms)).fetchone()[0]
        )
        if max_rows is not None and stats.rows > max_rows:
            stats.truncated = True
        return stats

    budget = -1 if max_rows is None else max(max_rows, 0)
    stats.messages, budget = _delete_batched(conn, _DELETE_MESSAGES, cutoff_ms, budget, batch)
    stats.commands, budget = _delete_batched(conn, _DELETE_COMMANDS, cutoff_ms, budget, batch)

    if budget != 0:
        with store.transaction(conn):
            stats.sessions = max(conn.execute(_DELETE_SESSIONS, (cutoff_ms,)).rowcount, 0)

    if budget == 0:
        remaining_msgs, remaining_cmds = count_aged(conn, cutoff_ms)
        stats.truncated = bool(remaining_msgs or remaining_cmds)
    return stats


def _delete_batched(
    conn, sql: str, cutoff_ms: int, budget: int, batch: int
) -> tuple[int, int]:
    """Delete in bounded batches. Returns (deleted, remaining budget).

    ``budget`` of -1 means unlimited; it is returned unchanged in that case so
    callers can tell "capped and exhausted" (0) from "no cap" (-1).
    """
    deleted = 0
    while True:
        limit = batch if budget < 0 else min(batch, budget)
        if limit <= 0:
            break
        with store.transaction(conn):
            affected = max(conn.execute(sql, (cutoff_ms, limit)).rowcount, 0)
        deleted += affected
        if budget > 0:
            budget -= affected
        if affected < limit:
            break
    return deleted, budget


def compact(conn) -> None:
    """Merge FTS segments and rewrite the file to reclaim freed pages.

    Slow and disk-hungry (VACUUM needs room for a second copy), so it is never
    part of an unattended gather — only ``undrudge prune --vacuum``.
    """
    for table in ("messages_fts", "commands_fts"):
        conn.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")


def db_size_bytes(db_path: Path) -> int:
    """On-disk footprint of the database, WAL and shm included."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix)
        with contextlib.suppress(OSError):
            total += p.stat().st_size
    return total


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
