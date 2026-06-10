"""Re-sanitizing already-stored command text.

After a new secret pattern lands (here: Linear ``lin_api_`` keys), rows
ingested before it existed still carry the raw secret. ``scrub`` re-runs
redaction over them. These tests plant a secret *directly* in the
commands table (bypassing the ingest sanitizer, simulating a pre-pattern
row) and assert the scrub cleans it.
"""

from __future__ import annotations

from pathlib import Path

from fixtures import _linear_key

from undrudge import scrub, store


def _insert_command(conn, command: str, ext_id: str = "c1") -> None:
    conn.execute(
        "INSERT INTO commands(source, external_id, ts, command) "
        "VALUES ('atuin', ?, 1, ?)",
        (ext_id, command),
    )


def test_rescan_redacts_a_pre_pattern_linear_key(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    secret = _linear_key()
    _insert_command(conn, f"LINEAR_API_KEY={secret} linear-cli sync")

    stats = scrub.rescan_commands(conn)
    assert stats.scanned == 1
    assert stats.changed == 1

    stored = conn.execute("SELECT command FROM commands").fetchone()[0]
    assert secret not in stored
    assert "[REDACTED" in stored


def test_rescan_is_idempotent(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    _insert_command(conn, f"export LIN={_linear_key()}")
    scrub.rescan_commands(conn)              # first pass cleans it
    second = scrub.rescan_commands(conn)     # nothing left to change
    assert second.scanned == 1
    assert second.changed == 0


def test_rescan_leaves_clean_rows_untouched(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    _insert_command(conn, "git status", "a")
    _insert_command(conn, "ls -la /tmp", "b")
    stats = scrub.rescan_commands(conn)
    assert stats.scanned == 2
    assert stats.changed == 0
    rows = {r[0] for r in conn.execute("SELECT command FROM commands")}
    assert rows == {"git status", "ls -la /tmp"}


def test_rescan_dry_run_does_not_write(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    secret = _linear_key()
    _insert_command(conn, f"key={secret}")
    stats = scrub.rescan_commands(conn, dry_run=True)
    assert stats.changed == 1
    # Row is unchanged on disk.
    stored = conn.execute("SELECT command FROM commands").fetchone()[0]
    assert secret in stored
