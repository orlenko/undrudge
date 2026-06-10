"""Re-apply current redaction over already-stored command text.

When a secret pattern is added to ``sanitize`` (e.g. the Linear
``lin_api_`` key format the older patterns missed), rows ingested before
the pattern existed still carry the raw secret. ``rescan_commands``
re-runs the current redaction over ``commands.command`` and rewrites any
row whose text changes — closing the exposure window without a full
re-ingest.

Commands are the *kept-forever* extracted signal (unlike message text,
which the retention window is meant to phase out), so they're the
priority scrub target. Commands are not FTS-indexed, so there is no
search index to maintain here.

Re-running ``sanitize.redact`` is idempotent for text that was already
clean (or already redacted at ingest), so only rows carrying a secret a
*newly added* pattern now catches will actually change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import sanitize


@dataclass
class ScrubStats:
    scanned: int = 0
    changed: int = 0


def rescan_commands(
    conn: sqlite3.Connection, *, dry_run: bool = False
) -> ScrubStats:
    """Re-redact every stored command; rewrite rows whose text changes."""
    stats = ScrubStats()
    rows = conn.execute(
        "SELECT rowid, command FROM commands WHERE command IS NOT NULL"
    ).fetchall()
    for rowid, command in rows:
        stats.scanned += 1
        redacted = sanitize.redact(command).text
        if redacted != command:
            stats.changed += 1
            if not dry_run:
                conn.execute(
                    "UPDATE commands SET command = ? WHERE rowid = ?",
                    (redacted, rowid),
                )
    return stats
