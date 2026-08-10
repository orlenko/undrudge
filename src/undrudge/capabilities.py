"""Installed-capability inventory: what your agents can do vs what you use.

Implements docs/capability-gap.md. Three local sources plus one network
source, each failure-isolated:

- ``help``:  ``claude --help`` / ``codex --help`` (plus one level of
  subcommand help), parsed into flag/subcommand rows. Version-gated;
  cannot hallucinate. Only this source may retire its own rows outright.
- ``probe``: a live session asked to enumerate its own tools, skills, and
  slash commands. Runs daily — plugins and MCP servers arrive with no
  version bump. Sees the *active* surface only; a dormant feature never
  appears in a probe. Probe rows retire after three consecutive absences.
- ``notes``: the provider's public changelog, fetched with a bare
  conditional GET carrying no local state (product decision 2026-08-09,
  docs/capability-gap.md). A one-time backfill walks history in
  byte-capped chunks, then only entries above the last-seen version are
  read. The one category where notes are the primary source is dormant
  gates — features shipped behind an env var leave no other local trace.

The gap — active rows never seen in ingested usage — feeds one narrow
prompt section in ``analyze``. A capability with no matching pain in the
digest produces no recommendation; that judgment belongs to the LLM pass,
not to this module.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg_mod
from . import events, sanitize, store

logger = logging.getLogger(__name__)

PROVIDERS = ("claude", "codex")
KINDS = ("flag", "subcommand", "tool", "skill", "command", "note")

PROBE_KINDS = {"tool", "skill", "command"}
PROBE_RETIRE_MISSES = 3
PROBE_INTERVAL_MS = 20 * 3_600_000  # "daily" with slack for launchd jitter

_VERSION_TIMEOUT_S = 15
_HELP_TIMEOUT_S = 15
_MAX_SUBCOMMAND_HELPS = 40
_FETCH_HARD_CAP = 5_000_000  # bytes read off the wire, regardless of config
_NAME_MAX = 120
_DESC_MAX = 400
_NOTE_BODY_MAX = 1500

_VERSION_RE = re.compile(r"\d+\.\d+[\w.\-]*")
_GATE_RE = re.compile(r"\b((?:CLAUDE_CODE|CODEX)_[A-Z0-9_]{3,})\b")
_COMMAND_NAME_RE = re.compile(r"<command-name>\s*/?([\w:-]+)")


@dataclass
class Row:
    provider: str
    kind: str
    name: str
    description: str | None = None
    source: str = "help"
    version: str | None = None
    gate: str | None = None


@dataclass
class ProviderRefresh:
    provider: str
    version: str | None = None
    version_changed: bool = False
    scraped: int = 0
    probed: int = 0
    notes_entries: int = 0
    new_rows: int = 0
    updated_rows: int = 0
    retired_rows: int = 0
    new_names: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


def cap_id(provider: str, kind: str, name: str) -> str:
    return hashlib.sha256(f"{provider}|{kind}|{name}".encode()).hexdigest()


# --------------------------------------------------------------------------
# Cursor state — one JSON blob per provider, following the cursors convention
# --------------------------------------------------------------------------


def _cursor_key(provider: str) -> str:
    return f"capabilities:{provider}"


def load_state(conn: sqlite3.Connection, provider: str) -> dict:
    row = conn.execute(
        "SELECT position FROM cursors WHERE source = ?", (_cursor_key(provider),)
    ).fetchone()
    if row is None:
        return {}
    try:
        state = json.loads(row[0])
        return state if isinstance(state, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def save_state(conn: sqlite3.Connection, provider: str, state: dict) -> None:
    conn.execute(
        """INSERT INTO cursors(source, position, updated_at) VALUES(?, ?, ?)
           ON CONFLICT(source) DO UPDATE
             SET position = excluded.position, updated_at = excluded.updated_at""",
        (_cursor_key(provider), json.dumps(state), store.now_ms()),
    )


_CONSUMED_KEY = "capabilities:consumed"


def consumed_through(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT position FROM cursors WHERE source = ?", (_CONSUMED_KEY,)
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def mark_consumed(conn: sqlite3.Connection, *, now_ms: int | None = None) -> None:
    conn.execute(
        """INSERT INTO cursors(source, position, updated_at) VALUES(?, ?, ?)
           ON CONFLICT(source) DO UPDATE
             SET position = excluded.position, updated_at = excluded.updated_at""",
        (_CONSUMED_KEY, str(now_ms or store.now_ms()), store.now_ms()),
    )


# --------------------------------------------------------------------------
# Sanitize-on-ingest. Help text and probe output carry no user data in
# practice, but "in practice" is not the standard — every external string
# goes through redact, and a failing row is dropped, never kept raw.
# --------------------------------------------------------------------------


class _DropRow(Exception):
    pass


def _cleaned(
    conn: sqlite3.Connection, provider: str, value: str | None, *, limit: int
) -> str | None:
    if value is None:
        return None
    try:
        # Collapse all whitespace: every stored field is a single-line
        # value, and a newline smuggled through a plugin/MCP description
        # must not be able to break out of its list line in the prompt.
        text = " ".join(sanitize.redact(str(value)).text.split())
    except Exception as e:
        with contextlib.suppress(sqlite3.Error):
            store.log_redaction_failure(
                conn, f"capabilities:{provider}", f"{type(e).__name__}: {e}"
            )
        raise _DropRow from None
    return text[:limit] or None


def sanitize_rows(
    conn: sqlite3.Connection, rows: list[Row]
) -> list[Row]:
    out: list[Row] = []
    for r in rows:
        if r.kind not in KINDS:
            continue
        try:
            name = _cleaned(conn, r.provider, r.name, limit=_NAME_MAX)
            desc = _cleaned(conn, r.provider, r.description, limit=_DESC_MAX)
            gate = _cleaned(conn, r.provider, r.gate, limit=_NAME_MAX)
        except _DropRow:
            continue
        if not name:
            continue
        out.append(Row(r.provider, r.kind, name, desc, r.source, r.version, gate))
    return out


# --------------------------------------------------------------------------
# Source 1: static surface — the installed binary's own help
# --------------------------------------------------------------------------


def binary_version(binary: str) -> str | None:
    """Version of an installed provider binary, or None when absent/odd."""
    path = shutil.which(binary)
    if path is None:
        return None
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True, text=True, timeout=_VERSION_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = _VERSION_RE.search(proc.stdout or "") or _VERSION_RE.search(proc.stderr or "")
    return m.group(0) if m else None


_SECTION_RE = re.compile(r"^(options|flags|commands|subcommands)\s*:?\s*$", re.I)
_FLAG_RE = re.compile(
    r"^\s+(?:-\w[\w-]*,\s+)?(?P<long>--[A-Za-z][\w-]*)"
    r"(?:[ =]<[^>]*>|\s+\[[^\]]*\])?"
    r"(?:\s{2,}(?P<desc>\S.*))?$"
)
_CMD_RE = re.compile(
    r"^(?P<indent>\s+)(?P<name>[a-z][\w:-]*)"
    r"(?:\|[\w-]+)?(?:\s+\[[^\]]*\]|\s+<[^>]*>)*"
    r"(?:\s{2,}(?P<desc>\S.*))?$"
)


def parse_help_text(
    provider: str, text: str, *, version: str | None, prefix: str = ""
) -> list[Row]:
    """Parse commander/clap-style ``--help`` output into rows.

    Lenient by design: unrecognized lines are skipped, wrapped description
    continuations don't match either pattern (no leading dash; deep indent
    for the command form), and a section ends at the first unindented line.
    """
    rows: list[Row] = []
    section: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not line[0].isspace():
            m = _SECTION_RE.match(stripped)
            section = m.group(1).lower() if m else None
            continue
        if section in ("options", "flags"):
            fm = _FLAG_RE.match(line)
            if fm and fm.group("long") not in ("--help", "--version"):
                name = f"{prefix} {fm.group('long')}".strip()
                rows.append(Row(provider, "flag", name, fm.group("desc"),
                                "help", version))
        elif section in ("commands", "subcommands"):
            cm = _CMD_RE.match(line)
            if cm and len(cm.group("indent")) <= 4 and cm.group("name") != "help":
                name = f"{prefix} {cm.group('name')}".strip()
                rows.append(Row(provider, "subcommand", name, cm.group("desc"),
                                "help", version))
    return rows


def _run_help(argv: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_HELP_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout or proc.stderr or None


def scrape_help(provider: str, *, version: str | None) -> list[Row]:
    """Top-level help plus one level of subcommand help."""
    binary = shutil.which(provider)
    if binary is None:
        return []
    top = _run_help([binary, "--help"])
    if top is None:
        return []
    rows = parse_help_text(provider, top, version=version)
    subs = [r.name for r in rows if r.kind == "subcommand" and " " not in r.name]
    for sub in subs[:_MAX_SUBCOMMAND_HELPS]:
        sub_help = _run_help([binary, sub, "--help"])
        if sub_help:
            rows.extend(
                parse_help_text(provider, sub_help, version=version, prefix=sub)
            )
    return rows


def settings_env_names(settings_path: Path) -> set[str]:
    """Env var *names* set in an agent's settings file. Names only — values
    are parsed as part of the JSON but never returned, stored, or compared
    beyond key extraction."""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return set()
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict):
        return set()
    return {str(k) for k in env}


def claude_settings_path(cfg: cfg_mod.Config) -> Path:
    # projects_root is ~/.claude/projects; settings.json is its sibling.
    return cfg.claude.projects_root.parent / "settings.json"


# --------------------------------------------------------------------------
# Source 2: live probe — asking the installed build what it can do
# --------------------------------------------------------------------------


PROBE_PROMPT = """\
You are being probed by undrudge, a local tool that inventories agent
capabilities. Enumerate every tool, skill (including plugin skills), and
slash command available to you in this session — built-in and installed
alike, MCP tools included.

Your entire answer must be a single JSON array, no prose, no code fences.
Each element:

  {"kind": "tool" | "skill" | "command",
   "name": "exact name as invocable, e.g. SendMessage or /review",
   "description": "one line: what it does"}

Include every entry you can see, even ones that seem obscure. Do not
invent entries you are not certain exist in this session. Do not run any
tools other than what the harness instructions require for delivering
your answer.
"""


def parse_probe_items(provider: str, items: list, *, version: str | None) -> list[Row]:
    rows: list[Row] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        name = str(item.get("name") or "").strip()
        if kind not in PROBE_KINDS or not name:
            continue
        desc = str(item.get("description") or "").strip() or None
        rows.append(Row(provider, kind, name, desc, "probe", version))
    return rows


def probe_claude(cfg: cfg_mod.Config, *, workdir: Path) -> list:
    """One live enumeration call through the same file-based seam analyze
    uses. Returns the parsed JSON items; raises on any failure — the caller
    isolates it."""
    from . import analyze, llm  # local import: analyze imports this module

    resolved = llm.resolve_command(cfg.llm.command)
    invoker = analyze.FileBasedInvoker(
        command_argv=[str(resolved)],
        workdir=workdir,
        timeout=cfg.llm.timeout_seconds,
    )
    response = invoker(PROBE_PROMPT)
    return analyze.extract_json_array(response)


def probe_codex(cfg: cfg_mod.Config, *, workdir: Path) -> list:
    """Codex enumeration via ``codex exec``, stdout-based. The final message
    lands on stdout amid log lines; the tolerant array scan handles that."""
    from . import analyze  # local import, same reason as above

    binary = shutil.which("codex")
    if binary is None:
        raise FileNotFoundError("codex not on PATH")
    workdir.mkdir(parents=True, exist_ok=True)
    argv = [binary, "exec", "--skip-git-repo-check", PROBE_PROMPT]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=cfg.llm.timeout_seconds, cwd=workdir,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"codex exec timed out after {cfg.llm.timeout_seconds}s") from e
    if proc.returncode != 0 and "--skip-git-repo-check" in (proc.stderr or ""):
        # Older codex builds don't know the flag; retry bare.
        proc = subprocess.run(
            [binary, "exec", PROBE_PROMPT], capture_output=True, text=True,
            timeout=cfg.llm.timeout_seconds, cwd=workdir,
        )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-500:]
        raise RuntimeError(f"codex exec exited {proc.returncode}: {tail}")
    return analyze.extract_json_array(proc.stdout or "")


_PROBERS = {"claude": probe_claude, "codex": probe_codex}


# --------------------------------------------------------------------------
# Source 4: release notes — the one network call
# --------------------------------------------------------------------------


def fetch_notes(
    url: str, *, timeout_s: int, etag: str | None = None
) -> tuple[str, str | None, str | None]:
    """Fetch a changelog. Returns ``(status, text, etag)`` with status one of
    ``"ok"`` / ``"unchanged"`` / ``"failed"``.

    The request is a bare GET of exactly the configured URL. No query
    parameters, no local state — the only optional header is the cache
    validator from the *previous fetch of the same URL*. ``file://`` is
    accepted for mirrors and air-gapped hosts (no conditional there).
    """
    is_file = url.startswith("file://")
    headers = {"User-Agent": "undrudge-capabilities"}
    if etag and not is_file:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    # operator-configured (https default, file:// for mirrors); no local state.
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = resp.read(_FETCH_HARD_CAP)
            new_etag = None if is_file else resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return "unchanged", None, etag
        return "failed", None, etag
    except (urllib.error.URLError, OSError, ValueError):
        return "failed", None, etag
    return "ok", data.decode("utf-8", errors="replace"), new_etag


_ENTRY_HEADER_RE = re.compile(r"^#{1,3}\s*\[?v?(\d+\.\d+[\w.\-]*)\]?.*$", re.M)


def parse_changelog(text: str) -> list[tuple[str, str]]:
    """Split a changelog into ``(version, body)`` entries in file order
    (conventionally newest-first). No headers parsed → one synthetic entry
    so a plain-text notes file still flows through."""
    matches = list(_ENTRY_HEADER_RE.finditer(text))
    if not matches:
        body = text.strip()
        if not body:
            return []
        # Key the synthetic entry by content, so an edited headerless file
        # reads as a new entry instead of freezing behind a stable name.
        digest = hashlib.sha256(body.encode("utf-8", errors="replace"))
        return [(f"unversioned-{digest.hexdigest()[:8]}", body)]
    entries: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append((m.group(1), text[m.end():end].strip()))
    return entries


def select_entries(
    entries: list[tuple[str, str]], state: dict, *, budget_bytes: int
) -> tuple[list[tuple[str, str]], dict]:
    """Pick which entries this run processes, keeping the processed range
    contiguous from the top of the file.

    State: ``{"hi": newest processed version, "lo": oldest processed
    version, "done": backfill finished}``. New entries arriving above
    ``hi`` are taken first (closest-to-hi first, so the range stays
    contiguous under the byte budget); then backfill continues below
    ``lo``. First run starts at the top — newest entries are the most
    relevant, and the teams-style deep-history entries arrive as the
    backfill walks down. If ``hi``/``lo`` vanish from the file (upstream
    history rewrite), everything resets and the walk starts over —
    dedupe upstream makes re-processing harmless.
    """
    if not entries:
        return [], state
    versions = [v for v, _ in entries]
    hi, lo = state.get("hi"), state.get("lo")
    if hi not in versions or lo not in versions:
        hi = lo = None
    selected: list[tuple[str, str]] = []
    spent = 0

    def take(i: int) -> bool:
        nonlocal spent
        cost = len(entries[i][1].encode("utf-8", errors="replace")) + 100
        if selected and spent + cost > budget_bytes:
            return False
        selected.append(entries[i])
        spent += cost
        return True

    if hi is None:
        i = 0
        while i < len(versions) and take(i):
            i += 1
        if not selected:
            return [], state
        new_state = dict(state)
        new_state.update(
            hi=selected[0][0], lo=selected[-1][0],
            done=selected[-1][0] == versions[-1],
        )
        return selected, new_state

    idx_hi, idx_lo = versions.index(hi), versions.index(lo)
    # Entries that arrived above the processed range, nearest-first.
    for i in range(idx_hi - 1, -1, -1):
        if not take(i):
            break
        hi = versions[i]
    # Extend below the processed range: the backfill walk, and — after it
    # finished — anything appended at the bottom (oldest-first mirrors
    # grow that way).
    for i in range(idx_lo + 1, len(versions)):
        if not take(i):
            break
        lo = versions[i]
    new_state = dict(state)
    new_state.update(hi=hi, lo=lo, done=lo == versions[-1])
    return selected, new_state


def notes_rows(
    provider: str, selected: list[tuple[str, str]]
) -> list[Row]:
    rows = []
    for version, body in selected:
        if not body:
            continue
        gate_m = _GATE_RE.search(body)
        rows.append(Row(
            provider, "note", version, body[:_NOTE_BODY_MAX],
            "notes", version, gate_m.group(1) if gate_m else None,
        ))
    return rows


# --------------------------------------------------------------------------
# Upsert + retirement
# --------------------------------------------------------------------------


def upsert_rows(
    conn: sqlite3.Connection, rows: list[Row], *, now_ms: int | None = None
) -> tuple[int, int, list[str]]:
    """Insert or refresh rows. Returns (new, updated, new_names). A row
    reappearing after retirement is revived — the surface has it again."""
    ts = now_ms or store.now_ms()
    new = updated = 0
    new_names: list[str] = []
    for r in rows:
        exists = conn.execute(
            "SELECT 1 FROM capabilities WHERE provider=? AND kind=? AND name=?",
            (r.provider, r.kind, r.name),
        ).fetchone()
        conn.execute(
            """INSERT INTO capabilities(
                   id, provider, kind, name, description, source, version,
                   gate, first_seen, last_seen, retired_at, probe_misses)
               VALUES(?,?,?,?,?,?,?,?,?,?,NULL,0)
               ON CONFLICT(provider, kind, name) DO UPDATE SET
                 description = COALESCE(excluded.description, capabilities.description),
                 gate = COALESCE(excluded.gate, capabilities.gate),
                 last_seen = excluded.last_seen,
                 retired_at = NULL,
                 probe_misses = 0""",
            (cap_id(r.provider, r.kind, r.name), r.provider, r.kind, r.name,
             r.description, r.source, r.version, r.gate, ts, ts),
        )
        if exists:
            updated += 1
        else:
            new += 1
            new_names.append(f"{r.kind}:{r.name}")
    return new, updated, new_names


def retire_missing_help(
    conn: sqlite3.Connection, provider: str, seen: set[tuple[str, str]],
    *, now_ms: int | None = None,
) -> int:
    """A help-sourced row absent from a successful scrape is gone from the
    surface. Only the static source gets this direct retirement."""
    ts = now_ms or store.now_ms()
    retired = 0
    for row in conn.execute(
        """SELECT id, kind, name FROM capabilities
           WHERE provider=? AND source='help' AND retired_at IS NULL""",
        (provider,),
    ).fetchall():
        if (row["kind"], row["name"]) not in seen:
            conn.execute(
                "UPDATE capabilities SET retired_at=? WHERE id=?", (ts, row["id"]),
            )
            retired += 1
    return retired


def bump_probe_misses(
    conn: sqlite3.Connection, provider: str, seen: set[tuple[str, str]],
    *, now_ms: int | None = None,
) -> int:
    """One probe omission is generative wobble; three in a row is an
    uninstalled plugin. Applies only to probe-sourced rows."""
    ts = now_ms or store.now_ms()
    retired = 0
    for row in conn.execute(
        """SELECT id, kind, name, probe_misses FROM capabilities
           WHERE provider=? AND source='probe' AND retired_at IS NULL""",
        (provider,),
    ).fetchall():
        if (row["kind"], row["name"]) in seen:
            continue
        misses = row["probe_misses"] + 1
        if misses >= PROBE_RETIRE_MISSES:
            conn.execute(
                "UPDATE capabilities SET probe_misses=?, retired_at=? WHERE id=?",
                (misses, ts, row["id"]),
            )
            retired += 1
        else:
            conn.execute(
                "UPDATE capabilities SET probe_misses=? WHERE id=?",
                (misses, row["id"]),
            )
    return retired


# --------------------------------------------------------------------------
# Usage inventory — the "not using" half, already sitting in the DB
# --------------------------------------------------------------------------


def used_names(conn: sqlite3.Connection, provider: str) -> dict[str, set[str]]:
    """Lowercased names observed in ingested history, keyed by kind."""
    used: dict[str, set[str]] = {k: set() for k in KINDS}

    for row in conn.execute(
        """SELECT DISTINCT m.tool_name FROM messages m
           JOIN sessions s ON m.session_id = s.id
           WHERE s.source = ? AND m.tool_name IS NOT NULL""",
        (provider,),
    ):
        used["tool"].add(row[0].lower())

    # Slash invocations typed by the user. Two transcript shapes: a
    # *registered* command lands wrapped — `<command-name>/model</command-name>…`
    # — while unregistered slash text arrives verbatim. Match both; only
    # matching the bare form would mark every real command "never used".
    for row in conn.execute(
        """SELECT DISTINCT substr(m.text, 1, 200) FROM messages m
           JOIN sessions s ON m.session_id = s.id
           WHERE s.source = ? AND m.role = 'user'
             AND (m.text LIKE '/%' OR m.text LIKE '<command-name>%')""",
        (provider,),
    ):
        text = row[0] or ""
        m = _COMMAND_NAME_RE.search(text)
        token = m.group(1) if m else text.split()[0].lstrip("/")
        token = token.lower().strip()
        if token:
            used["command"].add(token)
            used["skill"].add(token)

    # Skills invoked through the Skill tool carry the name in tool_input.
    for row in conn.execute(
        """SELECT DISTINCT m.tool_input FROM messages m
           JOIN sessions s ON m.session_id = s.id
           WHERE s.source = ? AND m.tool_name = 'Skill'
             AND m.tool_input IS NOT NULL""",
        (provider,),
    ):
        with contextlib.suppress(json.JSONDecodeError, TypeError, AttributeError):
            skill = json.loads(row[0]).get("skill")
            if skill:
                used["skill"].add(str(skill).lower())

    # Shell rows: `claude <sub> [<sub2>] --flag ...`. Token scan stops at a
    # `--` separator or the first quoted token — words inside a prompt
    # argument (`claude -p "try --verbose"`) are not flag usage, and a
    # false "used" hides a real gap.
    for row in conn.execute(
        "SELECT DISTINCT command FROM commands WHERE command LIKE ? LIMIT 20000",
        (f"{provider} %",),
    ):
        tokens = row[0].split()
        sub = tokens[1] if len(tokens) > 1 and not tokens[1].startswith("-") else None
        if sub:
            used["subcommand"].add(sub.lower())
            if len(tokens) > 2 and not tokens[2].startswith("-"):
                used["subcommand"].add(f"{sub.lower()} {tokens[2].lower()}")
        for tok in tokens[1:]:
            if tok == "--" or "'" in tok or '"' in tok:
                break
            if tok.startswith("--"):
                flag = tok.split("=")[0].lower()
                used["flag"].add(flag)
                if sub:
                    used["flag"].add(f"{sub.lower()} {flag}")
    return used


def _is_used(row: sqlite3.Row, used: dict[str, set[str]]) -> bool:
    kind, name = row["kind"], row["name"].lower()
    if kind == "note":
        return False
    if kind in ("skill", "command"):
        candidates = {name, name.lstrip("/"), name.split(":")[-1]}
        return bool(candidates & (used["skill"] | used["command"]))
    # Flags and subcommands match exactly (qualified names included): a
    # historical `claude mcp list` marks `mcp` and `mcp list` used — never
    # `mcp add`, and never `--transport` under some other subcommand.
    return name in used[kind]


# --------------------------------------------------------------------------
# The gap, rendered for the analyze prompt and for `capabilities --show`
# --------------------------------------------------------------------------


@dataclass
class GapRow:
    provider: str
    kind: str
    name: str
    description: str | None
    gate: str | None
    first_seen: int
    is_new: bool
    gate_enabled: bool


def gap_rows(
    conn: sqlite3.Connection, cfg: cfg_mod.Config
) -> list[GapRow]:
    """Active, never-used rows across providers, newest first."""
    consumed = consumed_through(conn)
    env_names = settings_env_names(claude_settings_path(cfg))
    out: list[GapRow] = []
    for provider in PROVIDERS:
        used = used_names(conn, provider)
        for row in conn.execute(
            """SELECT * FROM capabilities
               WHERE provider=? AND retired_at IS NULL
               ORDER BY first_seen DESC""",
            (provider,),
        ).fetchall():
            if _is_used(row, used):
                continue
            gate = row["gate"]
            out.append(GapRow(
                provider=provider, kind=row["kind"], name=row["name"],
                description=row["description"], gate=gate,
                first_seen=row["first_seen"],
                is_new=row["first_seen"] > consumed,
                gate_enabled=bool(gate and gate in env_names),
            ))
    out.sort(key=lambda g: (not g.is_new, -g.first_seen))
    return out


_SECTION_BYTE_CAP = 12_000
_OLDER_ROW_CAP = 15


def _gap_line(g: GapRow) -> str:
    desc = f" — {g.description}" if g.description else ""
    if g.gate and g.gate_enabled:
        state = f" (gate {g.gate} enabled in settings — enabled but never used)"
    elif g.gate:
        state = f" (dormant — requires {g.gate})"
    else:
        state = ""
    return f"- [{g.provider} {g.kind}] {g.name}{desc}{state}"


def load_gap_template() -> str:
    from importlib.resources import files

    return (files("undrudge") / "prompts" / "capability_gap.md").read_text()


def render_gap_section(conn: sqlite3.Connection, cfg: cfg_mod.Config) -> str:
    """The capability-gap prompt section, or "" when there is nothing new
    to say. Never raises on a healthy DB; callers isolate the rest."""
    rows = gap_rows(conn, cfg)
    new = [g for g in rows if g.is_new]
    if not new:
        return ""
    older = [g for g in rows if not g.is_new][:_OLDER_ROW_CAP]

    def capped(items: list[GapRow]) -> tuple[str, int]:
        lines, spent, dropped = [], 0, 0
        for g in items:
            line = _gap_line(g)
            if spent + len(line) > _SECTION_BYTE_CAP:
                dropped += 1
                continue
            lines.append(line)
            spent += len(line)
        return "\n".join(lines), dropped

    new_md, new_dropped = capped(new)
    if new_dropped:
        new_md += f"\n- (+{new_dropped} more new rows not shown)"
    older_md, older_dropped = capped(older)
    if older_dropped:
        older_md += f"\n- (+{older_dropped} more not shown)"
    return (
        load_gap_template()
        .replace("{new_rows}", new_md or "_(none)_")
        .replace("{older_rows}", older_md or "_(none)_")
    )


# --------------------------------------------------------------------------
# Refresh orchestration
# --------------------------------------------------------------------------


def refresh(
    conn: sqlite3.Connection,
    cfg: cfg_mod.Config,
    *,
    force: bool = False,
    with_probe: bool = False,
    probers: dict | None = None,
) -> list[ProviderRefresh]:
    """Refresh the inventory. Cheap by default: help scrape and notes fetch
    are version-gated, the probe is clock-gated to once a day and only runs
    when ``with_probe`` (gather passes False — no LLM calls in gather;
    the daily ``analyze day`` passes True). Every stage is failure-isolated
    per provider; nothing here is ever fatal to the caller."""
    probers = probers if probers is not None else _PROBERS
    results: list[ProviderRefresh] = []
    for provider in PROVIDERS:
        result = ProviderRefresh(provider=provider)
        results.append(result)
        version = binary_version(provider)
        if version is None:
            result.skipped_reason = "binary not on PATH (or no parseable version)"
            continue
        result.version = version
        state = load_state(conn, provider)
        now = store.now_ms()
        result.version_changed = force or state.get("version") != version

        if result.version_changed:
            try:
                scraped = sanitize_rows(conn, scrape_help(provider, version=version))
                result.scraped = len(scraped)
                if scraped:
                    n, u, names = upsert_rows(conn, scraped, now_ms=now)
                    result.new_rows += n
                    result.updated_rows += u
                    result.new_names += names
                    result.retired_rows += retire_missing_help(
                        conn, provider,
                        {(r.kind, r.name) for r in scraped}, now_ms=now,
                    )
                state["version"] = version
            except Exception as e:
                result.errors.append(f"scrape: {type(e).__name__}: {e}")
                logger.exception("capability help scrape failed (%s)", provider)
                events.record(
                    cfg.paths.events_log, "capability_scrape_failed",
                    {"provider": provider, "error": f"{type(e).__name__}: {e}"},
                    conn=conn,
                )

        url = cfg.capabilities.release_notes_urls.get(provider, "")
        notes_state = state.get("notes") or {}
        if cfg.capabilities.fetch_release_notes and url and (
            force or result.version_changed or not notes_state.get("done")
        ):
            try:
                # Conditional GET only once the backfill has finished: while
                # the walk is mid-history, a 304 would stall it — the state
                # machine needs the full text to continue below `lo`.
                status, text, etag = fetch_notes(
                    url, timeout_s=cfg.capabilities.fetch_timeout_s,
                    etag=notes_state.get("etag") if notes_state.get("done") else None,
                )
                if status == "failed":
                    # Degrade silently to local-only; doctor shows staleness.
                    result.errors.append("notes: fetch failed (offline is fine)")
                elif status == "ok":
                    entries = parse_changelog(text or "")
                    selected, notes_state = select_entries(
                        entries, notes_state,
                        budget_bytes=cfg.capabilities.max_notes_bytes,
                    )
                    notes_state["etag"] = etag
                    result.notes_entries = len(selected)
                    n_rows = sanitize_rows(conn, notes_rows(provider, selected))
                    if n_rows:
                        n, u, names = upsert_rows(conn, n_rows, now_ms=now)
                        result.new_rows += n
                        result.updated_rows += u
                        result.new_names += names
                    state["notes"] = notes_state
            except Exception as e:
                result.errors.append(f"notes: {type(e).__name__}: {e}")
                logger.exception("capability notes fetch failed (%s)", provider)

        probe_due = force or (
            now - int(state.get("probed_at") or 0) >= PROBE_INTERVAL_MS
        )
        if with_probe and cfg.capabilities.probe and probe_due:
            prober = probers.get(provider)
            if prober is not None:
                try:
                    workdir = cfg.paths.db.parent / "runs" / (
                        f"{now}-probe-{provider}"
                    )
                    items = prober(cfg, workdir=workdir)
                    probed = sanitize_rows(
                        conn, parse_probe_items(provider, items, version=version)
                    )
                    result.probed = len(probed)
                    if probed:
                        n, u, names = upsert_rows(conn, probed, now_ms=now)
                        result.new_rows += n
                        result.updated_rows += u
                        result.new_names += names
                        result.retired_rows += bump_probe_misses(
                            conn, provider,
                            {(r.kind, r.name) for r in probed}, now_ms=now,
                        )
                    state["probed_at"] = now
                except Exception as e:
                    result.errors.append(f"probe: {type(e).__name__}: {e}")
                    logger.exception("capability probe failed (%s)", provider)
                    events.record(
                        cfg.paths.events_log, "capability_probe_failed",
                        {"provider": provider, "error": f"{type(e).__name__}: {e}"},
                        conn=conn,
                    )

        save_state(conn, provider, state)
        if result.new_rows or result.retired_rows or result.probed or result.scraped:
            events.record(
                cfg.paths.events_log, "capability_refresh",
                {
                    "provider": provider,
                    "version": version,
                    "version_changed": result.version_changed,
                    "new": result.new_rows,
                    "updated": result.updated_rows,
                    "retired": result.retired_rows,
                    "probed": result.probed,
                    "notes_entries": result.notes_entries,
                    "new_names": result.new_names[:20],
                },
                conn=conn,
            )
        for err in result.errors:
            if err.startswith("notes:"):
                events.record(
                    cfg.paths.events_log, "capability_fetch_failed",
                    {"provider": provider, "error": err}, conn=conn,
                )
    return results
