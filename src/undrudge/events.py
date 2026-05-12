"""Append-only JSONL audit trail.

Each rec write, status change, and analyze run logs a single line of
JSON to ``cfg.paths.events_log``. Orthogonal to the SQLite store: the
DB tracks current state, the events log tracks history. Useful for
"when did I dismiss this rec?", "how often does the analyze run
produce anything?", and "show me the resolution-rate trend."

Format: one JSON object per line, with at minimum ``ts`` (millis since
epoch) and ``event`` (string tag). Extra fields are event-specific.
The writer never raises — a missing parent dir, a full disk, or any
other I/O error is swallowed and logged to the redaction_failures
table the same way other best-effort hooks degrade.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from . import store


def record(
    events_log: Path,
    event: str,
    payload: dict[str, Any] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Append one event line to ``events_log``. Never raises."""
    entry: dict[str, Any] = {
        "ts": store.now_ms(),
        "event": event,
    }
    if payload:
        entry.update(payload)
    try:
        events_log.parent.mkdir(parents=True, exist_ok=True)
        with events_log.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    except (OSError, ValueError, TypeError) as e:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                store.log_redaction_failure(
                    conn, "events_log", f"{type(e).__name__}: {e}"
                )
