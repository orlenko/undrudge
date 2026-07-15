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
import os
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import store

# Stable handles for [shell #...] / [msg #...] / [session #...] refs.
# Was 8, but atuin ULIDs only carry ~8 bits of intra-millisecond
# entropy in the first 8 chars — tight activity (npm install loops,
# `gh pr` cycles inside a single session) routinely collided, so 5 of
# every ~11 evidence_refs from the analyzer landed on ambiguous
# prefixes and counted as unresolved. 12 chars adds two full base32
# digits of random entropy past the millisecond timestamp without
# making the handle ugly.
HANDLE_PREFIX = 12
SESSION_ID_PREFIX = HANDLE_PREFIX  # session headings reuse handle width

DEFAULT_WINDOW_HOURS = 24
TOP_TOOLS = 5
PROMPT_HEAD_CHARS = 280
MAX_SESSIONS_LISTED = 25
MIN_REPEAT_COUNT = 2
MAX_REPEATED_PROMPTS = 25
MAX_REPEATED_COMMANDS = 30
MAX_TOOL_NGRAMS = 20
MAX_ERROR_CHAINS = 15
MAX_HANDLES_PER_CLUSTER = 3   # stable refs to surface for repeat clusters
MAX_PRS_LISTED = 5            # PR numbers shown inline before truncating
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
    git_cache: dict[str, dict[str, str]] = {}
    parts: list[str] = []
    parts.append(_render_header(stats, start_ts_ms, end_ts_ms))
    parts.append(_render_sessions(conn, start_ts_ms, end_ts_ms, git_cache))
    parts.append(_render_repeated_prompts(conn, start_ts_ms, end_ts_ms))
    parts.append(_render_repeated_commands(conn, start_ts_ms, end_ts_ms, git_cache))
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


def _render_sessions(
    conn: sqlite3.Connection,
    start: int,
    end: int,
    git_cache: dict[str, dict[str, str]],
) -> str:
    rows = conn.execute(
        """SELECT m.session_id AS id,
                  s.project    AS project,
                  COALESCE(s.source, 'claude') AS source,
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
        first_user = _pick_user_prompt(conn, sid, start, end, order="ASC")
        last_user = _pick_user_prompt(conn, sid, start, end, order="DESC")

        out.append(
            f"\n### session #{short} [source={r['source']}] — {project} — "
            f"{duration} — {r['msg_count']} msgs"
        )
        out.append(f"- top tools: {tools_str}")
        if first_user:
            author, payload = first_user
            tag = "" if author == "you" else f" [{author}]"
            out.append(f"- first user{tag}: {_quote_one_line(payload)}")
        if last_user and (not first_user or last_user[1] != first_user[1]):
            author, payload = last_user
            tag = "" if author == "you" else f" [{author}]"
            out.append(f"- last user{tag}:  {_quote_one_line(payload)}")

        # Shell commands run during this session's time range, attributed
        # by overlap. Best-effort temporal correlation, no fancy joins.
        cmd_rows = conn.execute(
            """SELECT external_id, command, author, cwd FROM commands
                WHERE ts BETWEEN ? AND ?
                ORDER BY ts ASC LIMIT 5""",
            (r["first_ts"], r["last_ts"]),
        ).fetchall()
        if cmd_rows:
            out.append("- shell during session (sample):")
            for c in cmd_rows:
                tag = _author_label(c["author"])
                handle = (
                    f" #{_short_handle(c['external_id'])}"
                    if c["external_id"]
                    else ""
                )
                where = _format_inline_location(c["cwd"], git_cache)
                out.append(
                    f"  - [shell{handle} {tag}]{where} "
                    f"`{_one_line(c['command'])[:140]}`"
                )

    return "\n".join(out) + "\n"


def _pick_user_prompt(
    conn: sqlite3.Connection, sid: str, start: int, end: int, *, order: str
) -> tuple[str, str] | None:
    """First (order='ASC') or last (order='DESC') user message in the
    window that carries semantic content, as (author, payload).

    Skips protocol rows — task notifications and idle pings routinely
    bookend a session and used to shadow the human's actual words.
    """
    assert order in ("ASC", "DESC")
    cur = conn.execute(
        f"""SELECT text, origin FROM messages
             WHERE session_id = ? AND role = 'user' AND ts BETWEEN ? AND ?
               AND text IS NOT NULL AND text != ''
             ORDER BY seq {order}""",
        (sid, start, end),
    )
    for row in cur:
        author, payload = classify_user_text(row["text"], row["origin"])
        if author != "protocol":
            return author, payload
    return None


def _render_repeated_prompts(conn: sqlite3.Connection, start: int, end: int) -> str:
    rows = conn.execute(
        """SELECT m.id AS msg_id, m.session_id AS sid, m.text AS text,
                  m.origin AS origin, s.project AS project,
                  COALESCE(s.source, 'claude') AS source
             FROM messages m
             LEFT JOIN sessions s ON s.id = m.session_id
            WHERE m.role = 'user' AND m.ts BETWEEN ? AND ?
              AND m.text IS NOT NULL AND m.text != ''""",
        (start, end),
    ).fetchall()

    # (msg_id, session_id, payload, project) per skeleton. Clustering
    # runs on the classified payload, not the raw text, so teammate
    # messages group by what was said rather than by their envelope.
    buckets: dict[str, list[tuple[str, str, str, str | None]]] = defaultdict(list)
    authors: dict[str, Counter[str]] = defaultdict(Counter)
    providers: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        author, payload = classify_user_text(r["text"], r["origin"])
        if author == "protocol":
            continue
        skeleton = canonicalize_prompt(payload)
        if not skeleton or len(skeleton) < 12:
            continue
        buckets[skeleton].append(
            (r["msg_id"], r["sid"], payload, r["project"])
        )
        authors[skeleton][author] += 1
        providers[skeleton][r["source"]] += 1

    repeats = sorted(
        ((sk, items) for sk, items in buckets.items() if len(items) >= MIN_REPEAT_COUNT),
        key=lambda kv: -len(kv[1]),
    )[:MAX_REPEATED_PROMPTS]

    if not repeats:
        return ""

    out = ["## Repeated user-prompt skeletons"]
    for skeleton, items in repeats:
        sessions = sorted({sid for _, sid, _, _ in items})
        sample = items[0][2]
        breakdown = _format_author_breakdown(authors[skeleton])
        provider = _format_provider_breakdown(providers[skeleton])
        handles = _format_handles("msg", (mid for mid, _, _, _ in items))
        projects = _format_project_summary(p for _, _, _, p in items)
        prs: set[int] = set()
        for _, _, text, _ in items:
            prs |= _extract_pr_numbers(text)
        pr_summary = _format_pr_summary(prs)
        out.append(
            f"- ×{len(items)}{breakdown}{provider} across {len(sessions)} session(s)"
            f"{projects}{pr_summary}{handles}: `{_one_line(skeleton)[:160]}`"
        )
        out.append(f"  - sample: {_quote_one_line(sample)}")
    return "\n".join(out) + "\n"


def _render_repeated_commands(
    conn: sqlite3.Connection,
    start: int,
    end: int,
    git_cache: dict[str, dict[str, str]],
) -> str:
    rows = conn.execute(
        "SELECT external_id, command, author, cwd FROM commands WHERE ts BETWEEN ? AND ?",
        (start, end),
    ).fetchall()

    counter: Counter[str] = Counter()
    by_author: dict[str, Counter[str]] = defaultdict(Counter)
    sample: dict[str, str] = {}
    handles: dict[str, list[str]] = defaultdict(list)
    cwd_counts: dict[str, Counter[str]] = defaultdict(Counter)
    pr_nums: dict[str, set[int]] = defaultdict(set)
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
        if r["external_id"]:
            handles[skel].append(r["external_id"])
        if r["cwd"]:
            cwd_counts[skel][r["cwd"]] += 1
        pr_nums[skel] |= _extract_pr_numbers(cmd)

    repeats = [
        (skel, count) for skel, count in counter.most_common(MAX_REPEATED_COMMANDS)
        if count >= MIN_REPEAT_COUNT
    ]
    if not repeats:
        return ""

    out = ["## Repeated shell commands"]
    for skel, count in repeats:
        breakdown = _format_author_breakdown(by_author[skel])
        ref = _format_handles("shell", handles[skel])
        location = _format_cwd_summary(cwd_counts[skel], git_cache)
        prs = _format_pr_summary(pr_nums[skel])
        out.append(
            f"- ×{count}{breakdown}{location}{prs}{ref}: "
            f"`{_one_line(skel)[:200]}`"
        )
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


def _format_provider_breakdown(counts: Counter[str]) -> str:
    if not counts:
        return ""
    if len(counts) == 1:
        return f" via {next(iter(counts))}"
    parts = ", ".join(f"{provider}×{n}" for provider, n in counts.most_common())
    return f" via {parts}"


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
        """SELECT m.id          AS msg_id,
                  m.session_id  AS sid,
                  m.seq         AS seq,
                  m.tool_name   AS name,
                  m.tool_input  AS args,
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
            handle = (
                f" [msg #{_short_handle(it['msg_id'])}]"
                if it["msg_id"]
                else ""
            )
            out.append(
                f"- {short}@seq{it['seq']}{handle}: {tool} {args} → {result}"
            )
            shown += 1
            if shown >= MAX_ERROR_CHAINS:
                break
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Prompt provenance
# --------------------------------------------------------------------------

# A growing share of role='user' rows are not typed by the human:
# teammate messages relayed between Claude sessions, background-task
# notifications, idle pings, harness reminders. The envelope is protocol
# noise — clustering on it collapses every teammate exchange into a
# handful of "<teammate-message ...>" buckets that crowd out the human
# patterns. But the *payload* of a teammate message is real work: agents
# re-sending the same briefing is exactly the drudgery this tool exists
# to catch. So: unwrap and attribute, don't discard. Only messages with
# no semantic content at all are classified 'protocol' and kept out of
# prompt clustering.

_RE_TEAMMATE_MSG = re.compile(
    r"<teammate-message\b[^>]*>(.*?)</teammate-message>", re.DOTALL
)
_RE_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_RE_TASK_NOTIFICATION = re.compile(
    r"<task-notification>.*?</task-notification>", re.DOTALL
)
# '[unhandled-content-type: image]', '[Image: source: /tmp/x.png]',
# '[Image #24]', '[image]' — placeholders substituted for binary content.
_RE_MEDIA_MARKER = re.compile(
    r"\[(?:unhandled-content-type:[^\]]*|image\b[^\]]*)\]", re.IGNORECASE
)

# Inter-agent protocol payload types (JSON bodies inside
# <teammate-message>) observed in real transcripts. Extend as new ones
# show up. Unknown JSON payloads are kept — a repeated structured
# hand-off between agents is still a pattern worth seeing.
_PROTOCOL_MSG_TYPES = {
    "idle_notification",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_request",
    "plan_approval_response",
}


def _teammate_payload(body: str) -> str | None:
    """Semantic content of one teammate-message body, or None for a
    protocol ping (idle notification, shutdown handshake, ...)."""
    body = body.strip()
    if body.startswith("{"):
        try:
            d = json.loads(body)
        except json.JSONDecodeError:
            return body
        if isinstance(d, dict) and d.get("type") in _PROTOCOL_MSG_TYPES:
            return None
    return body or None


def classify_user_text(text: str, origin: str | None = None) -> tuple[str, str]:
    """Attribute a user-role message; return (author, payload).

    author ∈ {'you', 'teammate', 'agent', 'protocol'}. payload is the
    text worth clustering on: the unwrapped message body for teammate
    traffic, the text minus harness wrappers otherwise. 'protocol'
    means no semantic content survived — skip the row. 'agent' marks
    subagent transcripts (``origin='subagent'``), where the 'user'
    prompt was authored by the orchestrating session, not the human —
    still real drudgery signal, just framed differently.
    """
    bodies = _RE_TEAMMATE_MSG.findall(text)
    if bodies:
        payloads = [p for b in bodies if (p := _teammate_payload(b)) is not None]
        if not payloads:
            return "protocol", ""
        # Everything outside the tags ("Another Claude session sent a
        # message: ...") is harness framing; the extraction drops it.
        return "teammate", "\n".join(payloads)

    s = _RE_SYSTEM_REMINDER.sub(" ", text)
    s = _RE_TASK_NOTIFICATION.sub(" ", s)
    s = _RE_MEDIA_MARKER.sub(" ", s)
    s = s.strip()
    if not s:
        return "protocol", ""
    if origin == "subagent":
        return "agent", s
    return "you", s


# --------------------------------------------------------------------------
# Canonicalization
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# PR-number extraction
# --------------------------------------------------------------------------

# Heuristic: catches the `gh pr <verb>` family, github URL/API forms
# (`pull/<n>`, `pulls/<n>`), and natural-language references in user
# prompts ("look at PR 350", "pull request #351"). Whitelisted verbs
# only — `gh pr list --limit 100` would otherwise match the limit
# value as a PR number. Heuristic, not authoritative; a wrong
# extraction is acceptable here because the command/prompt text
# itself stays in the digest for the LLM to disambiguate.
_PR_VERBS = [
    "view", "checkout", "merge", "review", "close", "reopen",
    "comment", "edit", "lock", "unlock", "ready", "diff",
]
_PR_PATTERNS = [
    re.compile(
        r"\bgh\s+pr\s+(?:" + "|".join(_PR_VERBS) + r")\b[^\n]{0,120}?\b(\d+)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bpulls?/(\d+)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:PR|pull[\s\-]?request)\s*#?\s*(\d+)\b",
        re.IGNORECASE,
    ),
]


def _extract_pr_numbers(text: str | None) -> set[int]:
    """Return the set of likely-PR numbers cited in ``text``."""
    if not text:
        return set()
    out: set[int] = set()
    for pat in _PR_PATTERNS:
        for m in pat.finditer(text):
            try:
                out.add(int(m.group(1)))
            except (ValueError, TypeError):
                continue
    return out


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


def _short_handle(raw: str | None, *, width: int = HANDLE_PREFIX) -> str:
    """8-char prefix used for [shell #...] / [msg #...] refs in the digest.

    Strips dashes (to keep UUIDs and atuin ids the same shape) and clips
    to ``width``. The same form is what the LLM cites in evidence_refs;
    the resolver does a prefix match against the full id.
    """
    if not raw:
        return ""
    return raw.replace("-", "")[:width]


def _format_handles(kind: str, ids: Iterable[str]) -> str:
    """Render `[<kind> #abc12345 def67890]` from up to N stable ids."""
    seen: list[str] = []
    for raw in ids:
        h = _short_handle(raw)
        if not h or h in seen:
            continue
        seen.append(h)
        if len(seen) >= MAX_HANDLES_PER_CLUSTER:
            break
    return f" [{kind} #{' '.join(seen)}]" if seen else ""


# --------------------------------------------------------------------------
# Location enrichment (cwd → git repo + branch)
# --------------------------------------------------------------------------

# Best-effort means: this is the repo state *now*, not necessarily when
# the command ran. Branches change. Document this in the prompt so the
# LLM uses location as a hint, not as a guarantee. cwd itself, on the
# other hand, is captured at ingest time and is authoritative.
_GIT_LOOKUP_TIMEOUT_S = 2.0


def _git_context(
    cwd: str | None, cache: dict[str, dict[str, str]]
) -> dict[str, str]:
    """Return ``{'repo': <toplevel>, 'branch': <current>}`` for a cwd, or {}.

    Best-effort: returns empty dict if the dir is gone, isn't a git repo,
    git isn't on PATH, or the lookup times out. Cached per-cwd within a
    single render so we don't shell out for every row in a cluster.
    """
    if not cwd:
        return {}
    if cwd in cache:
        return cache[cwd]
    out: dict[str, str] = {}
    try:
        if Path(cwd).is_dir():
            r = subprocess.run(
                ["git", "-C", cwd, "rev-parse",
                 "--show-toplevel", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=_GIT_LOOKUP_TIMEOUT_S,
                check=False,
            )
            if r.returncode == 0:
                lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
                if len(lines) >= 1:
                    out["repo"] = lines[0]
                if len(lines) >= 2 and lines[1] != "HEAD":
                    out["branch"] = lines[1]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    cache[cwd] = out
    return out


def _shorten_path(p: str) -> str:
    """Replace $HOME with ~ for a tighter display path."""
    home = os.path.expanduser("~")
    if home and p.startswith(home):
        return "~" + p[len(home):]
    return p


def _render_location(cwd: str, ctx: dict[str, str]) -> str:
    """Format a location as `repo-name [branch]` or shortened cwd."""
    repo = ctx.get("repo")
    if repo:
        repo_name = Path(repo).name or repo
        branch = ctx.get("branch")
        return f"`{repo_name}`" + (f" [{branch}]" if branch else "")
    return f"`{_shorten_path(cwd)}`"


def _format_inline_location(
    cwd: str | None, cache: dict[str, dict[str, str]]
) -> str:
    """Compact location annotation for per-row digest lines."""
    if not cwd:
        return ""
    ctx = _git_context(cwd, cache)
    return f" in {_render_location(cwd, ctx)}"


def _format_cwd_summary(
    counts: Counter[str], cache: dict[str, dict[str, str]]
) -> str:
    """Cluster-level location summary across the cwds where the pattern hit."""
    if not counts:
        return ""
    if len(counts) == 1:
        cwd = next(iter(counts))
        return f" in {_render_location(cwd, _git_context(cwd, cache))}"
    items = counts.most_common()
    head = items[: MAX_HANDLES_PER_CLUSTER]
    rendered = ", ".join(
        f"{_render_location(cwd, _git_context(cwd, cache))}×{n}"
        for cwd, n in head
    )
    rest = sum(n for _, n in items[MAX_HANDLES_PER_CLUSTER:])
    suffix = f" +{rest} in {len(items) - MAX_HANDLES_PER_CLUSTER} more" if rest else ""
    return f" across {len(items)} dirs ({rendered}{suffix})"


def _format_pr_summary(pr_nums: set[int]) -> str:
    """Cluster-level GitHub PR annotation when ``_extract_pr_numbers``
    found anything in the cluster's commands or prompts."""
    if not pr_nums:
        return ""
    sorted_prs = sorted(pr_nums)
    head = sorted_prs[: MAX_PRS_LISTED]
    rendered = ", ".join(f"#{n}" for n in head)
    rest = len(sorted_prs) - len(head)
    suffix = f" +{rest} more" if rest else ""
    return f" PRs: {rendered}{suffix}"


def _format_project_summary(projects: Iterable[str | None]) -> str:
    """Cluster-level project annotation for repeated-prompt clusters."""
    counts: Counter[str] = Counter()
    for p in projects:
        if p:
            counts[p] += 1
    if not counts:
        return ""
    items = counts.most_common()
    if len(items) == 1:
        proj = items[0][0]
        return f" in `{Path(proj).name or proj}`"
    head = items[: MAX_HANDLES_PER_CLUSTER]
    rendered = ", ".join(f"`{Path(p).name or p}`×{n}" for p, n in head)
    rest = sum(n for _, n in items[MAX_HANDLES_PER_CLUSTER:])
    suffix = f" +{rest} in {len(items) - MAX_HANDLES_PER_CLUSTER} more" if rest else ""
    return f" across {len(items)} projects ({rendered}{suffix})"


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
