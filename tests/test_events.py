"""Append-only JSONL event log behavior."""

from __future__ import annotations

import json
from pathlib import Path

from undrudge import events, store


def test_record_appends_one_line_per_event(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    events.record(log, "rec_written", {"id": "abc", "title": "first"})
    events.record(log, "rec_dismissed", {"id": "abc"})
    events.record(log, "analyze_complete", {"parsed": 3, "written": 2})

    lines = log.read_text().splitlines()
    assert len(lines) == 3
    parsed = [json.loads(ln) for ln in lines]
    assert parsed[0]["event"] == "rec_written"
    assert parsed[0]["id"] == "abc"
    assert parsed[0]["title"] == "first"
    assert parsed[1]["event"] == "rec_dismissed"
    assert parsed[2]["parsed"] == 3
    # Every line carries a millis-since-epoch ``ts``.
    for entry in parsed:
        assert isinstance(entry["ts"], int)
        assert entry["ts"] > 0


def test_record_creates_parent_dir(tmp_path: Path):
    log = tmp_path / "nested" / "subdir" / "events.jsonl"
    events.record(log, "rec_written", {"id": "x"})
    assert log.exists()
    entry = json.loads(log.read_text().strip())
    assert entry["id"] == "x"


def test_record_swallows_payload_with_unserialisable_value(tmp_path: Path):
    """A payload with a non-JSON value should not raise — ``default=str``
    is the safety net."""
    log = tmp_path / "events.jsonl"
    events.record(log, "rec_written", {"id": "x", "path": Path("/tmp/y")})
    entry = json.loads(log.read_text().strip())
    assert entry["path"] == "/tmp/y"


def test_record_swallows_io_error_and_logs_to_db(tmp_path: Path):
    """If the events log can't be written, fall through to log_redaction_failure."""
    db_path = tmp_path / "u.sqlite"
    conn = store.init(db_path)
    # Point at a path under a regular file (creation will fail).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    log = blocker / "events.jsonl"

    events.record(log, "rec_written", {"id": "x"}, conn=conn)

    # No exception should escape; a redaction_failures row should record
    # the underlying I/O error.
    row = conn.execute(
        "SELECT source, reason FROM redaction_failures ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[0] == "events_log"
    assert "FileExistsError" in row[1] or "NotADirectoryError" in row[1] or "OSError" in row[1]
