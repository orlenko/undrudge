"""Ingest atuin shell history.

atuin's ``history.db`` is the source of truth. atuin runs a long-lived
daemon that holds the db in WAL mode and writes continuously (shell
hooks stream every command in real time). A direct read-only open
against the live file intermittently fails at first SELECT with
``OperationalError: unable to open database file`` — the race is in
SQLite's WAL-index/-shm handshake, not in the file itself.

To avoid the race entirely we snapshot the db + WAL + SHM into a
private temp dir with ``shutil.copy2`` (byte-level reads, no SQLite
locks) and read from the copy. ~57MB of I/O per hourly gather is well
worth never debugging WAL races again.

Cursor: max ``timestamp`` (nanoseconds since epoch) processed so far.
``_write_cursor`` only fires after a successful ingest, so a snapshot
or read failure leaves the cursor untouched and the next gather picks
up the same window — no rows are lost. The ``UNIQUE(source,
external_id)`` constraint on ``commands`` makes re-ingest trivially
safe — duplicates collapse via ``INSERT OR IGNORE``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import sanitize, store

logger = logging.getLogger(__name__)

ATUIN_SOURCE = "atuin"

# Backup-API snapshots cooperate with the WAL writer at the page level,
# but very heavy load can still surface SQLITE_BUSY or transient errors.
# Three attempts with a small backoff is plenty for an hourly job.
_SNAPSHOT_ATTEMPTS = 3
_SNAPSHOT_BACKOFF_SECONDS = 0.5


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
        logger.info("atuin db missing: %s — skipping", atuin_db_path)
        return stats

    cursor_ns = _read_cursor(conn)
    stats.last_ts_ns = cursor_ns
    logger.info(
        "reading atuin from %s (cursor=%d ns)",
        atuin_db_path, cursor_ns,
    )
    rows = _read_atuin_rows_via_snapshot(atuin_db_path, cursor_ns)
    logger.debug("atuin returned %d candidate rows", len(rows))

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

    logger.info(
        "atuin ingest done: seen=%d inserted=%d dropped=%d cursor=%d",
        stats.rows_seen, stats.rows_inserted, stats.rows_dropped, stats.last_ts_ns,
    )
    return stats


def _read_atuin_rows_via_snapshot(
    atuin_db_path: Path, cursor_ns: int
) -> list[sqlite3.Row]:
    """Snapshot atuin's db via SQLite's backup API, then read from the copy.

    Two failure modes that had to be designed around at once:

    1. **Production WAL handshake race.** Plain ``file:...?mode=ro``
       against the live db racks up intermittent
       ``OperationalError: unable to open database file`` at first
       SELECT — SQLite needs to consult ``-shm`` and the WAL index,
       which races atuin's always-active writer.

    2. **Torn-page byte-copy.** Naively ``shutil.copy2``-ing the main db
       (or main + ``-wal`` + ``-shm``) while the writer is checkpointing
       can produce an internally inconsistent file
       (``DatabaseError: database disk image is malformed``).

    The fix: open the source read-only, use SQLite's online backup API
    (``Connection.backup``) to snapshot into a temp dir, then read from
    the snapshot with ``immutable=1``.

    Why this works:

    - The backup API is the canonical way to copy a live SQLite db.
      It iterates pages under brief shared locks and cooperates with
      concurrent writers — no torn pages.
    - We open the source with ``mode=ro``, so we still promise never
      to mutate or lock atuin's live file in any user-observable way.
    - The destination snapshot is in our private tempdir and opened
      with ``immutable=1`` for reads, which skips WAL/shm logic
      entirely on the read side. No race possible against a writer
      that doesn't exist for the copy.
    - A small retry loop handles transient ``SQLITE_BUSY`` /
      ``unable to open database file`` from the source under
      pathological load.

    Cursor safety: this function either returns rows or raises. The
    caller does not advance the cursor on exceptions, so a snapshot
    failure simply defers those rows to the next gather run — no
    history is lost.
    """
    last_exc: BaseException | None = None
    for attempt in range(_SNAPSHOT_ATTEMPTS):
        try:
            return _snapshot_and_read_once(atuin_db_path, cursor_ns)
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            last_exc = e
            if attempt < _SNAPSHOT_ATTEMPTS - 1:
                delay = _SNAPSHOT_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "atuin snapshot attempt %d/%d failed (%s); retry in %.1fs",
                    attempt + 1, _SNAPSHOT_ATTEMPTS, e, delay,
                )
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _snapshot_and_read_once(
    atuin_db_path: Path, cursor_ns: int
) -> list[sqlite3.Row]:
    with tempfile.TemporaryDirectory(prefix="undrudge-atuin-") as tmp:
        snapshot_path = Path(tmp) / "history-snapshot.db"

        src = sqlite3.connect(f"file:{atuin_db_path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(snapshot_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        reader = sqlite3.connect(
            f"file:{snapshot_path}?mode=ro&immutable=1", uri=True
        )
        reader.row_factory = sqlite3.Row
        try:
            cols = {row[1] for row in reader.execute("PRAGMA table_info(history)")}
            select_cols = (
                "id, timestamp, duration, exit, command, cwd, session, hostname"
            )
            if "author" in cols:
                select_cols += ", author"
            if "intent" in cols:
                select_cols += ", intent"
            return reader.execute(
                f"""SELECT {select_cols}
                     FROM history
                    WHERE deleted_at IS NULL
                      AND timestamp > ?
                    ORDER BY timestamp ASC""",
                (cursor_ns,),
            ).fetchall()
        finally:
            reader.close()
