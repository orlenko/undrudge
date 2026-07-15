"""Codex rollout ingestion tests built entirely from synthetic JSONL."""

from __future__ import annotations

import json
from pathlib import Path

from undrudge import config, digest, ingest_codex


def _record(timestamp: str, top_type: str, payload: dict) -> dict:
    return {"timestamp": timestamp, "type": top_type, "payload": payload}


def _rollout(
    home: Path,
    session_id: str,
    records: list[dict],
    *,
    archived: bool = False,
) -> Path:
    parent = home / "archived_sessions" if archived else home / "sessions" / "2026" / "07" / "15"
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / f"rollout-2026-07-15T12-00-00-{session_id}.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def _meta(session_id: str, **extra) -> dict:
    payload = {
        "id": session_id,
        "timestamp": "2026-07-15T16:00:00.000Z",
        "cwd": "/Users/fake/repo",
        "source": "cli",
        **extra,
    }
    return _record("2026-07-15T16:00:00.000Z", "session_meta", payload)


def test_codex_ingest_uses_canonical_messages_and_sanitizes_tools(db, tmp_path: Path):
    home = tmp_path / "codex"
    sid = "11111111-2222-7333-8444-555555555555"
    secret = "sk-" + "A" * 32
    records = [
        _meta(sid),
        # Mirrored/injected response messages must not become human prompts.
        _record(
            "2026-07-15T16:00:01.000Z",
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "real human prompt"},
                    {
                        "type": "input_text",
                        "text": "<environment_context>noise</environment_context>",
                    },
                ],
            },
        ),
        _record(
            "2026-07-15T16:00:01.001Z",
            "event_msg",
            {"type": "user_message", "message": "real human prompt", "images": []},
        ),
        _record(
            "2026-07-15T16:00:02.000Z",
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "assistant answer"}],
            },
        ),
        _record(
            "2026-07-15T16:00:02.001Z",
            "event_msg",
            {"type": "agent_message", "message": "assistant answer"},
        ),
        _record(
            "2026-07-15T16:00:03.000Z",
            "response_item",
            {
                "type": "function_call",
                "name": "nested_tool",
                "call_id": "call-secret",
                "arguments": json.dumps({"outer": {"api_key": secret}}),
            },
        ),
        _record(
            "2026-07-15T16:00:03.100Z",
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call-secret",
                "output": [{"type": "input_text", "text": f"token={secret}"}],
            },
        ),
        _record(
            "2026-07-15T16:00:04.000Z",
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": "call-patch",
                "input": (
                    "*** Begin Patch\n*** Update File: .env\n@@\n"
                    f"-TOKEN=old\n+TOKEN={secret}\n*** End Patch"
                ),
            },
        ),
        # Developer/reasoning records are deliberately ignored.
        _record(
            "2026-07-15T16:00:05.000Z",
            "response_item",
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "policy"}],
            },
        ),
        _record(
            "2026-07-15T16:00:06.000Z",
            "response_item",
            {"type": "reasoning", "encrypted_content": secret},
        ),
    ]
    _rollout(home, sid, records)

    stats = ingest_codex.ingest(db, home)

    assert stats.rows_inserted == 4  # user + assistant + two combined tool rows
    rows = db.execute(
        "SELECT role, text, tool_name, tool_input, tool_result FROM messages ORDER BY seq"
    ).fetchall()
    assert [row["role"] for row in rows] == ["user", "assistant", "tool", "tool"]
    assert [row["text"] for row in rows[:2]] == ["real human prompt", "assistant answer"]
    assert "environment_context" not in "\n".join(row["text"] or "" for row in rows)
    haystack = "\n".join(
        str(row[key] or "") for row in rows for key in ("text", "tool_input", "tool_result")
    )
    assert secret not in haystack
    patch = next(row for row in rows if row["tool_name"] == "apply_patch")
    assert "REDACTED:sensitive-file" in patch["tool_input"]
    session = db.execute("SELECT source, project FROM sessions").fetchone()
    assert (session["source"], session["project"]) == ("codex", "/Users/fake/repo")


def test_codex_subagent_drops_copied_prefix_and_empty_handoff_envelope(db, tmp_path: Path):
    home = tmp_path / "codex"
    sid = "22222222-3333-7444-8555-666666666666"
    records = [
        _meta(
            sid,
            thread_source="subagent",
            parent_thread_id="11111111-2222-7333-8444-555555555555",
            agent_path="/root/reviewer",
            source={"subagent": {"thread_spawn": {"depth": 1}}},
        ),
        _record(
            "2026-07-15T16:00:01.000Z",
            "event_msg",
            {"type": "user_message", "message": "copied parent prompt"},
        ),
        _record(
            "2026-07-15T16:00:02.000Z",
            "event_msg",
            {"type": "agent_message", "message": "copied parent answer"},
        ),
        _record(
            "2026-07-15T16:00:03.000Z",
            "response_item",
            {
                "type": "function_call",
                "name": "before_boundary",
                "call_id": "old",
                "arguments": "{}",
            },
        ),
        _record(
            "2026-07-15T16:00:04.000Z",
            "inter_agent_communication_metadata",
            {"agent_path": "/root/reviewer"},
        ),
        _record(
            "2026-07-15T16:00:05.000Z",
            "response_item",
            {
                "type": "agent_message",
                "author": "root",
                "recipient": "reviewer",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Message Type: NEW_TASK\n"
                            "Task name: /root/reviewer\n"
                            "Sender: /root\n"
                            "Payload:\n"
                        ),
                    },
                    {"type": "encrypted_content", "encrypted_content": "opaque"},
                ],
            },
        ),
        _record(
            "2026-07-15T16:00:06.000Z",
            "event_msg",
            {"type": "agent_message", "message": "Found one cursor edge case"},
        ),
        _record(
            "2026-07-15T16:00:07.000Z",
            "response_item",
            {
                "type": "function_call",
                "name": "after_boundary",
                "call_id": "new",
                "arguments": "{}",
            },
        ),
    ]
    _rollout(home, sid, records)

    ingest_codex.ingest(db, home)

    rows = db.execute("SELECT role, text, tool_name, origin FROM messages ORDER BY seq").fetchall()
    assert len(rows) == 2
    assert [row["role"] for row in rows] == ["assistant", "tool"]
    assert rows[0]["text"] == "Found one cursor edge case"
    assert rows[1]["tool_name"] == "after_boundary"
    assert {row["origin"] for row in rows} == {"subagent"}


def test_codex_tool_end_updates_call_and_sets_error(db, tmp_path: Path):
    home = tmp_path / "codex"
    sid = "33333333-4444-7555-8666-777777777777"
    records = [
        _meta(sid),
        _record(
            "2026-07-15T16:00:01.000Z",
            "response_item",
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call-exec",
                "arguments": json.dumps({"cmd": "pytest -q"}),
            },
        ),
        _record(
            "2026-07-15T16:00:02.000Z",
            "event_msg",
            {
                "type": "exec_command_end",
                "call_id": "call-exec",
                "command": ["pytest", "-q"],
                "cwd": "/Users/fake/repo",
                "formatted_output": "1 failed",
                "status": "failed",
                "exit_code": 1,
            },
        ),
    ]
    _rollout(home, sid, records)

    stats = ingest_codex.ingest(db, home)

    assert (stats.rows_inserted, stats.rows_updated) == (1, 1)
    row = db.execute("SELECT tool_name, tool_input, tool_result, is_error FROM messages").fetchone()
    assert row["tool_name"] == "exec_command"
    assert "pytest" in row["tool_input"]
    assert row["tool_result"] == "1 failed"
    assert row["is_error"] == 1


def test_codex_tool_output_can_arrive_on_later_gather(db, tmp_path: Path):
    home = tmp_path / "codex"
    sid = "34343434-4545-7676-8787-898989898989"
    path = _rollout(
        home,
        sid,
        [
            _meta(sid),
            _record(
                "2026-07-15T16:00:01.000Z",
                "response_item",
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "split-call",
                    "arguments": json.dumps({"cmd": "pytest -q"}),
                },
            ),
        ],
    )
    first = ingest_codex.ingest(db, home)
    assert first.rows_inserted == 1

    with path.open("a") as stream:
        stream.write(
            json.dumps(
                _record(
                    "2026-07-15T16:00:02.000Z",
                    "response_item",
                    {
                        "type": "function_call_output",
                        "call_id": "split-call",
                        "output": "all tests passed",
                    },
                )
            )
            + "\n"
        )
    second = ingest_codex.ingest(db, home)

    assert (second.rows_inserted, second.rows_updated) == (0, 1)
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert db.execute("SELECT tool_result FROM messages").fetchone()[0] == "all tests passed"


def test_codex_shell_read_of_excluded_file_drops_entire_output(db, tmp_path: Path):
    home = tmp_path / "codex"
    sid = "34534534-5656-7787-8898-909090909091"
    private_value = "ordinary-value-that-is-not-a-known-token-pattern"
    records = [
        _meta(sid),
        _record(
            "2026-07-15T16:00:01.000Z",
            "response_item",
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "read-env",
                "arguments": json.dumps({"cmd": "cat /repo/.env"}),
            },
        ),
        _record(
            "2026-07-15T16:00:02.000Z",
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "read-env",
                "output": private_value,
            },
        ),
    ]
    _rollout(home, sid, records)

    ingest_codex.ingest(db, home)

    row = db.execute("SELECT tool_input, tool_result FROM messages").fetchone()
    assert private_value not in row["tool_result"]
    assert "REDACTED:sensitive-file" in row["tool_input"]
    assert row["tool_result"] == "[REDACTED:sensitive-file]"


def test_codex_excluded_file_suppresses_any_tool_output_in_either_order(db, tmp_path: Path):
    home = tmp_path / "codex"
    sid = "34634634-5757-7898-8909-919191919192"
    private_values = {
        "read-env": "ordinary-read-value",
        "patch-env": "ordinary-patch-value",
        "outer-exec": "ordinary-wrapper-value",
        "late-input": "ordinary-output-before-input-value",
    }
    records = [_meta(sid)]
    calls = [
        (
            "function_call",
            "Read",
            "read-env",
            "arguments",
            json.dumps({"file_path": "/repo/.env"}),
        ),
        (
            "custom_tool_call",
            "apply_patch",
            "patch-env",
            "input",
            "*** Begin Patch\n*** Update File: .env\n@@\n-old\n+new\n*** End Patch",
        ),
        (
            "custom_tool_call",
            "exec",
            "outer-exec",
            "input",
            'await tools.exec_command({cmd: "cat /repo/.env"})',
        ),
    ]
    for index, (kind, name, call_id, input_key, raw_input) in enumerate(calls, 1):
        records.extend(
            [
                _record(
                    f"2026-07-15T16:00:0{index}.000Z",
                    "response_item",
                    {"type": kind, "name": name, "call_id": call_id, input_key: raw_input},
                ),
                _record(
                    f"2026-07-15T16:00:0{index}.100Z",
                    "response_item",
                    {
                        "type": f"{kind}_output",
                        "call_id": call_id,
                        "output": private_values[call_id],
                    },
                ),
            ]
        )
    records.extend(
        [
            _record(
                "2026-07-15T16:00:05.000Z",
                "response_item",
                {
                    "type": "function_call_output",
                    "call_id": "late-input",
                    "output": private_values["late-input"],
                },
            ),
            _record(
                "2026-07-15T16:00:05.100Z",
                "response_item",
                {
                    "type": "function_call",
                    "name": "Read",
                    "call_id": "late-input",
                    "arguments": json.dumps({"path": "/repo/.env.local"}),
                },
            ),
        ]
    )
    _rollout(home, sid, records)

    ingest_codex.ingest(db, home)

    rows = db.execute("SELECT tool_input, tool_result FROM messages").fetchall()
    assert len(rows) == len(private_values)
    assert all("REDACTED:sensitive-file" in row["tool_input"] for row in rows)
    assert all(row["tool_result"] == "[REDACTED:sensitive-file]" for row in rows)
    haystack = "\n".join(
        str(row[key] or "") for row in rows for key in ("tool_input", "tool_result")
    )
    assert not any(value in haystack for value in private_values.values())


def test_codex_end_only_patch_mcp_and_web_tools_are_ingested(db, tmp_path: Path):
    home = tmp_path / "codex"
    sid = "35353535-4646-7777-8888-909090909090"
    private_value = "ordinary-mcp-value-not-matched-by-token-rules"
    records = [
        _meta(sid),
        _record(
            "2026-07-15T16:00:01.000Z",
            "event_msg",
            {
                "type": "patch_apply_end",
                "call_id": "patch-only",
                "changes": {"/repo/main.py": {"type": "update"}},
                "stdout": "",
                "stderr": "patch rejected",
                "status": "failed",
                "success": False,
            },
        ),
        _record(
            "2026-07-15T16:00:02.000Z",
            "event_msg",
            {
                "type": "mcp_tool_call_end",
                "call_id": "mcp-only",
                "invocation": {
                    "server": "files",
                    "tool": "read",
                    "arguments": {"file_path": "/repo/.env"},
                },
                "result": {"Err": private_value},
            },
        ),
        _record(
            "2026-07-15T16:00:03.000Z",
            "event_msg",
            {
                "type": "web_search_end",
                "call_id": "web-only",
                "query": "sqlite jsonl cursor semantics",
                "action": {"type": "search"},
            },
        ),
    ]
    _rollout(home, sid, records)

    ingest_codex.ingest(db, home)

    rows = db.execute(
        "SELECT tool_name, tool_input, tool_result, is_error FROM messages ORDER BY seq"
    ).fetchall()
    assert [row["tool_name"] for row in rows] == ["apply_patch", "files.read", "web_search"]
    assert [row["is_error"] for row in rows] == [1, 1, 0]
    haystack = "\n".join(
        str(row[key] or "") for row in rows for key in ("tool_input", "tool_result")
    )
    assert private_value not in haystack
    assert "REDACTED:sensitive-file" in rows[1]["tool_input"]
    assert rows[1]["tool_result"] == "[REDACTED:sensitive-file]"


def test_codex_archive_move_is_idempotent(db, tmp_path: Path):
    home = tmp_path / "codex"
    sid = "44444444-5555-7666-8777-888888888888"
    records = [
        _meta(sid),
        _record(
            "2026-07-15T16:00:01.000Z",
            "event_msg",
            {"type": "user_message", "message": "move-safe prompt"},
        ),
    ]
    active = _rollout(home, sid, records)
    first = ingest_codex.ingest(db, home)

    archive_dir = home / "archived_sessions"
    archive_dir.mkdir(parents=True)
    active.rename(archive_dir / active.name)
    second = ingest_codex.ingest(db, home)

    assert first.rows_inserted == 1
    assert second.rows_inserted == 0
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_codex_truncated_rewrite_with_shift_is_idempotent(db, tmp_path: Path):
    home = tmp_path / "codex"
    sid = "45454545-5656-7777-8888-999999999990"
    prompt = _record(
        "2026-07-15T16:00:02.000Z",
        "event_msg",
        {"type": "user_message", "message": "rewrite-safe prompt"},
    )
    path = _rollout(
        home,
        sid,
        [
            _meta(sid),
            _record(
                "2026-07-15T16:00:01.000Z",
                "response_item",
                {"type": "reasoning", "summary": "x" * 1000},
            ),
            prompt,
        ],
    )
    first = ingest_codex.ingest(db, home)

    path.write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in [
                _meta(sid),
                _record(
                    "2026-07-15T16:00:01.500Z",
                    "response_item",
                    {"type": "reasoning", "summary": "shifted"},
                ),
                prompt,
            ]
        )
    )
    second = ingest_codex.ingest(db, home)

    assert first.rows_inserted == 1
    assert second.rows_inserted == 0
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_codex_partial_final_line_is_retried(db, tmp_path: Path):
    home = tmp_path / "codex"
    sid = "55555555-6666-7777-8888-999999999999"
    path = _rollout(home, sid, [_meta(sid)])
    prompt = _record(
        "2026-07-15T16:00:01.000Z",
        "event_msg",
        {"type": "user_message", "message": "completed later"},
    )
    with path.open("a") as stream:
        stream.write(json.dumps(prompt))  # no newline: writer may still be active

    first = ingest_codex.ingest(db, home)
    assert first.rows_inserted == 0

    with path.open("a") as stream:
        stream.write("\n")
    second = ingest_codex.ingest(db, home)

    assert second.rows_inserted == 1
    assert db.execute("SELECT text FROM messages").fetchone()[0] == "completed later"


def test_codex_config_defaults_to_codex_home_and_round_trips(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "from-env"))
    default = config.default_config()
    assert default.codex is not None
    assert default.codex.home == (tmp_path / "from-env").resolve()
    assert "[codex]" in config.render_default_config_toml()

    path = tmp_path / "config.toml"
    path.write_text(f'[codex]\nhome = "{tmp_path / "custom"}"\n')
    loaded = config.load(path)
    assert loaded.codex is not None
    assert loaded.codex.home == (tmp_path / "custom").resolve()


def test_codex_provider_is_visible_in_digest(db, tmp_path: Path):
    home = tmp_path / "codex"
    sid = "66666666-7777-7888-8999-000000000000"
    records = [
        _meta(sid),
        _record(
            "2026-07-15T16:00:01.000Z",
            "event_msg",
            {"type": "user_message", "message": "Review pull request 123 for races"},
        ),
        _record(
            "2026-07-15T16:00:02.000Z",
            "event_msg",
            {"type": "user_message", "message": "Review pull request 456 for races"},
        ),
    ]
    _rollout(home, sid, records)
    ingest_codex.ingest(db, home)

    end_ts = db.execute("SELECT MAX(ts) FROM messages").fetchone()[0] + 1000
    md = digest.render_daily(db, end_ts_ms=end_ts, window_hours=24)
    assert "[source=codex]" in md
    assert "×2 [you] via codex" in md
