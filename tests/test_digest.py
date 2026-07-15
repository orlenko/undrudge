"""Digest renderer tests.

Asserts shape, not exact wording — the markdown is for human + LLM eyes,
and small phrasing tweaks shouldn't break the suite. We do verify
canonicalization correctness directly (it's the load-bearing piece for
pattern detection).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from undrudge import digest, store

# --------------------------------------------------------------------------
# Canonicalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Look at PR 1234", "look at pr <n>"),
        ("Look at PR 5678", "look at pr <n>"),
        ("Look at the file /Users/foo/bar/baz.py please",
         "look at the file <path> please"),
        ("Look at https://example.com/x and tell me",
         "look at <url> and tell me"),
        ("Use uuid 7f3a91b8-c4d2-4e5f-9a01-1234567890ab",
         "use uuid <uuid>"),
        ("Search for 'foo bar' in the repo", "search for <str> in the repo"),
        ("Hash is deadbeef0123abcd1234", "hash is <hex>"),
    ],
)
def test_canonicalize_prompt(raw: str, expected: str):
    assert digest.canonicalize_prompt(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ls -la /Users/foo", "ls -la <path>"),
        ("ls -la /Users/bar", "ls -la <path>"),
        ("git rebase -i HEAD~10", "git rebase -i HEAD~<n>"),
        ("grep 'pattern' src/main.py", "grep <str> <path>"),
        ("curl https://api.example.com/v1/x | jq .y",
         "curl <url> | jq .y"),
    ],
)
def test_canonicalize_command(raw: str, expected: str):
    assert digest.canonicalize_command(raw) == expected


def test_two_paths_collapse_to_same_skeleton():
    a = digest.canonicalize_prompt("Run pytest on src/main.py")
    b = digest.canonicalize_prompt("Run pytest on src/other/file.py")
    assert a == b


# --------------------------------------------------------------------------
# Render against a synthetic DB
# --------------------------------------------------------------------------


def _seed(conn: sqlite3.Connection) -> None:
    """Build a small but realistic activity window."""
    sessions = [
        ("aaaaaaaa-1111-2222-3333-444444444444", "/repo/foo", 1_000_000_000_000, 1_000_000_300_000),
        ("bbbbbbbb-2222-3333-4444-555555555555", "/repo/bar", 1_000_000_400_000, 1_000_000_700_000),
    ]
    for sid, project, start, last in sessions:
        conn.execute(
            "INSERT INTO sessions(id, project, started_at, last_seen) VALUES (?,?,?,?)",
            (sid, project, start, last),
        )

    rows = [
        # session 1: user asks twice "look at PR <n>", with Read-Edit-Bash chain.
        ("m1#0", sessions[0][0], 0, 1_000_000_000_000, "user", "Look at PR 100", None, None, None, 0),
        ("m1#1", sessions[0][0], 1, 1_000_000_010_000, "assistant", "Sure", None, None, None, 0),
        ("m1#2", sessions[0][0], 2, 1_000_000_020_000, "assistant", None, "Read", '{"file_path":"/repo/foo/x.py"}', None, 0),
        ("m1#3", sessions[0][0], 3, 1_000_000_030_000, "tool", None, None, None, "ok", 0),
        ("m1#4", sessions[0][0], 4, 1_000_000_040_000, "assistant", None, "Edit", '{"file_path":"/repo/foo/x.py"}', None, 0),
        ("m1#5", sessions[0][0], 5, 1_000_000_050_000, "tool", None, None, None, "ok", 0),
        ("m1#6", sessions[0][0], 6, 1_000_000_060_000, "assistant", None, "Bash", '{"command":"pytest x.py"}', None, 0),
        ("m1#7", sessions[0][0], 7, 1_000_000_070_000, "tool", None, None, None, "FAIL", 1),
        ("m2#0", sessions[0][0], 8, 1_000_000_100_000, "user", "Look at PR 200", None, None, None, 0),
        ("m2#1", sessions[0][0], 9, 1_000_000_110_000, "assistant", None, "Read", '{"file_path":"/repo/foo/y.py"}', None, 0),
        ("m2#2", sessions[0][0], 10, 1_000_000_120_000, "tool", None, None, None, "ok", 0),
        ("m2#3", sessions[0][0], 11, 1_000_000_130_000, "assistant", None, "Edit", '{"file_path":"/repo/foo/y.py"}', None, 0),
        ("m2#4", sessions[0][0], 12, 1_000_000_140_000, "tool", None, None, None, "ok", 0),
        ("m2#5", sessions[0][0], 13, 1_000_000_150_000, "assistant", None, "Bash", '{"command":"pytest y.py"}', None, 0),
        ("m2#6", sessions[0][0], 14, 1_000_000_160_000, "tool", None, None, None, "ok", 0),
        # session 2: also "Look at PR <n>" — cross-session repeat.
        ("m3#0", sessions[1][0], 0, 1_000_000_400_000, "user", "Look at PR 300", None, None, None, 0),
        ("m3#1", sessions[1][0], 1, 1_000_000_410_000, "assistant", "Reading", None, None, None, 0),
    ]
    conn.executemany(
        "INSERT INTO messages(id, session_id, seq, ts, role, text, tool_name, tool_input, tool_result, is_error) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )

    cmds = [
        # repeated find/grep skeleton — automation candidate; mixed authors
        ("atuin", "c1", 1_000_000_000_500, "find . -name '*.py' | xargs grep 'foo'", 0, None),
        ("atuin", "c2", 1_000_000_010_500, "find . -name '*.py' | xargs grep 'bar'", 0, "claude-code"),
        ("atuin", "c3", 1_000_000_020_500, "find . -name '*.py' | xargs grep 'baz'", 0, "claude-code"),
        ("atuin", "c4", 1_000_000_030_500, "git status", 0, None),
        ("atuin", "c5", 1_000_000_040_500, "ls -la", 0, None),
    ]
    for source, ext_id, ts, command, exit_status, author in cmds:
        conn.execute(
            "INSERT INTO commands(source, external_id, ts, command, exit_status, author) "
            "VALUES (?,?,?,?,?,?)",
            (source, ext_id, ts, command, exit_status, author),
        )


@pytest.fixture
def seeded_db(tmp_path: Path) -> sqlite3.Connection:
    conn = store.init(tmp_path / "d.sqlite")
    _seed(conn)
    yield conn
    conn.close()


def test_digest_header_and_counts(seeded_db: sqlite3.Connection):
    md = digest.render_daily(
        seeded_db,
        end_ts_ms=1_000_001_000_000,
        window_hours=24 * 365,  # wide enough to include the synthetic ts range
    )
    assert "Activity digest" in md
    assert "sessions: 2 across 2 project(s)" in md
    assert "messages: 17" in md
    assert "shell commands: 5" in md


def test_digest_lists_each_session(seeded_db: sqlite3.Connection):
    md = digest.render_daily(seeded_db, end_ts_ms=1_000_001_000_000, window_hours=24 * 365)
    # Session id prefix should appear under "## Sessions".
    assert "## Sessions" in md
    assert "aaaaaaaa" in md
    assert "bbbbbbbb" in md
    assert "/repo/foo" in md
    assert "/repo/bar" in md


def test_digest_session_heading_carries_citable_handle(seeded_db: sqlite3.Connection):
    """Each session block heading should expose `session #<handle>` so
    the analyzer can cite the session as evidence_refs source=session."""
    md = digest.render_daily(seeded_db, end_ts_ms=1_000_001_000_000, window_hours=24 * 365)
    # Seeded session UUIDs start with 'aaaaaaaa' / 'bbbbbbbb'. After
    # widening to a 12-char handle, the headings carry the explicit
    # `session #...` tag so the LLM doesn't have to guess.
    assert "session #aaaaaaaa" in md
    assert "session #bbbbbbbb" in md


def test_digest_finds_repeated_user_prompts(seeded_db: sqlite3.Connection):
    md = digest.render_daily(seeded_db, end_ts_ms=1_000_001_000_000, window_hours=24 * 365)
    assert "## Repeated user-prompt skeletons" in md
    assert "look at pr <n>" in md.lower()
    # Project annotation: "Look at PR <n>" hits in /repo/foo (2x) and
    # /repo/bar (1x), so the cluster line should call out both projects.
    section = md.split("## Repeated user-prompt skeletons", 1)[1].split("##", 1)[0]
    assert "across 2 projects" in section
    assert "foo" in section and "bar" in section
    # PRs cited in the prompts (100, 200, 300) should be aggregated.
    assert "PRs:" in section
    for pr in ("#100", "#200", "#300"):
        assert pr in section


@pytest.mark.parametrize(
    "text,expected",
    [
        ("gh pr view 350", {350}),
        ("gh pr checkout 351 -R foo/bar", {351}),
        ("gh pr merge --squash 352", {352}),
        ("gh api repos/foo/bar/pulls/353/comments", {353}),
        ("git fetch origin pull/354/head:pr-354", {354}),
        ("https://github.com/foo/bar/pull/355", {355}),
        ("look at PR 100", {100}),
        ("analyze pull request #200", {200}),
        ("we need pull-request 201 reviewed", {201}),
        # Negative: list/status verbs and bare numbers should not match.
        ("gh pr list --limit 100", set()),
        ("gh pr status", set()),
        ("ls -la /repo/100", set()),
        ("", set()),
    ],
)
def test_extract_pr_numbers(text: str, expected: set[int]):
    assert digest._extract_pr_numbers(text) == expected


def test_digest_finds_repeated_shell_skeleton(seeded_db: sqlite3.Connection):
    md = digest.render_daily(seeded_db, end_ts_ms=1_000_001_000_000, window_hours=24 * 365)
    assert "## Repeated shell commands" in md
    # The 3 find/grep commands should collapse to a single skeleton with ×3.
    assert "find . -name <str>" in md
    assert "×3" in md


def test_digest_shows_author_breakdown_in_repeated_commands(seeded_db: sqlite3.Connection):
    md = digest.render_daily(seeded_db, end_ts_ms=1_000_001_000_000, window_hours=24 * 365)
    # Scope to the "Repeated shell commands" section only.
    section = md.split("## Repeated shell commands", 1)[1].split("##", 1)[0]
    line = next(ln for ln in section.splitlines() if "find . -name" in ln)
    # mixed authors: 1×you + 2×agent
    assert "you" in line and "agent" in line


def test_digest_uses_author_label_in_session_shell_sample(seeded_db: sqlite3.Connection):
    md = digest.render_daily(seeded_db, end_ts_ms=1_000_001_000_000, window_hours=24 * 365)
    # Per-session shell samples render as `[shell #<handle> <author>]`.
    assert "[shell" in md and " you]" in md
    assert " agent]" in md


def test_digest_finds_tool_ngrams(seeded_db: sqlite3.Connection):
    md = digest.render_daily(seeded_db, end_ts_ms=1_000_001_000_000, window_hours=24 * 365)
    assert "Repeated tool sequences" in md
    # Read → Edit → Bash sequence repeats twice across the synthetic data.
    assert "Read → Edit → Bash" in md or "Read → Bash" in md


def test_digest_lists_error_chains(seeded_db: sqlite3.Connection):
    md = digest.render_daily(seeded_db, end_ts_ms=1_000_001_000_000, window_hours=24 * 365)
    assert "## Tool errors" in md
    assert "FAIL" in md


def test_digest_empty_window_renders_header_only(tmp_path: Path):
    conn = store.init(tmp_path / "empty.sqlite")
    md = digest.render_daily(conn, end_ts_ms=1_000_000_000_000, window_hours=24)
    assert "Activity digest" in md
    assert "sessions: 0" in md
    # No noisy empty section headings:
    assert "## Sessions" not in md
    assert "## Repeated" not in md
    conn.close()


# --------------------------------------------------------------------------
# Location enrichment
# --------------------------------------------------------------------------


def _init_repo(tmp_path: Path, branch: str = "main") -> Path:
    """Create a minimal git repo with one commit on the given branch."""
    import subprocess
    repo = tmp_path / "demo-repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)],
                   check=True, env=env)
    (repo / "README").write_text("hi\n")
    subprocess.run(["git", "-C", str(repo), "add", "README"],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"],
                   check=True, env=env)
    return repo


def test_git_context_returns_repo_and_branch_for_real_repo(tmp_path: Path):
    repo = _init_repo(tmp_path, branch="trunk")
    cache: dict[str, dict[str, str]] = {}
    ctx = digest._git_context(str(repo), cache)
    assert Path(ctx["repo"]).resolve() == repo.resolve()
    assert ctx["branch"] == "trunk"
    # Cached: second call should not re-shell-out (we just check it
    # returns the same object reference).
    assert digest._git_context(str(repo), cache) is ctx


def test_git_context_returns_empty_for_nonexistent_cwd(tmp_path: Path):
    cache: dict[str, dict[str, str]] = {}
    assert digest._git_context(str(tmp_path / "nope"), cache) == {}


def test_git_context_returns_empty_for_non_repo_dir(tmp_path: Path):
    cache: dict[str, dict[str, str]] = {}
    assert digest._git_context(str(tmp_path), cache) == {}


def test_render_repeated_commands_surfaces_cwd_summary(tmp_path: Path):
    """Cluster line for a repeated shell skeleton should call out cwd(s)."""
    conn = store.init(tmp_path / "u.sqlite")
    cmds = [
        ("atuin", "h1", 1_000_000_000_500, "find . -name 'a' | xargs grep 'x'", "/foo/repo-a", None),
        ("atuin", "h2", 1_000_000_010_500, "find . -name 'b' | xargs grep 'y'", "/foo/repo-a", None),
        ("atuin", "h3", 1_000_000_020_500, "find . -name 'c' | xargs grep 'z'", "/foo/repo-b", None),
    ]
    for source, ext_id, ts, command, cwd, author in cmds:
        conn.execute(
            "INSERT INTO commands(source, external_id, ts, command, cwd, author) "
            "VALUES (?,?,?,?,?,?)",
            (source, ext_id, ts, command, cwd, author),
        )
    md = digest.render_daily(conn, end_ts_ms=1_000_001_000_000, window_hours=24)
    section = md.split("## Repeated shell commands", 1)[1]
    assert "across 2 dirs" in section
    # Both cwds (or their basenames) should be present in the rendering;
    # neither dir exists on disk so we fall back to the shortened cwd.
    assert "repo-a" in section
    assert "repo-b" in section
    conn.close()


# --------------------------------------------------------------------------
# Prompt provenance
# --------------------------------------------------------------------------


IDLE_PING = (
    'Another Claude session sent a message: '
    '<teammate-message teammate_id="aprs-sec" color="yellow"> '
    '{"type":"idle_notification","from":"aprs-sec",'
    '"timestamp":"2026-07-06T19:43:32.333Z","idleReason":"available"} '
    '</teammate-message>  This came from another Claude session — '
    'not typed by the user.'
)
TEAMMATE_BRIEF = (
    '<teammate-message teammate_id="team-lead" summary="review briefing"> '
    'Read the file /tmp/aprs-2303/p-ts.txt and follow the instructions '
    'in it exactly. </teammate-message>'
)


@pytest.mark.parametrize(
    "text",
    [
        IDLE_PING,
        "<task-notification> <task-id>a1</task-id> <status>completed</status> </task-notification>",
        "<system-reminder>The user named this session x.</system-reminder>",
        "[unhandled-content-type: image]",
        "[Image: source: /tmp/clipboard-2026.png]",
    ],
)
def test_classify_protocol_messages_are_dropped(text: str):
    author, payload = digest.classify_user_text(text)
    assert author == "protocol"
    assert payload == ""


def test_classify_teammate_message_unwraps_payload():
    author, payload = digest.classify_user_text(TEAMMATE_BRIEF)
    assert author == "teammate"
    assert payload.startswith("Read the file /tmp/aprs-2303/p-ts.txt")
    assert "<teammate-message" not in payload
    assert "team-lead" not in payload  # envelope attrs are gone


def test_classify_mixed_ping_and_real_teammate_message():
    """One user row can relay several teammate messages; pings among
    them must not mask the real one."""
    author, payload = digest.classify_user_text(IDLE_PING + "\n" + TEAMMATE_BRIEF)
    assert author == "teammate"
    assert "idle_notification" not in payload
    assert "follow the instructions" in payload


def test_classify_human_prompt_keeps_text_and_strips_wrappers():
    author, payload = digest.classify_user_text(
        "fix the flaky auth test <system-reminder>noise</system-reminder>"
    )
    assert author == "you"
    assert payload == "fix the flaky auth test"


def test_classify_subagent_origin_is_agent():
    author, payload = digest.classify_user_text(
        "You are a security reviewer. Audit the diff.", origin="subagent"
    )
    assert author == "agent"
    assert payload.startswith("You are a security reviewer")


def test_repeated_prompts_cluster_on_teammate_payload(tmp_path: Path):
    """Teammate briefings cluster by what was said (labelled), idle
    pings vanish, and first/last user lines skip protocol rows."""
    conn = store.init(tmp_path / "p.sqlite")
    sid = "cccccccc-1111-2222-3333-444444444444"
    conn.execute(
        "INSERT INTO sessions(id, project, started_at, last_seen) VALUES (?,?,?,?)",
        (sid, "/repo/ops", 1_000_000_000_000, 1_000_000_300_000),
    )
    brief = (
        '<teammate-message teammate_id="team-lead" summary="brief %d"> '
        "Read the file /tmp/aprs-%d/p.txt and follow the instructions "
        "in it exactly. </teammate-message>"
    )
    rows = [
        # protocol bookends that used to shadow the human's words
        ("n1#0", 0, "<task-notification> <task-id>t0</task-id> <status>started</status> </task-notification>", None),
        ("h1#0", 1, "Fix the flaky auth test please", None),
        ("b1#0", 2, brief % (1, 1), None),
        ("b2#0", 3, brief % (2, 2), None),
        ("b3#0", 4, brief % (3, 3), None),
        ("i1#0", 5, IDLE_PING, None),
        ("i2#0", 6, IDLE_PING, None),
        # subagent briefing repeated by the orchestrator
        ("s1#0", 7, "Review chunk 17 of the diff for bugs", "subagent"),
        ("s2#0", 8, "Review chunk 28 of the diff for bugs", "subagent"),
        ("n2#0", 9, "<task-notification> <task-id>t9</task-id> <status>completed</status> </task-notification>", None),
    ]
    for mid, seq, text, origin in rows:
        conn.execute(
            "INSERT INTO messages(id, session_id, seq, ts, role, text, origin) "
            "VALUES (?,?,?,?,?,?,?)",
            (mid, sid, seq, 1_000_000_000_000 + seq * 1000, "user", text, origin),
        )
    md = digest.render_daily(conn, end_ts_ms=1_000_001_000_000, window_hours=24)

    # The three briefings collapse to one labelled cluster on the payload.
    assert "×3 [teammate]" in md
    assert "read the file <path> and follow the instructions" in md
    # The subagent fan-out clusters too, labelled as agent-authored.
    assert "×2 [agent]" in md
    # Envelope and protocol traffic never surface.
    assert "<teammate-message" not in md
    assert "idle_notification" not in md
    assert "task-notification" not in md
    # first/last user skip the protocol bookends.
    assert "- first user: “Fix the flaky auth test please”" in md
    assert "t9" not in md
    conn.close()
