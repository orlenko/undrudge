"""Deterministic pipeline-outcome summaries from undrudge's durable stores."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATUSES = ("logged", "dismissed", "implemented", "dispatched", "rejected")
_SOURCES = frozenset(("claude", "codex", "shell"))
_SCOPES = frozenset(("daily", "weekly"))
_FAILURE_EVENTS = frozenset((
    "gather_failed",
    "prune_failed",
    "capability_scrape_failed",
    "capability_probe_failed",
    "capability_fetch_failed",
))
_MAX_TIMESTAMP_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _timestamp(value: Any) -> int | None:
    parsed = _integer(value)
    return parsed if parsed is not None and 0 <= parsed <= _MAX_TIMESTAMP_MS else None


def _gather_outcome(entry: dict[str, Any]) -> dict[str, Any]:
    failed = entry.get("failed_sources")
    failed_sources: list[str] = []
    if isinstance(failed, list):
        for source in failed:
            safe_source = (
                source if isinstance(source, str) and source in _SOURCES else "unknown"
            )
            if safe_source not in failed_sources:
                failed_sources.append(safe_source)
    else:
        # Only an explicit empty list proves a clean terminal run. Older or
        # malformed shapes must not silently turn an unknown outcome green.
        failed_sources.append("unknown")
    return {
        "ts": entry["ts"],
        "legacy_failure": False,
        "failed_sources": failed_sources,
        "rows": {
            source: _integer(entry.get(f"{source}_rows"))
            for source in ("claude", "codex", "shell")
        },
    }


def _legacy_gather_failure(entry: dict[str, Any]) -> dict[str, Any]:
    source = entry.get("source")
    safe_source = source if isinstance(source, str) and source in _SOURCES else "unknown"
    return {
        "ts": entry["ts"],
        "legacy_failure": True,
        "failed_sources": [safe_source],
        "rows": {source: None for source in ("claude", "codex", "shell")},
    }


def _analyze_outcome(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": entry["ts"],
        "parsed": _integer(entry.get("parsed")),
        "written": _integer(entry.get("written")),
        "skipped": _integer(entry.get("skipped")),
    }


def iter_events(events_log: Path) -> Iterator[dict[str, Any] | None]:
    """Yield sanitized event objects, or ``None`` for each malformed line.

    Reading bytes keeps a partial multibyte tail inside the per-line error
    boundary. Timestamp validation also lives here so every audit-log reader
    agrees on which events are eligible for ordering and cutoff checks.
    """
    with events_log.open("rb") as fp:
        for line in fp:
            try:
                entry = json.loads(line)
            except (UnicodeDecodeError, ValueError):
                yield None
                continue
            if not isinstance(entry, dict):
                yield None
                continue
            ts = _timestamp(entry.get("ts"))
            event = entry.get("event")
            if ts is None or not isinstance(event, str):
                yield None
                continue
            entry["ts"] = ts
            yield entry


@dataclass
class GatherReducer:
    """Select the latest terminal gather outcome by timestamp.

    ``gather_failed`` was the only signal in older logs, so it remains a
    fallback when the selected window has no ``gather_complete``. Once a
    completion exists it is authoritative: failures are per-source events
    emitted before that terminal record and only contribute to the transient
    count. Append order is deliberately irrelevant.
    """

    cutoff_ms: int | None = None
    latest_complete: dict[str, Any] | None = None
    latest_legacy_failure: dict[str, Any] | None = None
    failure_events: int = 0

    def add(self, entry: dict[str, Any]) -> None:
        ts = entry["ts"]
        if self.cutoff_ms is not None and ts < self.cutoff_ms:
            return
        event = entry["event"]
        if event == "gather_complete":
            outcome = _gather_outcome(entry)
            if self.latest_complete is None or ts >= self.latest_complete["ts"]:
                self.latest_complete = outcome
        elif event == "gather_failed":
            self.failure_events += 1
            outcome = _legacy_gather_failure(entry)
            if (
                self.latest_legacy_failure is None
                or ts >= self.latest_legacy_failure["ts"]
            ):
                self.latest_legacy_failure = outcome

    @property
    def outcome(self) -> dict[str, Any] | None:
        return self.latest_complete or self.latest_legacy_failure


def summarize_events(events_log: Path, *, cutoff_ms: int) -> dict[str, Any]:
    """Stream the audit log into latest outcomes and windowed aggregates.

    Latest stage outcomes span the whole log so a stale run remains visible.
    Counts include only events at or after ``cutoff_ms``. Raw payloads, which
    can contain paths, titles, reasons, and error text, are never returned.
    """
    latest_analyze: dict[str, dict[str, Any] | None] = {
        "daily": None,
        "weekly": None,
    }
    gather_runs = 0
    gather_failed_runs = 0
    analyze_runs: dict[str, dict[str, int]] = {
        scope: {"runs": 0, "parsed": 0, "written": 0, "skipped": 0}
        for scope in ("daily", "weekly")
    }
    recommendations_written = 0
    failures: dict[str, int] = {}
    malformed_lines = 0
    gather = GatherReducer()

    for entry in iter_events(events_log):
        if entry is None:
            malformed_lines += 1
            continue
        gather.add(entry)
        ts = entry["ts"]
        event = entry["event"]

        if event == "analyze_complete":
            scope = entry.get("scope")
            if not isinstance(scope, str) or scope not in _SCOPES:
                scope = "unknown"
            previous = latest_analyze.get(scope)
            if previous is None or ts >= previous["ts"]:
                latest_analyze[scope] = _analyze_outcome(entry)

        if ts < cutoff_ms:
            continue
        if event == "gather_complete":
            gather_runs += 1
            if _gather_outcome(entry)["failed_sources"]:
                gather_failed_runs += 1
        elif event == "analyze_complete":
            scope = entry.get("scope")
            if not isinstance(scope, str) or scope not in _SCOPES:
                scope = "unknown"
            totals = analyze_runs.setdefault(
                scope, {"runs": 0, "parsed": 0, "written": 0, "skipped": 0}
            )
            totals["runs"] += 1
            for key in ("parsed", "written", "skipped"):
                value = _integer(entry.get(key))
                if value is not None:
                    totals[key] += value
        elif event == "rec_written":
            recommendations_written += 1
        if event.endswith("_failed"):
            safe_event = event if event in _FAILURE_EVENTS else "other_failed"
            failures[safe_event] = failures.get(safe_event, 0) + 1

    return {
        "latest": {"gather": gather.outcome, "analyze": latest_analyze},
        "window": {
            "gather_runs": gather_runs,
            "gather_failed_runs": gather_failed_runs,
            "analyze": analyze_runs,
            "recommendations_written": recommendations_written,
            "failures": dict(sorted(failures.items())),
        },
        "malformed_lines": malformed_lines,
    }


def current_status_counts(db_path: Path, *, created_since_ms: int) -> dict[str, int]:
    """Count current statuses of recs surfaced in the requested window.

    Open with SQLite's read-only URI so ``health`` cannot create or migrate the
    query cache. This DB query is intentional: not every status transition is
    present in the best-effort event log, so reconstructing current state from
    JSONL would be misleading.
    """
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM recommendations "
            "WHERE created_at >= ? GROUP BY status",
            (created_since_ms,),
        ).fetchall()
    finally:
        conn.close()
    observed = {str(status): int(count) for status, count in rows}
    return {status: observed.get(status, 0) for status in STATUSES}
