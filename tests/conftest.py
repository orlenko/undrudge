"""Shared fixtures."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fixtures import ANTHROPIC_KEY, GITHUB_TOKEN, GITHUB_TOKEN_FAKE_REPEATED

from undrudge import store


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = store.init(tmp_path / "undrudge.sqlite")
    yield conn
    conn.close()


@pytest.fixture
def claude_projects(tmp_path: Path) -> Path:
    """A fake ~/.claude/projects with one session containing planted secrets."""
    root = tmp_path / "claude_projects"
    proj = root / "-Users-fake-repo"
    proj.mkdir(parents=True)
    session_id = "00000000-1111-2222-3333-444444444444"
    file = proj / f"{session_id}.jsonl"

    lines = [
        # operational noise — should be skipped
        {"type": "permission-mode", "permissionMode": "bypassPermissions",
         "sessionId": session_id},
        # user prompt with a planted GitHub token
        {
            "type": "user",
            "uuid": "u-1",
            "sessionId": session_id,
            "timestamp": "2026-05-04T10:00:00.000Z",
            "cwd": "/Users/fake/repo",
            "message": {
                "role": "user",
                "content": (
                    f"please use my token {GITHUB_TOKEN} "
                    f"to look at the repo"
                ),
            },
        },
        # assistant: thinking + text + tool_use to Read on a sensitive file
        {
            "type": "assistant",
            "uuid": "a-1",
            "sessionId": session_id,
            "timestamp": "2026-05-04T10:00:30.000Z",
            "cwd": "/Users/fake/repo",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "ok let's check"},
                    {"type": "text", "text": "Reading the env file."},
                    {"type": "tool_use", "id": "tu-1", "name": "Read",
                     "input": {"file_path": "/Users/fake/.env"}},
                ],
            },
        },
        # tool_result inside a user message; result text contains a secret
        {
            "type": "user",
            "uuid": "u-2",
            "sessionId": session_id,
            "timestamp": "2026-05-04T10:00:32.000Z",
            "cwd": "/Users/fake/repo",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu-1",
                     "content": [
                         {"type": "text",
                          "text": f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}"},
                     ]},
                ],
            },
        },
    ]
    file.write_text("\n".join(json.dumps(d) for d in lines) + "\n")
    return root


@pytest.fixture
def atuin_db(tmp_path: Path) -> Path:
    """A fake atuin history.db with a few commands, including one secret."""
    path = tmp_path / "atuin.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE history (
          id TEXT, timestamp INTEGER, duration INTEGER, exit INTEGER,
          command TEXT, cwd TEXT, session TEXT, hostname TEXT,
          deleted_at INTEGER, author TEXT, intent TEXT
        );
        """
    )
    rows = [
        ("a1", 1714000000_000_000_000, 100_000_000, 0,
         "ls -la", "/Users/fake/repo", "s1", "host:user", None, "", None),
        ("a2", 1714000010_000_000_000, 200_000_000, 0,
         f"export GITHUB_TOKEN={GITHUB_TOKEN_FAKE_REPEATED}",
         "/Users/fake/repo", "s1", "host:user", None, "claude-code", "set token"),
        ("a3", 1714000020_000_000_000, 50_000_000, 0,
         "git status", "/Users/fake/repo", "s1", "host:user", None, "", None),
        # deleted row should be skipped entirely
        ("a4", 1714000030_000_000_000, 50_000_000, 0,
         "rm -rf /", "/Users/fake/repo", "s1", "host:user", 1714000031_000_000_000, "", None),
    ]
    conn.executemany("INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path
