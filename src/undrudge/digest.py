"""Daily digest rendering: DB → markdown.

The digest is what the analyzer LLM ingests on its daily run. It's a
sampled, structured view of recent activity — not a transcript. The aim
is "small enough to fit in a single Claude prompt, dense enough that
patterns pop out."

Rough contents:

- Window summary (counts, projects touched).
- One short block per session that had activity in the window.
- Cross-session repeated user-prompt skeletons.
- Cross-session repeated shell commands (canonicalized).
- Repeated tool-call sequences (3-grams).
- Error → retry chains.

All input comes from an already-sanitized DB — there is no path to leak
secrets at this layer.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import store

DEFAULT_WINDOW_HOURS = 24
SESSION_ID_PREFIX = 8         # short id width in the heading
TOP_TOOLS = 5
PROMPT_HEAD_CHARS = 280
MAX_SESSIONS_LISTED = 25
MIN_REPEAT_COUNT = 2
MAX_REPEATED_PROMPTS = 25
MAX_REPEATED_COMMANDS = 30
MAX_TOOL_NGRAMS = 20
MAX_ERROR_CHAINS = 15
NGRAM_WINDOW = 3


@dataclass
class DigestStats:
    total_messages: int
    user_prompts: int
    assistant_turns: int
    tool_calls: int
    shell_commands: int
    sessions: int
    projects: int


def render_daily(
    conn: sqlite3.Connection,
    *,
    end_ts_ms: int | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> str:
    """Render a digest of activity in the trailing window."""
    end_ts_ms = end_ts_ms or store.now_ms()
    start_ts_ms = end_ts_ms - window_hours * 3600 * 1000

    stats = _stats(conn, start_ts_ms, end_ts_ms)
    parts: list[str] = []
    parts.append(_render_header(stats, start_ts_ms, end_ts_ms))
    parts.append(_render_sessions(conn, start_ts_ms, end_ts_ms))
    parts.append(_render_repeated_prompts(conn, start_ts_ms, end_ts_ms))
    parts.append(_render_repeated_commands(conn, start_ts_ms, end_ts_ms))
    parts.append(_render_tool_ngrams(conn, start_ts_ms, end_ts_ms))
    parts.append(_render_error_chains(conn, start_ts_ms, end_ts_ms))
    return "\n".join(p for p in parts if p).rstrip() + "\n"


# --------------------------------------------------------------------------
# Section builders
# --------------------------------------------------------------------------


def _stats(conn: sqlite3.Connection, start: int, end: int) -> DigestStats:
    row = conn.execute(
        """SELECT
             COUNT(*) AS total,
             SUM(role = 'user') AS users,
             SUM(role = 'assistant') AS asst,
             SUM(role = 'tool') AS tools
           FROM messages
           WHERE ts BETWEEN ? AND ?""",
        (start, end),
    ).fetchone()
    sessions = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM messages WHERE ts BETWEEN ? AND ?",
        (start, end),
    ).fetchone()[0]
    projects = conn.execute(
        """SELECT COUNT(DISTINCT s.project)
             FROM sessions s
             JOIN messages m ON m.session_id = s.id
            WHERE m.ts BETWEEN ? AND ?""",
        (start, end),
    ).fetchone()[0]
    cmds = conn.execute(
        "SELECT COUNT(*) FROM commands WHERE ts BETWEEN ? AND ?",
        (start, end),
    ).fetchone()[0]
    return DigestStats(
        total_messages=row["total"] or 0,
        user_prompts=row["users"] or 0,
        assistant_turns=row["asst"] or 0,
        tool_calls=row["tools"] or 0,
        shell_commands=cmds,
        sessions=sessions or 0,
        projects=projects or 0,
    )


def _render_header(stats: DigestStats, start: int, end: int) -> str:
    return (
        f"# Activity digest — {_fmt_ts(start)} → {_fmt_ts(end)}\n\n"
        f"- sessions: {stats.sessions} across {stats.projects} project(s)\n"
        f"- messages: {stats.total_messages} "
        f"(user: {stats.user_prompts}, assistant: {stats.assistant_turns}, tool: {stats.tool_calls})\n"
        f"- shell commands: {stats.shell_commands}\n"
    )


def _render_sessions(conn: sqlite3.Connection, start: int, end: int) -> str:
    rows = conn.execute(
        """SELECT m.session_id AS id,
                  s.project    AS project,
                  MIN(m.ts)    AS first_ts,
                  MAX(m.ts)    AS last_ts,
                  COUNT(*)     AS msg_count,
                  SUM(m.role = 'user') AS user_count
             FROM messages m
             LEFT JOIN sessions s ON s.id = m.session_id
            WHERE m.ts BETWEEN ? AND ?
            GROUP BY m.session_id
            ORDER BY msg_count DESC
            LIMIT ?""",
        (start, end, MAX_SESSIONS_LISTED),
    ).fetchall()
    if not rows:
        return ""

    out = ["## Sessions"]
    for r in rows:
        sid = r["id"]
        short = _short_session_id(sid)
        duration = _fmt_duration(r["last_ts"] - r["first_ts"])
        project = r["project"] or "(unknown)"

        # Top tools by call count.
        tools = conn.execute(
            """SELECT tool_name, COUNT(*) AS c
                 FROM messages
                WHERE session_id = ? AND tool_name IS NOT NULL
                  AND ts BETWEEN ? AND ?
                GROUP BY tool_name
                ORDER BY c DESC
                LIMIT ?""",
            (sid, start, end, TOP_TOOLS),
        ).fetchall()
        tools_str = (
            ", ".join(f"{t['tool_name']}×{t['c']}" for t in tools) if tools else "—"
        )

        # First and last user prompts in this session within the window.
        first_user = conn.execute(
            """SELECT text FROM messages
                WHERE session_id = ? AND role = 'user' AND ts BETWEEN ? AND ?
                  AND text IS NOT NULL AND text != ''
                ORDER BY seq ASC LIMIT 1""",
            (sid, start, end),
        ).fetchone()
        last_user = conn.execute(
            """SELECT text FROM messages
                WHERE session_id = ? AND role = 'user' AND ts BETWEEN ? AND ?
                  AND text IS NOT NULL AND text != ''
                ORDER BY seq DESC LIMIT 1""",
            (sid, start, end),
        ).fetchone()

        out.append(f"\n### {short} — {project} — {duration} — {r['msg_count']} msgs")
        out.append(f"- top tools: {tools_str}")
        if first_user and first_user["text"]:
            out.append(f"- first user: {_quote_one_line(first_user['text'])}")
        if last_user and last_user["text"] and (not first_user or last_user["text"] != first_user["text"]):
            out.append(f"- last user:  {_quote_one_line(last_user['text'])}")

        # Shell commands run during this session's time range, attributed
        # by overlap. Best-effort temporal correlation, no fancy joins.
        cmd_rows = conn.execute(
            """SELECT command, author FROM commands
                WHERE ts BETWEEN ? AND ?
                ORDER BY ts ASC LIMIT 5""",
            (r["first_ts"], r["last_ts"]),
        ).fetchall()
        if cmd_rows:
            out.append("- shell during session (sample):")
            for c in cmd_rows:
                tag = _author_label(c["author"])
                out.append(f"  - [{tag}] `{_one_line(c['command'])[:140]}`")

    return "\n".join(out) + "\n"


def _render_repeated_prompts(conn: sqlite3.Connection, start: int, end: int) -> str:
    rows = conn.execute(
        """SELECT session_id, text FROM messages
             WHERE role = 'user' AND ts BETWEEN ? AND ?
               AND text IS NOT NULL AND text != ''""",
        (start, end),
    ).fetchall()

    buckets: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in rows:
        skeleton = canonicalize_prompt(r["text"])
        if not skeleton or len(skeleton) < 12:
            continue
        buckets[skeleton].append((r["session_id"], r["text"]))

    repeats = sorted(
        ((sk, items) for sk, items in buckets.items() if len(items) >= MIN_REPEAT_COUNT),
        key=lambda kv: -len(kv[1]),
    )[:MAX_REPEATED_PROMPTS]

    if not repeats:
        return ""

    out = ["## Repeated user-prompt skeletons"]
    for skeleton, items in repeats:
        sessions = sorted({sid for sid, _ in items})
        sample = items[0][1]
        out.append(
            f"- ×{len(items)} across {len(sessions)} session(s): "
            f"`{_one_line(skeleton)[:160]}`"
        )
        out.append(f"  - sample: {_quote_one_line(sample)}")
    return "\n".join(out) + "\n"


def _render_repeated_commands(conn: sqlite3.Connection, start: int, end: int) -> str:
    rows = conn.execute(
        "SELECT command, author FROM commands WHERE ts BETWEEN ? AND ?",
        (start, end),
    ).fetchall()

    counter: Counter[str] = Counter()
    by_author: dict[str, Counter[str]] = defaultdict(Counter)
    sample: dict[str, str] = {}
    for r in rows:
        cmd = r["command"]
        if not cmd:
            continue
        skel = canonicalize_command(cmd)
        if not skel or len(skel) < 4:
            continue
        counter[skel] += 1
        by_author[skel][_author_label(r["author"])] += 1
        sample.setdefault(skel, cmd)

    repeats = [
        (skel, count) for skel, count in counter.most_common(MAX_REPEATED_COMMANDS)
        if count >= MIN_REPEAT_COUNT
    ]
    if not repeats:
        return ""

    out = ["## Repeated shell commands"]
    for skel, count in repeats:
        breakdown = _format_author_breakdown(by_author[skel])
        out.append(f"- ×{count}{breakdown}: `{_one_line(skel)[:200]}`")
        if sample[skel] != skel:
            out.append(f"  - sample: `{_one_line(sample[skel])[:200]}`")
    return "\n".join(out) + "\n"


def _author_label(raw: str | None) -> str:
    """Map an atuin author tag to a stable display label.

    'claude-code' is the tag atuin's AI-agent shell hooks set on commands
    invoked through the Bash tool. Empty/None means a human at the
    keyboard (or any shell session without the hook installed).
    """
    if not raw:
        return "you"
    if raw == "claude-code":
        return "agent"
    return raw


def _format_author_breakdown(counts: Counter[str]) -> str:
    if not counts:
        return ""
    if len(counts) == 1:
        only = next(iter(counts))
        return f" [{only}]"
    parts = ", ".join(f"{label}×{n}" for label, n in counts.most_common())
    return f" ({parts})"


def _render_tool_ngrams(conn: sqlite3.Connection, start: int, end: int) -> str:
    rows = conn.execute(
        """SELECT session_id, tool_name FROM messages
             WHERE tool_name IS NOT NULL AND ts BETWEEN ? AND ?
             ORDER BY session_id, seq""",
        (start, end),
    ).fetchall()

    sequences: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        sequences[r["session_id"]].append(r["tool_name"])

    counter: Counter[tuple[str, ...]] = Counter()
    for seq in sequences.values():
        if len(seq) < NGRAM_WINDOW:
            continue
        for i in range(len(seq) - NGRAM_WINDOW + 1):
            counter[tuple(seq[i : i + NGRAM_WINDOW])] += 1

    repeats = [
        (gram, c) for gram, c in counter.most_common(MAX_TOOL_NGRAMS)
        if c >= MIN_REPEAT_COUNT
    ]
    if not repeats:
        return ""

    out = [f"## Repeated tool sequences ({NGRAM_WINDOW}-grams)"]
    for gram, c in repeats:
        out.append(f"- ×{c}: ({' → '.join(gram)})")
    return "\n".join(out) + "\n"


def _render_error_chains(conn: sqlite3.Connection, start: int, end: int) -> str:
    rows = conn.execute(
        """SELECT m.session_id AS sid,
                  m.seq        AS seq,
                  m.tool_name  AS name,
                  m.tool_input AS args,
                  m.tool_result AS result
             FROM messages m
            WHERE m.is_error = 1 AND m.ts BETWEEN ? AND ?
            ORDER BY m.session_id, m.seq""",
        (start, end),
    ).fetchall()
    if not rows:
        return ""

    grouped: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        grouped[r["sid"]].append(r)

    out = ["## Tool errors"]
    shown = 0
    for sid, items in grouped.items():
        if shown >= MAX_ERROR_CHAINS:
            break
        for it in items:
            short = _short_session_id(sid)
            args = _short_json(it["args"])
            result = _one_line(it["result"] or "")[:140]
            tool = it["name"] or "(tool_result)"
            out.append(f"- {short}@seq{it['seq']}: {tool} {args} → {result}")
            shown += 1
            if shown >= MAX_ERROR_CHAINS:
                break
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Canonicalization
# --------------------------------------------------------------------------


_RE_PATH = re.compile(
    r"""(?xi)
        (?<![\w/<>])
        (?:
            /[\w.\-]+(?:/[\w.\-]+)+       # absolute with ≥2 segments (skips slash commands like /loop)
            |
            (?:[\w.\-]+/)+[\w.\-]+        # relative with ≥1 slash, e.g. src/main.py
        )
        (?![\w<>])
    """
)
_RE_URL = re.compile(r"https?://\S+")
_RE_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_RE_NUMBER = re.compile(r"\b\d{2,}\b")
_RE_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")
_RE_HEX = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_RE_WS = re.compile(r"\s+")


def canonicalize_prompt(text: str) -> str:
    """Normalize a user prompt to a comparable skeleton.

    The point: two prompts that differ only in file path / quoted string /
    big number / hex hash / URL should hash to the same skeleton, so we
    can spot "I've asked this same kind of thing 4 times this week."
    """
    if not text:
        return ""
    s = text.strip()
    s = _RE_URL.sub("<url>", s)
    s = _RE_UUID.sub("<uuid>", s)
    s = _RE_PATH.sub("<path>", s)
    s = _RE_QUOTED.sub("<str>", s)
    s = _RE_HEX.sub("<hex>", s)
    s = _RE_NUMBER.sub("<n>", s)
    s = _RE_WS.sub(" ", s).strip().lower()
    return s


def canonicalize_command(cmd: str) -> str:
    """Normalize a shell command to a skeleton.

    Preserves the executable + flags; replaces path/quoted/numeric
    arguments with placeholders.
    """
    if not cmd:
        return ""
    s = cmd.strip()
    s = _RE_URL.sub("<url>", s)
    s = _RE_UUID.sub("<uuid>", s)
    s = _RE_QUOTED.sub("<str>", s)
    s = _RE_PATH.sub("<path>", s)
    s = _RE_HEX.sub("<hex>", s)
    s = _RE_NUMBER.sub("<n>", s)
    s = _RE_WS.sub(" ", s).strip()
    return s


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def _short_session_id(sid: str, *, width: int = SESSION_ID_PREFIX) -> str:
    """Stable short id that works for UUIDs and arbitrary string ids alike."""
    return sid.replace("-", "")[:width]


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="minutes")


def _fmt_duration(ms: int) -> str:
    if ms <= 0:
        return "<1m"
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    rem = m % 60
    return f"{h}h{rem}m" if rem else f"{h}h"


def _one_line(text: str) -> str:
    return text.replace("\n", " ").replace("\r", " ").replace("`", "ˋ")


def _quote_one_line(text: str) -> str:
    head = _one_line(text)[:PROMPT_HEAD_CHARS]
    if len(text) > PROMPT_HEAD_CHARS:
        head += "…"
    return f"“{head}”"


def _short_json(s: str | None, *, limit: int = 120) -> str:
    if not s:
        return "{}"
    try:
        d = json.loads(s)
        out = json.dumps(d, separators=(",", ":"), default=str)
    except json.JSONDecodeError:
        out = s
    out = out.replace("\n", " ")
    return (out[:limit] + "…") if len(out) > limit else out
