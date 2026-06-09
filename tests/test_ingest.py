"""End-to-end ingest tests.

The privacy invariant carries through: anything planted in fixture data
must be absent from every column of every row in the resulting DB.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fixtures import ANTHROPIC_KEY, GITHUB_TOKEN, GITHUB_TOKEN_FAKE_REPEATED

from undrudge import ingest_claude, ingest_shell

# Secrets planted in the fixtures (mirrors conftest.py).
PLANTED = [GITHUB_TOKEN, ANTHROPIC_KEY, GITHUB_TOKEN_FAKE_REPEATED]


def _all_text(conn: sqlite3.Connection) -> str:
    """Concatenate every textual column of every row in messages and commands.

    Used as a single haystack for "no planted secret survives" assertions.
    """
    parts: list[str] = []
    for row in conn.execute(
        "SELECT text, tool_input, tool_result FROM messages"
    ):
        parts.extend(c or "" for c in row)
    for row in conn.execute("SELECT command FROM commands"):
        parts.append(row[0] or "")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Claude ingest
# --------------------------------------------------------------------------


def test_claude_ingest_basic(db, claude_projects: Path):
    stats = ingest_claude.ingest(db, claude_projects)

    assert stats.files_seen == 1
    assert stats.lines_read >= 4
    assert stats.rows_inserted >= 4  # user, thinking, text, tool_use, tool_result

    sessions = db.execute("SELECT id, project FROM sessions").fetchall()
    assert len(sessions) == 1
    assert sessions[0]["project"] == "/Users/fake/repo"

    rows = db.execute(
        "SELECT role, text, tool_name, tool_input, tool_result FROM messages ORDER BY seq"
    ).fetchall()
    roles = [r["role"] for r in rows]
    assert "user" in roles
    assert "assistant" in roles
    assert "tool" in roles  # tool_result rows take role='tool'


def test_claude_ingest_redacts_planted_secrets(db, claude_projects: Path):
    ingest_claude.ingest(db, claude_projects)
    haystack = _all_text(db)
    for secret in PLANTED:
        assert secret not in haystack, f"{secret!r} survived ingest"


def test_claude_ingest_drops_excluded_file_content(db, claude_projects: Path):
    ingest_claude.ingest(db, claude_projects)
    rows = db.execute(
        "SELECT tool_input FROM messages WHERE tool_name = 'Read'"
    ).fetchall()
    assert rows, "expected a Read tool_use row"
    payload = json.loads(rows[0]["tool_input"])
    assert payload["file_path"] == "/Users/fake/.env"
    # Content for sensitive files must be dropped to the placeholder.
    assert payload.get("content", "").startswith("[REDACTED")


def test_claude_ingest_idempotent(db, claude_projects: Path):
    first = ingest_claude.ingest(db, claude_projects)
    second = ingest_claude.ingest(db, claude_projects)
    assert second.rows_inserted == 0
    count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == first.rows_inserted


def test_claude_ingest_resumes_from_cursor_on_append(
    db, claude_projects: Path, tmp_path: Path
):
    """Adding new lines should pick up only the new ones on the second run."""
    ingest_claude.ingest(db, claude_projects)
    before_count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    jsonl = next(claude_projects.rglob("*.jsonl"))
    with jsonl.open("a") as f:
        f.write(json.dumps({
            "type": "user",
            "uuid": "u-3",
            "sessionId": "00000000-1111-2222-3333-444444444444",
            "timestamp": "2026-05-04T10:01:00.000Z",
            "cwd": "/Users/fake/repo",
            "message": {"role": "user", "content": "another prompt"},
        }) + "\n")

    second = ingest_claude.ingest(db, claude_projects)
    assert second.rows_inserted == 1
    after_count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert after_count == before_count + 1


def test_claude_ingest_resets_cursor_on_truncation(
    db, claude_projects: Path, tmp_path: Path
):
    ingest_claude.ingest(db, claude_projects)
    jsonl = next(claude_projects.rglob("*.jsonl"))
    # Truncate the file — simulate a rotation.
    jsonl.write_text(
        json.dumps({
            "type": "user",
            "uuid": "fresh-1",
            "sessionId": "11111111-2222-3333-4444-555555555555",
            "timestamp": "2026-05-04T11:00:00.000Z",
            "cwd": "/Users/fake/repo",
            "message": {"role": "user", "content": "after truncation"},
        }) + "\n"
    )

    stats = ingest_claude.ingest(db, claude_projects)
    assert stats.rows_inserted == 1
    new_session = db.execute(
        "SELECT id FROM sessions WHERE id = ?",
        ("11111111-2222-3333-4444-555555555555",),
    ).fetchone()
    assert new_session is not None


def test_claude_ingest_skips_malformed_lines(db, tmp_path: Path):
    proj = tmp_path / "claude" / "-x"
    proj.mkdir(parents=True)
    f = proj / "abc.jsonl"
    f.write_text(
        "this is not json\n"
        + json.dumps({
            "type": "user", "uuid": "u-1",
            "sessionId": "ses-1",
            "timestamp": "2026-05-04T10:00:00.000Z",
            "message": {"role": "user", "content": "hello"},
        }) + "\n"
    )
    stats = ingest_claude.ingest(db, tmp_path / "claude")
    assert stats.rows_inserted == 1
    assert stats.lines_skipped == 1


# --------------------------------------------------------------------------
# Shell ingest
# --------------------------------------------------------------------------


def test_shell_ingest_basic(db, atuin_db: Path):
    stats = ingest_shell.ingest(db, atuin_db)
    assert stats.rows_seen == 3  # the deleted row is filtered server-side
    assert stats.rows_inserted == 3

    rows = db.execute("SELECT command FROM commands ORDER BY ts").fetchall()
    assert len(rows) == 3
    assert "ls -la" in rows[0]["command"]


def test_shell_ingest_captures_author_and_intent(db, atuin_db: Path):
    ingest_shell.ingest(db, atuin_db)
    row = db.execute(
        "SELECT author, intent FROM commands WHERE external_id = 'a2'"
    ).fetchone()
    assert row["author"] == "claude-code"
    assert row["intent"] == "set token"


def test_shell_ingest_redacts_secret_in_command(db, atuin_db: Path):
    ingest_shell.ingest(db, atuin_db)
    haystack = _all_text(db)
    assert GITHUB_TOKEN_FAKE_REPEATED not in haystack


def test_shell_ingest_idempotent(db, atuin_db: Path):
    ingest_shell.ingest(db, atuin_db)
    second = ingest_shell.ingest(db, atuin_db)
    assert second.rows_inserted == 0


def test_shell_ingest_does_not_touch_source_db(db, atuin_db: Path):
    """The live atuin db must not be opened, locked, or modified —
    snapshot path reads only from the temp copy.
    """
    before = atuin_db.stat()
    ingest_shell.ingest(db, atuin_db)
    after = atuin_db.stat()
    assert before.st_mtime_ns == after.st_mtime_ns
    assert before.st_size == after.st_size
    # Neither -wal nor -shm should appear in the source dir from our read
    # (the fixture isn't in WAL mode, so they shouldn't exist either way).
    assert not atuin_db.with_name(atuin_db.name + "-wal").exists()
    assert not atuin_db.with_name(atuin_db.name + "-shm").exists()


def test_shell_ingest_propagates_persistent_snapshot_failure(
    db, atuin_db: Path, monkeypatch
):
    """If the snapshot keeps failing past the retry budget, the error
    surfaces to the caller (the gather CLI records the gather_failed
    event from there). Cursor must not advance.
    """
    def always_fail(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(ingest_shell, "_snapshot_and_read_once", always_fail)
    monkeypatch.setattr(ingest_shell.time, "sleep", lambda _s: None)

    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        ingest_shell.ingest(db, atuin_db)

    cur = db.execute(
        "SELECT position FROM cursors WHERE source = 'atuin'"
    ).fetchone()
    assert cur is None or json.loads(cur["position"]).get("max_ts_ns", 0) == 0


def test_shell_ingest_retries_transient_snapshot_failure(
    db, atuin_db: Path, monkeypatch
):
    """Two transient failures then success → ingest still works.
    The retry loop exists for SQLITE_BUSY storms; verify it actually
    retries instead of bailing on the first error.
    """
    real_snapshot = ingest_shell._snapshot_and_read_once
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("unable to open database file")
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(ingest_shell, "_snapshot_and_read_once", flaky)
    monkeypatch.setattr(ingest_shell.time, "sleep", lambda _s: None)

    stats = ingest_shell.ingest(db, atuin_db)
    assert calls["n"] == 3
    assert stats.rows_inserted == 3


def test_shell_ingest_under_wal_concurrent_writer(db, tmp_path: Path):
    """A separate thread writes to a WAL-mode db while we ingest in a
    tight loop. The snapshot path must complete every run cleanly —
    no OperationalError, no rows lost, source file unmolested.
    """
    import threading

    atuin_path = tmp_path / "atuin.db"
    src = sqlite3.connect(atuin_path)
    src.execute("PRAGMA journal_mode=WAL")
    src.executescript(
        """
        CREATE TABLE history (
          id TEXT PRIMARY KEY, timestamp INTEGER, duration INTEGER, exit INTEGER,
          command TEXT, cwd TEXT, session TEXT, hostname TEXT,
          deleted_at INTEGER, author TEXT, intent TEXT
        );
        """
    )
    src.commit()
    src.close()

    stop = threading.Event()
    writes_done = {"n": 0}
    writer_errors: list[str] = []

    def writer() -> None:
        # Throttled writer: realistic atuin pace is bursts of commits,
        # not a pure busy loop. A pure busy loop can prevent SQLite's
        # backup API from ever converging — the source keeps changing
        # while pages are being copied. The 2ms sleep gives backup
        # forward progress without removing the concurrency stress.
        w = sqlite3.connect(atuin_path, timeout=5.0)
        try:
            while not stop.is_set():
                i = writes_done["n"]
                ts_ns = 1_700_000_000_000_000_000 + i * 1_000_000
                try:
                    w.execute(
                        "INSERT OR IGNORE INTO history VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?)",
                        (f"w{i:08d}", ts_ns, 1000, 0, f"cmd {i}",
                         "/tmp", "s", "host:user", None, "", None),
                    )
                    w.commit()
                    writes_done["n"] += 1
                except sqlite3.OperationalError as e:
                    writer_errors.append(str(e))
                stop.wait(0.002)
        finally:
            w.close()

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        ingest_errors: list[BaseException] = []
        for _ in range(5):
            try:
                ingest_shell.ingest(db, atuin_path)
            except sqlite3.OperationalError as e:
                ingest_errors.append(e)
    finally:
        stop.set()
        t.join(timeout=5)

    assert ingest_errors == [], (
        f"snapshot reader raised under concurrent WAL writer: "
        f"{[str(e) for e in ingest_errors]}"
    )
    assert writes_done["n"] > 0

    # Force a WAL checkpoint so the rows the writer committed land in
    # the main db file. With immutable=1 the snapshot only sees
    # checkpointed data; without this final checkpoint the snapshot can
    # legitimately be empty (the freshness-gap trade-off we accepted),
    # which would mask whether the reader actually works.
    ck = sqlite3.connect(atuin_path)
    try:
        ck.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        ck.close()
    ingest_shell.ingest(db, atuin_path)

    seen = db.execute(
        "SELECT COUNT(*) FROM commands WHERE source = 'atuin'"
    ).fetchone()[0]
    assert seen > 0


def test_shell_ingest_resumes_after_new_rows(db, atuin_db: Path):
    ingest_shell.ingest(db, atuin_db)

    src = sqlite3.connect(atuin_db)
    src.execute(
        "INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("a5", 1714000040_000_000_000, 60_000_000, 0,
         "echo hi", "/Users/fake/repo", "s1", "host:user", None, "", None),
    )
    src.commit()
    src.close()

    stats = ingest_shell.ingest(db, atuin_db)
    assert stats.rows_inserted == 1


def test_shell_ingest_skips_deleted_rows(db, atuin_db: Path):
    ingest_shell.ingest(db, atuin_db)
    rm = db.execute(
        "SELECT 1 FROM commands WHERE command LIKE 'rm -rf%'"
    ).fetchone()
    assert rm is None


# --------------------------------------------------------------------------
# Combined gather behavior
# --------------------------------------------------------------------------


def test_gather_no_redaction_failures_on_clean_fixtures(db, claude_projects: Path, atuin_db: Path):
    ingest_claude.ingest(db, claude_projects)
    ingest_shell.ingest(db, atuin_db)
    failures = db.execute("SELECT COUNT(*) FROM redaction_failures").fetchone()[0]
    # Sanitization should succeed on every planted-secret case; the planted
    # secrets are *redacted*, not failures. Failures would mean the
    # sanitizer threw — that's the catastrophic case.
    assert failures == 0
