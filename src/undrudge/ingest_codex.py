"""Ingest Codex rollout JSONL without persisting harness context.

Canonical transcripts live below ``$CODEX_HOME/sessions`` and
``$CODEX_HOME/archived_sessions``. Codex mirrors user/assistant messages across
several event shapes, so this parser deliberately chooses one canonical shape:

- main-session user/assistant text from ``event_msg``;
- post-fork subagent handoffs from ``response_item.agent_message``;
- tool calls/results combined by ``call_id``.

Subagent rollout files begin with a copied parent-context prefix. Nothing from
that prefix is ingested; the ``inter_agent_communication_metadata`` record is
the boundary after which the child transcript becomes its own activity.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import sanitize, store

logger = logging.getLogger(__name__)

_UUID_AT_END = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$", re.MULTILINE)
_MEDIA_TYPES = {"image", "input_image", "output_image"}
_MEDIA_KEYS = {"image_url", "images", "local_images"}
_SENSITIVE_PATH_KEYS = {"file_path", "notebook_path", "path"}
_SHELL_TOOL_NAMES = {"exec", "exec_command", "functions.exec", "shell_command"}
_SUBAGENT_ENVELOPE = re.compile(
    r"\A\s*Message Type:[^\n]*\n"
    r"Task name:[^\n]*\n"
    r"Sender:[^\n]*\n"
    r"Payload:\s*(?P<payload>[\s\S]*?)\s*\Z"
)


@dataclass
class CodexIngestStats:
    files_seen: int = 0
    files_skipped: int = 0
    lines_read: int = 0
    lines_skipped: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    sessions_touched: int = 0
    redaction_drops: int = 0
    bytes_consumed: int = 0


@dataclass
class _FileState:
    raw_session_id: str | None
    db_session_id: str | None
    project: str | None
    started_at: int | None
    last_seen: int | None
    next_seq: int
    metadata_seen: bool
    is_subagent: bool
    past_boundary: bool


def _parse_iso(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        return int(
            datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC).timestamp() * 1000
        )
    except (TypeError, ValueError):
        return None


def _session_id_from_path(path: Path) -> str | None:
    match = _UUID_AT_END.search(path.name)
    return match.group(1) if match else None


def _stable_id(*parts: object) -> str:
    payload = "\0".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode()).hexdigest()


def _db_session_id(raw_session_id: str) -> str:
    return _stable_id("codex", "session", raw_session_id)


def _message_id(raw_session_id: str, raw_digest: str, kind: str, piece: int) -> str:
    # A rollout can be truncated and rewritten with records shifted to new byte
    # offsets. The record digest already includes its timestamp and payload, so
    # excluding the offset keeps identical activity idempotent after rewrites.
    return _stable_id("codex", raw_session_id, raw_digest, kind, piece)


def _tool_id(raw_session_id: str, call_id: str) -> str:
    return _stable_id("codex", raw_session_id, "call", call_id)


def _read_cursor(conn: sqlite3.Connection, source: str) -> dict[str, Any]:
    row = conn.execute("SELECT position FROM cursors WHERE source = ?", (source,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["position"])
    except (json.JSONDecodeError, TypeError):
        return {}


def _write_cursor(conn: sqlite3.Connection, source: str, state: _FileState, offset: int) -> None:
    position = {
        "offset": offset,
        "session_id": state.raw_session_id,
        "metadata_seen": state.metadata_seen,
        "is_subagent": state.is_subagent,
        "past_boundary": state.past_boundary,
    }
    conn.execute(
        """INSERT INTO cursors(source, position, updated_at)
              VALUES (?, ?, ?)
           ON CONFLICT(source) DO UPDATE SET
              position = excluded.position,
              updated_at = excluded.updated_at""",
        (source, json.dumps(position), store.now_ms()),
    )


def _load_file_state(conn: sqlite3.Connection, path: Path, cursor: dict[str, Any]) -> _FileState:
    raw_id = cursor.get("session_id") or _session_id_from_path(path)
    db_id = _db_session_id(raw_id) if raw_id else None
    row = None
    next_seq = 0
    if db_id:
        row = conn.execute(
            "SELECT project, started_at, last_seen FROM sessions WHERE id = ?",
            (db_id,),
        ).fetchone()
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_id = ?",
            (db_id,),
        ).fetchone()
        next_seq = int(seq_row[0]) if seq_row else 0
    return _FileState(
        raw_session_id=raw_id,
        db_session_id=db_id,
        project=row["project"] if row else None,
        started_at=row["started_at"] if row else None,
        last_seen=row["last_seen"] if row else None,
        next_seq=next_seq,
        metadata_seen=bool(cursor.get("metadata_seen")),
        is_subagent=bool(cursor.get("is_subagent")),
        past_boundary=bool(cursor.get("past_boundary")),
    )


def _ensure_session_identity(state: _FileState, raw_id: str | None) -> bool:
    if state.raw_session_id is None and raw_id:
        state.raw_session_id = raw_id
        state.db_session_id = _db_session_id(raw_id)
    return state.raw_session_id is not None and state.db_session_id is not None


def _touch_state(state: _FileState, ts: int | None) -> None:
    if ts is None:
        return
    if state.started_at is None or ts < state.started_at:
        state.started_at = ts
    if state.last_seen is None or ts > state.last_seen:
        state.last_seen = ts


def _upsert_session(conn: sqlite3.Connection, state: _FileState) -> None:
    if not state.db_session_id:
        return
    conn.execute(
        """INSERT INTO sessions(id, source, project, started_at, last_seen)
              VALUES (?, 'codex', ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
              source = 'codex',
              project = COALESCE(excluded.project, sessions.project),
              started_at = COALESCE(
                  MIN(sessions.started_at, excluded.started_at), excluded.started_at
              ),
              last_seen = MAX(
                  COALESCE(sessions.last_seen, 0), COALESCE(excluded.last_seen, 0)
              )""",
        (state.db_session_id, state.project, state.started_at, state.last_seen),
    )


def _canonical_metadata(state: _FileState, payload: dict[str, Any]) -> None:
    if state.metadata_seen:
        return
    _ensure_session_identity(state, payload.get("id"))
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        state.project = cwd
    source = payload.get("source")
    state.is_subagent = bool(
        payload.get("thread_source") == "subagent"
        or payload.get("parent_thread_id")
        or payload.get("agent_path")
        or (isinstance(source, dict) and "subagent" in source)
    )
    # Main sessions start immediately. Children start only after their copied
    # parent prefix reaches inter_agent_communication_metadata.
    state.past_boundary = not state.is_subagent
    state.metadata_seen = True


def _sensitive_path(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and sanitize.is_excluded_file(key):
                return key
            if (
                key in _SENSITIVE_PATH_KEYS
                and isinstance(item, str)
                and sanitize.is_excluded_file(item)
            ):
                return item
            found = _sensitive_path(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _sensitive_path(item)
            if found:
                return found
    return None


def _excluded_command_path(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"cmd", "command"}:
                found = _excluded_command_path(item)
                if found:
                    return found
        return None
    if isinstance(value, list):
        for item in value:
            found = _excluded_command_path(item)
            if found:
                return found
        return None
    if not isinstance(value, str):
        return None
    try:
        candidates = shlex.split(value)
    except ValueError:
        candidates = value.split()
    for candidate in candidates:
        path = candidate.strip("\"'`()[]{}<>|&;,:")
        if path and sanitize.is_excluded_file(path):
            return path
    return None


def _redact_value(value: Any, *, field_name: str) -> Any:
    """Recursively sanitize JSON-like values and discard binary/encrypted data."""
    if isinstance(value, str):
        return sanitize.redact(value, field_name=field_name).text
    if isinstance(value, list):
        return [_redact_value(item, field_name=f"{field_name}[]") for item in value]
    if not isinstance(value, dict):
        return value

    if value.get("type") in _MEDIA_TYPES:
        return "[image]"
    excluded = _sensitive_path(value)
    if excluded:
        return {"file_path": excluded, "content": sanitize.REDACTED_FILE}

    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if key == "encrypted_content":
            cleaned[key] = "[encrypted]"
        elif key in _MEDIA_KEYS:
            cleaned[key] = "[image]" if item else []
        else:
            cleaned[key] = _redact_value(item, field_name=f"{field_name}.{key}")
    return cleaned


def _sanitize_tool_input(tool_name: str, value: Any) -> str:
    parsed = value
    if isinstance(value, str):
        if tool_name == "apply_patch":
            for path in _PATCH_FILE.findall(value):
                if sanitize.is_excluded_file(path):
                    parsed = {"file_path": path, "content": sanitize.REDACTED_FILE}
                    break
        if parsed is value:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = value
    if tool_name in _SHELL_TOOL_NAMES:
        excluded = _excluded_command_path(parsed)
        if excluded:
            parsed = {"file_path": excluded, "command": sanitize.REDACTED_FILE}
    cleaned = _redact_value(parsed, field_name=f"messages.tool_input.{tool_name}")
    return json.dumps(cleaned, ensure_ascii=False, default=str)


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") in {"input_text", "output_text"}:
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, dict) and item.get("type") in _MEDIA_TYPES:
                parts.append("[image]")
            else:
                parts.append(json.dumps(item, ensure_ascii=False, default=str))
        return "\n".join(part for part in parts if part)
    return json.dumps(value, ensure_ascii=False, default=str)


def _subagent_handoff_text(value: str) -> str:
    """Strip Codex's routing envelope and discard encrypted-only handoffs."""
    match = _SUBAGENT_ENVELOPE.fullmatch(value)
    return match.group("payload").strip() if match else value


def _sanitize_tool_output(tool_name: str, value: Any) -> str:
    cleaned = _redact_value(value, field_name=f"messages.tool_result.{tool_name}")
    return sanitize.redact(
        _content_text(cleaned), field_name=f"messages.tool_result.{tool_name}"
    ).text


def _record_drop(
    conn: sqlite3.Connection, stats: CodexIngestStats, kind: str, exc: Exception
) -> None:
    store.log_redaction_failure(conn, "codex_jsonl", f"{kind}: {type(exc).__name__}: {exc}")
    stats.redaction_drops += 1


def _insert_text(
    conn: sqlite3.Connection,
    state: _FileState,
    stats: CodexIngestStats,
    *,
    row_id: str,
    ts: int,
    role: str,
    text: str,
) -> None:
    if not state.db_session_id:
        stats.lines_skipped += 1
        return
    try:
        cleaned = sanitize.redact(text, field_name="messages.text").text
    except Exception as exc:
        _record_drop(conn, stats, "text", exc)
        return
    if not cleaned:
        return
    _upsert_session(conn, state)
    cur = conn.execute(
        """INSERT OR IGNORE INTO messages
               (id, session_id, seq, ts, role, text, origin)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            row_id,
            state.db_session_id,
            state.next_seq,
            ts,
            role,
            cleaned,
            "subagent" if state.is_subagent else "main",
        ),
    )
    if cur.rowcount > 0:
        state.next_seq += 1
        stats.rows_inserted += 1


def _upsert_tool(
    conn: sqlite3.Connection,
    state: _FileState,
    stats: CodexIngestStats,
    *,
    call_id: str,
    ts: int,
    tool_name: str,
    raw_input: Any = None,
    raw_output: Any = None,
    is_error: bool | None = None,
) -> None:
    if not state.raw_session_id or not state.db_session_id:
        stats.lines_skipped += 1
        return
    try:
        tool_input = _sanitize_tool_input(tool_name, raw_input) if raw_input is not None else None
        tool_result = (
            _sanitize_tool_output(tool_name, raw_output) if raw_output is not None else None
        )
    except Exception as exc:
        _record_drop(conn, stats, f"tool:{tool_name}", exc)
        return

    _upsert_session(conn, state)
    row_id = _tool_id(state.raw_session_id, call_id)
    existing = conn.execute(
        """SELECT ts, tool_name, tool_input, tool_result, is_error
             FROM messages WHERE id = ?""",
        (row_id,),
    ).fetchone()
    effective_input = (
        tool_input if tool_input is not None else (existing["tool_input"] if existing else None)
    )
    if (
        effective_input
        and sanitize.REDACTED_FILE in effective_input
        and (tool_result is not None or (existing and existing["tool_result"] is not None))
    ):
        # Once any tool call is known to target an excluded file, its result is
        # untrustworthy regardless of tool name or call/output arrival order.
        tool_result = sanitize.REDACTED_FILE
    origin = "subagent" if state.is_subagent else "main"
    if not existing:
        conn.execute(
            """INSERT INTO messages
                   (id, session_id, seq, ts, role, tool_name, tool_input,
                    tool_result, is_error, origin)
               VALUES (?, ?, ?, ?, 'tool', ?, ?, ?, ?, ?)""",
            (
                row_id,
                state.db_session_id,
                state.next_seq,
                ts,
                tool_name,
                tool_input,
                tool_result,
                int(bool(is_error)),
                origin,
            ),
        )
        state.next_seq += 1
        stats.rows_inserted += 1
        return

    updated = (
        min(existing["ts"], ts),
        tool_name or existing["tool_name"],
        tool_input if tool_input is not None else existing["tool_input"],
        tool_result if tool_result is not None else existing["tool_result"],
        int(is_error) if is_error is not None else existing["is_error"],
    )
    before = (
        existing["ts"],
        existing["tool_name"],
        existing["tool_input"],
        existing["tool_result"],
        existing["is_error"],
    )
    if updated == before:
        return
    conn.execute(
        """UPDATE messages
              SET ts = ?, tool_name = ?, tool_input = ?, tool_result = ?, is_error = ?
            WHERE id = ?""",
        (*updated, row_id),
    )
    stats.rows_updated += 1


def _tool_end(
    conn: sqlite3.Connection,
    state: _FileState,
    stats: CodexIngestStats,
    payload: dict[str, Any],
    *,
    offset: int,
    ts: int,
) -> bool:
    event_type = payload.get("type")
    call_id = str(payload.get("call_id") or f"offset:{offset}:{event_type}")
    if event_type == "exec_command_end":
        output = payload.get("formatted_output") or payload.get("aggregated_output")
        if output is None:
            output = {"stdout": payload.get("stdout"), "stderr": payload.get("stderr")}
        exit_code = payload.get("exit_code")
        is_error = (isinstance(exit_code, int) and exit_code != 0) or str(
            payload.get("status") or ""
        ).lower() in {"failed", "error"}
        _upsert_tool(
            conn,
            state,
            stats,
            call_id=call_id,
            ts=ts,
            tool_name="exec_command",
            raw_input={"command": payload.get("command"), "cwd": payload.get("cwd")},
            raw_output=output,
            is_error=is_error,
        )
        return True
    if event_type == "patch_apply_end":
        _upsert_tool(
            conn,
            state,
            stats,
            call_id=call_id,
            ts=ts,
            tool_name="apply_patch",
            raw_input={"changes": payload.get("changes")},
            raw_output={"stdout": payload.get("stdout"), "stderr": payload.get("stderr")},
            is_error=not bool(payload.get("success")),
        )
        return True
    if event_type == "mcp_tool_call_end":
        invocation = payload.get("invocation") or {}
        server = invocation.get("server") if isinstance(invocation, dict) else None
        tool = invocation.get("tool") if isinstance(invocation, dict) else None
        name = ".".join(str(part) for part in (server, tool) if part) or "mcp_tool"
        result = payload.get("result")
        ok_result = result.get("Ok") if isinstance(result, dict) else None
        is_error = isinstance(result, dict) and (
            "Err" in result
            or bool(ok_result.get("isError") if isinstance(ok_result, dict) else False)
        )
        _upsert_tool(
            conn,
            state,
            stats,
            call_id=call_id,
            ts=ts,
            tool_name=name,
            raw_input=invocation,
            raw_output=result,
            is_error=is_error,
        )
        return True
    if event_type == "web_search_end":
        _upsert_tool(
            conn,
            state,
            stats,
            call_id=call_id,
            ts=ts,
            tool_name="web_search",
            raw_input={"query": payload.get("query"), "action": payload.get("action")},
            is_error=False,
        )
        return True
    return False


def _ingest_response_item(
    conn: sqlite3.Connection,
    state: _FileState,
    stats: CodexIngestStats,
    payload: dict[str, Any],
    *,
    offset: int,
    raw_digest: str,
    ts: int,
) -> None:
    kind = payload.get("type")
    if kind == "agent_message" and state.is_subagent:
        piece = 0
        for item in payload.get("content") or []:
            if not isinstance(item, dict) or item.get("type") != "input_text":
                continue
            text = _subagent_handoff_text(str(item.get("text") or ""))
            if not text:
                continue
            _insert_text(
                conn,
                state,
                stats,
                row_id=_message_id(state.raw_session_id or "unknown", raw_digest, kind, piece),
                ts=ts,
                role="user",
                text=text,
            )
            piece += 1
        return
    if kind in {"function_call", "custom_tool_call"}:
        call_id = str(payload.get("call_id") or f"offset:{offset}:{kind}")
        name = str(payload.get("name") or "unknown_tool")
        raw_input = payload.get("arguments") if kind == "function_call" else payload.get("input")
        _upsert_tool(
            conn,
            state,
            stats,
            call_id=call_id,
            ts=ts,
            tool_name=name,
            raw_input=raw_input,
        )
        return
    if kind in {"function_call_output", "custom_tool_call_output"}:
        call_id = str(payload.get("call_id") or f"offset:{offset}:{kind}")
        row = None
        if state.raw_session_id:
            row = conn.execute(
                "SELECT tool_name FROM messages WHERE id = ?",
                (_tool_id(state.raw_session_id, call_id),),
            ).fetchone()
        _upsert_tool(
            conn,
            state,
            stats,
            call_id=call_id,
            ts=ts,
            tool_name=row["tool_name"] if row else "unknown_tool",
            raw_output=payload.get("output"),
        )


def _ingest_line(
    conn: sqlite3.Connection,
    state: _FileState,
    stats: CodexIngestStats,
    raw: bytes,
    *,
    offset: int,
) -> None:
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        stats.lines_skipped += 1
        return
    if not isinstance(record, dict):
        stats.lines_skipped += 1
        return

    top_type = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return
    ts = _parse_iso(record.get("timestamp")) or 0

    if top_type == "session_meta":
        if state.metadata_seen:
            # Some rollouts append metadata copied from another thread. The
            # filename-matching first metadata record remains canonical.
            return
        _canonical_metadata(state, payload)
        _touch_state(state, _parse_iso(payload.get("timestamp")) or ts)
        _upsert_session(conn, state)
        return

    if not state.metadata_seen:
        # Rollouts normally begin with session_meta. A filename UUID still lets
        # us ingest forward-compatible files whose metadata shape changed.
        _ensure_session_identity(state, None)
        state.metadata_seen = state.raw_session_id is not None
        state.past_boundary = state.metadata_seen
    if not state.raw_session_id:
        stats.lines_skipped += 1
        return

    if state.is_subagent and not state.past_boundary:
        if top_type == "inter_agent_communication_metadata":
            state.past_boundary = True
        return

    _touch_state(state, ts)
    _upsert_session(conn, state)
    raw_digest = hashlib.sha256(raw).hexdigest()

    if top_type == "event_msg":
        kind = payload.get("type")
        if kind in {"user_message", "agent_message"}:
            text = payload.get("message")
            if isinstance(text, str):
                if kind == "user_message" and (
                    payload.get("images") or payload.get("local_images")
                ):
                    text += "\n[image]"
                _insert_text(
                    conn,
                    state,
                    stats,
                    row_id=_message_id(state.raw_session_id, raw_digest, str(kind), 0),
                    ts=ts,
                    role="user" if kind == "user_message" else "assistant",
                    text=text,
                )
            return
        _tool_end(conn, state, stats, payload, offset=offset, ts=ts)
        return

    if top_type == "response_item":
        _ingest_response_item(
            conn,
            state,
            stats,
            payload,
            offset=offset,
            raw_digest=raw_digest,
            ts=ts,
        )


def ingest(
    conn: sqlite3.Connection,
    codex_home: Path,
    *,
    fail_loud: bool = True,
) -> CodexIngestStats:
    """Ingest new bytes from active and archived Codex rollout files."""
    # Privacy is fail-closed regardless of this legacy compatibility flag.
    # Keep the argument parallel with the other ingest functions/config.
    _ = fail_loud
    stats = CodexIngestStats()
    roots = (codex_home / "sessions", codex_home / "archived_sessions")
    paths = sorted({path for root in roots if root.exists() for path in root.rglob("*.jsonl")})
    if not paths:
        logger.info("codex session roots missing or empty under %s — skipping", codex_home)
        return stats

    logger.info("scanning codex sessions under %s", codex_home)
    for path in paths:
        stats.files_seen += 1
        source = f"codex:{path}"
        cursor = _read_cursor(conn, source)
        offset = int(cursor.get("offset", 0))
        state = _load_file_state(conn, path, cursor)
        try:
            file_size = path.stat().st_size
        except OSError as exc:
            logger.debug("stat failed for %s: %s — skipping", path, exc)
            stats.files_skipped += 1
            continue
        if file_size < offset:
            logger.debug(
                "%s: file shrank (size=%d < cursor=%d), re-ingesting from 0",
                path,
                file_size,
                offset,
            )
            offset = 0
            state = _load_file_state(conn, path, {})
        if file_size == offset:
            continue

        final = offset
        lines_before = stats.lines_read
        inserted_before = stats.rows_inserted
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                while True:
                    line_offset = stream.tell()
                    raw = stream.readline()
                    if not raw:
                        final = stream.tell()
                        break
                    if not raw.endswith(b"\n"):
                        # Codex may still be writing this record. Retry from the
                        # start of the line on the next gather.
                        final = line_offset
                        break
                    stats.lines_read += 1
                    _ingest_line(conn, state, stats, raw, offset=line_offset)
                    final = stream.tell()
        except OSError as exc:
            logger.debug("read failed for %s: %s — skipping", path, exc)
            stats.files_skipped += 1
            continue

        _upsert_session(conn, state)
        _write_cursor(conn, source, state, final)
        stats.bytes_consumed += final - offset
        logger.info(
            "%s: +%d lines, +%d rows, cursor=%d",
            path.name,
            stats.lines_read - lines_before,
            stats.rows_inserted - inserted_before,
            final,
        )

    stats.sessions_touched = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE source = 'codex'"
    ).fetchone()[0]
    logger.info(
        "codex ingest done: files=%d (skipped=%d) lines=%d rows=%d "
        "updated=%d sessions=%d redaction_drops=%d",
        stats.files_seen,
        stats.files_skipped,
        stats.lines_read,
        stats.rows_inserted,
        stats.rows_updated,
        stats.sessions_touched,
        stats.redaction_drops,
    )
    return stats
