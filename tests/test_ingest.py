"""End-to-end ingest tests.

The privacy invariant carries through: anything planted in fixture data
must be absent from every column of every row in the resulting DB.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

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
    first = ingest_shell.ingest(db, atuin_db)
    second = ingest_shell.ingest(db, atuin_db)
    assert second.rows_inserted == 0
    count = db.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
    assert count == first.rows_inserted


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
