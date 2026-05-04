"""Ingest Claude session JSONL files.

Source of truth: ``~/.claude/projects/**/*.jsonl``. Each file is one session;
each line is one event. We care about ``type ∈ {user, assistant}``; the rest
(``permission-mode``, ``file-history-snapshot``, etc.) is operational noise
that's safe to skip.

Resumability is per-file byte offsets stored in ``cursors``. JSONL is
append-only; if a file shrinks, we reset its cursor and re-ingest. The
``messages.id`` PK is ``{uuid}#{piece_index}`` so multi-content messages
deduplicate cleanly on re-run.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import sanitize, store


@dataclass
class ClaudeIngestStats:
    files_seen: int = 0
    files_skipped: int = 0
    lines_read: int = 0
    lines_skipped: int = 0
    rows_inserted: int = 0
    sessions_touched: int = 0
    redaction_drops: int = 0
    bytes_consumed: int = 0


def _parse_iso(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        # Python 3.11+ understands trailing Z.
        s = ts.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).astimezone(UTC).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _flatten(content: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (kind, payload) for each addressable piece of content.

    kind ∈ {'text', 'thinking', 'tool_use', 'tool_result', 'other'}.
    For string content (plain user prompts), yields a single ('text', {...}).
    """
    if isinstance(content, str):
        yield "text", {"text": content}
        return
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "text":
            yield "text", {"text": item.get("text", "")}
        elif t == "thinking":
            yield "thinking", {"text": item.get("thinking", "")}
        elif t == "tool_use":
            yield "tool_use", {
                "name": item.get("name") or "?",
                "input": item.get("input") or {},
                "tool_use_id": item.get("id"),
            }
        elif t == "tool_result":
            yield "tool_result", {
                "tool_use_id": item.get("tool_use_id"),
                "content": item.get("content"),
                "is_error": bool(item.get("is_error")),
            }
        else:
            yield "other", {"raw_type": t}


def _serialize_tool_result(content: Any) -> str:
    """Best-effort string form of a tool_result body for sanitization+storage.

    If it's a list of content items, extract the text-bearing parts and join.
    Otherwise json.dumps the whole thing. Sanitization runs on the result.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(json.dumps(item, default=str))
                continue
            it = item.get("type")
            if it == "text":
                parts.append(item.get("text", ""))
            elif it == "tool_reference":
                parts.append(f"[tool_reference: {item.get('tool_name', '?')}]")
            elif it == "image":
                parts.append("[image]")
            else:
                parts.append(json.dumps(item, default=str))
        return "\n".join(parts)
    return json.dumps(content, default=str)


@dataclass
class _SessionState:
    project: str | None
    started_at: int | None
    last_seen: int | None
    next_seq: int


def _load_session_state(conn: sqlite3.Connection, session_id: str) -> _SessionState:
    row = conn.execute(
        "SELECT project, started_at, last_seen FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    next_seq_row = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    next_seq = int(next_seq_row[0]) if next_seq_row else 0
    if row:
        return _SessionState(row["project"], row["started_at"], row["last_seen"], next_seq)
    return _SessionState(None, None, None, next_seq)


def _upsert_session(conn: sqlite3.Connection, session_id: str, state: _SessionState) -> None:
    conn.execute(
        """INSERT INTO sessions(id, project, started_at, last_seen)
              VALUES (?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
              project    = COALESCE(excluded.project, sessions.project),
              started_at = COALESCE(MIN(sessions.started_at, excluded.started_at), excluded.started_at),
              last_seen  = MAX(COALESCE(sessions.last_seen, 0), COALESCE(excluded.last_seen, 0))""",
        (session_id, state.project, state.started_at, state.last_seen),
    )


def _read_cursor(conn: sqlite3.Connection, source: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT position FROM cursors WHERE source = ?", (source,)
    ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["position"])
    except json.JSONDecodeError:
        return {}


def _write_cursor(conn: sqlite3.Connection, source: str, position: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO cursors(source, position, updated_at)
              VALUES (?, ?, ?)
           ON CONFLICT(source) DO UPDATE SET
              position   = excluded.position,
              updated_at = excluded.updated_at""",
        (source, json.dumps(position), store.now_ms()),
    )


def _ingest_line(
    conn: sqlite3.Connection,
    raw: str,
    sessions: dict[str, _SessionState],
    stats: ClaudeIngestStats,
    *,
    fail_loud: bool,
) -> None:
    raw = raw.strip()
    if not raw:
        return
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        stats.lines_skipped += 1
        return

    t = d.get("type")
    if t not in {"user", "assistant"}:
        return

    session_id = d.get("sessionId")
    uuid = d.get("uuid")
    if not session_id or not uuid:
        stats.lines_skipped += 1
        return

    ts = _parse_iso(d.get("timestamp"))
    state = sessions.get(session_id) or _load_session_state(conn, session_id)
    sessions[session_id] = state

    cwd = d.get("cwd")
    if cwd and not state.project:
        state.project = cwd
    if ts is not None:
        if state.started_at is None or ts < state.started_at:
            state.started_at = ts
        if state.last_seen is None or ts > state.last_seen:
            state.last_seen = ts

    # Ensure the session row exists before we insert any FK-bound messages.
    _upsert_session(conn, session_id, state)

    msg = d.get("message") or {}
    role = msg.get("role") or t

    pieces = list(_flatten(msg.get("content")))
    if not pieces:
        return

    for idx, (kind, payload) in enumerate(pieces):
        message_id = f"{uuid}#{idx}"
        text: str | None = None
        tool_name: str | None = None
        tool_input: str | None = None
        tool_result: str | None = None
        is_error = 0
        row_role = role

        try:
            if kind == "text":
                r = sanitize.redact(payload.get("text") or "", field_name="messages.text")
                text = r.text
            elif kind == "thinking":
                r = sanitize.redact(payload.get("text") or "", field_name="messages.thinking")
                text = "[thinking] " + r.text if r.text else "[thinking]"
            elif kind == "tool_use":
                tool_name = payload["name"]
                cleaned, _ = sanitize.redact_tool_input(tool_name, payload["input"])
                tool_input = json.dumps(cleaned, default=str)
            elif kind == "tool_result":
                row_role = "tool"
                serial = _serialize_tool_result(payload["content"])
                r = sanitize.redact(serial, field_name="messages.tool_result")
                tool_result = r.text
                is_error = 1 if payload.get("is_error") else 0
            else:
                # 'other' — store as a marker so we can spot unknown event types later.
                text = f"[unhandled-content-type: {payload.get('raw_type')}]"
        except Exception as e:
            store.log_redaction_failure(
                conn, "claude_jsonl", f"{kind}: {type(e).__name__}: {e}"
            )
            stats.redaction_drops += 1
            if fail_loud:
                continue  # drop row; never store unredacted
            else:
                continue

        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO messages
                       (id, session_id, seq, ts, role, text, tool_name, tool_input, tool_result, is_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    session_id,
                    state.next_seq,
                    ts or 0,
                    row_role,
                    text,
                    tool_name,
                    tool_input,
                    tool_result,
                    is_error,
                ),
            )
            if cur.rowcount > 0:
                stats.rows_inserted += 1
                state.next_seq += 1
        except sqlite3.IntegrityError as e:
            stats.lines_skipped += 1
            store.log_redaction_failure(conn, "claude_jsonl", f"insert: {e}")


def ingest(
    conn: sqlite3.Connection,
    projects_root: Path,
    *,
    fail_loud: bool = True,
) -> ClaudeIngestStats:
    """Walk ``projects_root`` for new JSONL bytes; load into the DB.

    Idempotent: a re-run after a partial failure picks up where the cursor
    left off. ``INSERT OR IGNORE`` on ``messages.id`` makes mid-line crashes
    safe — a duplicated row is silently skipped.
    """
    stats = ClaudeIngestStats()
    if not projects_root.exists():
        return stats

    sessions: dict[str, _SessionState] = {}

    for jsonl_path in sorted(projects_root.rglob("*.jsonl")):
        stats.files_seen += 1
        source = f"claude:{jsonl_path}"
        cursor = _read_cursor(conn, source)
        offset = int(cursor.get("offset", 0))

        try:
            file_size = jsonl_path.stat().st_size
        except OSError:
            stats.files_skipped += 1
            continue

        if file_size < offset:
            # Truncation/rotation: reset and re-ingest from the start.
            offset = 0

        if file_size == offset:
            continue

        try:
            with jsonl_path.open("rb") as f:
                f.seek(offset)
                for raw in f:
                    stats.lines_read += 1
                    text = raw.decode("utf-8", errors="replace")
                    _ingest_line(conn, text, sessions, stats, fail_loud=fail_loud)
                final = f.tell()
        except OSError:
            stats.files_skipped += 1
            continue

        for sid, state in sessions.items():
            _upsert_session(conn, sid, state)
        sessions.clear()

        _write_cursor(conn, source, {"offset": final})
        stats.bytes_consumed += final - offset

    stats.sessions_touched = conn.execute(
        "SELECT COUNT(*) FROM sessions"
    ).fetchone()[0]
    return stats
