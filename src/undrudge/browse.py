"""``undrudge browse`` — an fzf sidecar for triaging recommendations.

`list` answers "what's there"; `browse` answers "what do I do about it". It's
the same file-and-status model the CLI verbs already use, wrapped in a picker:
recommendations on the left (open ones first, closed ones dimmed), the rendered
rec in the preview pane on the right, and the actions that make sense *for the
highlighted rec's status* spelled out at the top of that pane.

Nothing new is persisted. Every key routes through ``recommend.set_status`` and
``events.record``, exactly as ``undrudge dismiss`` does — the picker is a faster
hand on the same levers, and closing it leaves no state behind but the temp
file holding the preview mode.

The ``__browse-*`` subcommands are internal hooks the fzf key bindings call
back into (via ``python -m undrudge``, so the child is always *this* undrudge
and not whatever is first on PATH). You never invoke them by hand.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import events, recommend

_GREEN = "\033[32m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[2m"
_RESET = "\033[0m"

# Statuses that still want a decision from a human. They sort to the top of the
# picker; everything else is history and gets dimmed.
OPEN_STATUSES = ("logged", "dispatched")

# glyph, colour — the status column, readable at a glance without reading words.
_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "logged": ("○", _YELLOW),
    "dispatched": ("→", _CYAN),
    "implemented": ("✓", _GREEN),
    "dismissed": ("✗", _DIM),
    "rejected": ("✗", _RED),
}

# action name -> (target status, prompts for a reason)
ACTIONS: dict[str, tuple[str, bool]] = {
    "implement": ("implemented", False),
    "dismiss": ("dismissed", True),
    "reject": ("rejected", True),
    "reopen": ("logged", False),
}

KEYS_HELP = """\
undrudge browse — interactive keys

  triage the highlighted (or Tab-selected) recommendations
    Ctrl-A         implement — mark it built, stop it being re-proposed
    Ctrl-D         dismiss   — prompts for a reason (see below)
    Ctrl-X         reject    — prompts for a reason
    Ctrl-L         reopen    — put a closed rec back to `logged`
    Tab            select / deselect (actions apply to the whole selection)
    Each triage key answers to Alt too (⌥a ⌥d ⌥x ⌥l), for when the Ctrl
    chord is already spoken for — tmux prefix, screen, a shell binding.

  Reasons matter more than the status flip. A dismissed rec's reason is
  written to its frontmatter and events.jsonl, listed in the next analyze
  prompt under "do not re-propose variants", and used by the write-time
  dedupe gate to suppress rephrasings. "we don't want a background daemon"
  buys silence on that whole idea; an empty reason buys none.

  read
    Enter / Ctrl-O open the rec full-screen in less (selectable, searchable)
    Ctrl-T         flip the preview between the rec and its audit trail
    Shift-Up/Down  scroll the preview
    ?              toggle the preview pane on / off

  yank
    Ctrl-Y         copy the rec as a hand-off — the rec verbatim, where its
                   evidence came from, and the `undrudge implement/dismiss`
                   lines that close the loop. Paste it straight into the
                   session you want to build it. (`undrudge copy <id>` does
                   the same from a shell.)
    Ctrl-P         copy the rec's file path

  navigate
    Alt-g / Alt-G  jump to newest / oldest (or Home / End)
    Ctrl-b/Ctrl-f  page up / down (vim-style)
    Ctrl-R         reload (pick up recs an analyze run wrote since launch)

  Ctrl-H           show this help     ·     Esc  quit

Rows: date · id · status · scope · title. Open recs (logged, dispatched)
sort first; closed ones are dimmed but still searchable — type `dismissed`
to see what you've already said no to, `^L` to take one back.

Acting on a rec drops it out of the open block and slides the next one under
your cursor, so a triage pass is ^D · reason · ^D · reason without ever
touching the arrow keys. The preview always shows what the cursor is on now —
glance at it before firing the next key.
"""


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


@dataclass
class Filters:
    status: str | None = None
    since_ms: int | None = None
    scope: str | None = None
    limit: int = 500


def fetch_rows(conn, filters: Filters) -> list[dict[str, Any]]:
    """Recs in picker order: open first (newest first), then closed by when
    they were closed. The thing you can still act on is never below the
    thing you already dealt with."""
    rows = recommend.list_recs(
        conn,
        status=filters.status,
        since_ms=filters.since_ms,
        scope=filters.scope,
        limit=filters.limit,
    )
    return sorted(
        rows,
        key=lambda r: (
            0 if r["status"] in OPEN_STATUSES else 1,
            -(r["created_at"] if r["status"] in OPEN_STATUSES else r["updated_at"]),
        ),
    )


def _date(ms: int | None) -> str:
    if not ms:
        return "----------"
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def _stamp(ms: int | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def render_row(rec: dict[str, Any]) -> str:
    """One tab-delimited picker line: id (hidden key) + the visible column."""
    status = rec["status"]
    glyph, colour = _STATUS_STYLE.get(status, ("·", ""))
    title = (rec["title"] or "").replace("\t", " ")[:110]
    visible = (
        f"{_date(rec['created_at'])}  {rec['id'][:12]}  "
        f"{colour}{glyph} {status:<11}{_RESET} "
        f"{_DIM}{rec['scope']:<6}{_RESET} "
    )
    visible += title if status in OPEN_STATUSES else f"{_DIM}{title}{_RESET}"
    return f"{rec['id']}\t{visible}"


def render_rows(rows: list[dict[str, Any]]) -> str:
    return "".join(render_row(r) + "\n" for r in rows)


def ids_from_rows_file(path: str) -> list[str]:
    """Pull rec ids out of the file fzf hands us for ``{+f}`` (one row per
    line, id in the first tab-delimited field)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    ids = []
    for line in text.splitlines():
        rec_id = line.split("\t", 1)[0].strip()
        if rec_id:
            ids.append(rec_id)
    return ids


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------


def actions_for(status: str) -> list[str]:
    """The actions that make sense for a rec in this status."""
    if status == "logged":
        return ["^A implement", "^D dismiss", "^X reject"]
    if status == "dispatched":
        return ["^A implement", "^D dismiss", "^L back to logged"]
    return ["^L reopen"]


def preview_markdown(conn, rec_id: str, *, mode: str = "rec", events_log: Path | None = None) -> str:
    """Markdown for the preview pane (piped through glow by the caller)."""
    row = conn.execute(
        """SELECT id, scope, title, signature, body_path, status, reason,
                  created_at, updated_at
             FROM recommendations WHERE id = ?""",
        (rec_id,),
    ).fetchone()
    if row is None:
        return f"_no recommendation with id `{rec_id[:12]}`_\n"
    rec = dict(row)

    if mode == "trail":
        return _trail_markdown(rec, events_log)

    fm, body = recommend.parse_rec_file(rec["body_path"])
    heading, prose = _split_heading(body)
    facts = [f"**{rec['status']}**", rec["scope"]]
    if fm.get("confidence"):
        facts.append(f"{fm['confidence']} confidence")
    if fm.get("automation_form"):
        facts.append(fm["automation_form"])
    if fm.get("target_scope"):
        facts.append(fm["target_scope"])

    # Title first — you decide what a rec *is* before you care when it landed.
    # Backticks are avoided in this header block: glow renders inline code as a
    # loud coloured chip, which turns a status line into a row of tags.
    out = [
        f"# {heading or rec['title']}",
        "",
        " · ".join(facts),
        "",
        f"created {_stamp(rec['created_at'])} · updated {_stamp(rec['updated_at'])} "
        f"· id {rec['id'][:12]}",
        "",
        "> " + " · ".join(["⏎ open", *actions_for(rec["status"]), "^T trail"]),
        "",
    ]
    if rec.get("reason"):
        # A reopened rec keeps the reason it was declined with, which would
        # otherwise read as if the open rec had just been turned down.
        label = "reason" if rec["status"] not in OPEN_STATUSES else "last reason"
        out += [f"**{label}:** {rec['reason']}", ""]
    out.append("---")
    out.append("")

    if prose.strip():
        out.append(prose.rstrip())
    else:
        out += [
            f"_body file missing: `{rec['body_path']}`_",
            "",
            f"signature: `{rec['signature']}`",
        ]

    refs = fm.get("evidence_refs") or []
    if refs:
        out += ["", "---", "", "**evidence refs**", ""]
        for ref in refs[:12]:
            if not isinstance(ref, dict):
                continue
            note = f" — {ref.get('note')}" if ref.get("note") else ""
            out.append(f"- `{ref.get('source')}` `{ref.get('external_id')}`{note}")
    return "\n".join(out) + "\n"


def _split_heading(body: str) -> tuple[str, str]:
    """Peel a rec body's own ``# Title`` off so the preview can put the status
    line *under* the title instead of above it."""
    lines = body.lstrip().splitlines()
    if lines and lines[0].startswith("# "):
        return lines[0][2:].strip(), "\n".join(lines[1:]).lstrip("\n")
    return "", body


def _trail_markdown(rec: dict[str, Any], events_log: Path | None) -> str:
    """Every audit-trail line this rec ever produced. Answers "what happened
    to this one, and when did I say no?" without leaving the picker."""
    out = [f"# trail — {rec['id'][:12]}", "", rec["title"], ""]
    entries = read_trail(events_log, rec["id"]) if events_log else []
    if not entries:
        out.append(
            "_no events recorded_" if events_log
            else "_no events log configured_"
        )
        return "\n".join(out) + "\n"

    for e in entries:
        when = _stamp(e.get("ts"))
        line = f"- {when} — **{e.get('event')}**"
        bits = []
        if e.get("from_status"):
            bits.append(f"{e['from_status']} → {e.get('event', '').removeprefix('rec_')}")
        if e.get("reason"):
            bits.append(f"reason: {e['reason']}")
        if e.get("signature"):
            bits.append(f"`{e['signature']}`")
        if e.get("path"):
            bits.append(f"`{e['path']}`")
        if bits:
            line += " — " + " · ".join(bits)
        out.append(line)
    out += ["", f"_{len(entries)} event(s) · {events_log}_"]
    return "\n".join(out) + "\n"


def read_trail(events_log: Path | None, rec_id: str, *, limit: int = 60) -> list[dict[str, Any]]:
    """Events mentioning this rec, oldest first. Matches on the full id or the
    12-char short form, since dispatch logs the short one."""
    if not events_log or not events_log.exists():
        return []
    hits: list[dict[str, Any]] = []
    try:
        with events_log.open(encoding="utf-8") as fp:
            for line in fp:
                if rec_id[:12] not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = str(entry.get("id") or entry.get("rec_id") or "")
                if rid and (rec_id.startswith(rid) or rid.startswith(rec_id[:12])):
                    hits.append(entry)
    except OSError:
        return []
    return hits[-limit:]


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


def apply_action(
    conn,
    cfg: config_mod.Config,
    ids: list[str],
    action: str,
    *,
    reason: str | None = None,
) -> list[str]:
    """Flip status for each id through the one status-mutation path.

    Returns one human-readable line per id. Already-in-that-status recs are
    reported and skipped rather than churning updated_at — `updated_at` is what
    the analyze prompt uses to decide what was *recently* declined.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}; expected one of {sorted(ACTIONS)}")
    target, _ = ACTIONS[action]
    lines = []
    for rec_id in ids:
        current = conn.execute(
            "SELECT status FROM recommendations WHERE id = ?", (rec_id,)
        ).fetchone()
        if current is None:
            lines.append(f"{rec_id[:12]}: not found")
            continue
        if current["status"] == target:
            lines.append(f"{rec_id[:12]}: already {target}")
            continue
        result = recommend.set_status(conn, rec_id, target, reason=reason)
        events.record(
            cfg.paths.events_log,
            f"rec_{target}",
            {
                "id": result.matched_id,
                "from_status": result.old_status,
                "reason": reason,
                "body_path": str(result.body_path) if result.body_path else None,
                "via": "browse",
            },
        )
        lines.append(f"{rec_id[:12]}: {result.old_status} → {target}")
    return lines


def prompt_reason(action: str, titles: list[str]) -> tuple[bool, str | None]:
    """Ask why. Returns (proceed, reason). Ctrl-C / Ctrl-D cancels the action.

    The prompt is the confirmation step for the destructive-ish verbs: there is
    no other "are you sure" between the key and the status flip.
    """
    print(f"\n{action} {len(titles)} recommendation(s):")
    for t in titles[:5]:
        print(f"  · {t}")
    if len(titles) > 5:
        print(f"  · … and {len(titles) - 5} more")
    print(
        "\nA reason is fed back to the analyzer as 'do not re-propose variants'.\n"
        "Empty = no reason. Ctrl-C cancels."
    )
    try:
        reason = input(f"{action} reason> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\ncancelled")
        return False, None
    return True, reason or None


# What `undrudge copy` / ^Y can put on the clipboard.
PAYLOADS = ("handoff", "body", "path", "id")


def payload_for(conn, rec: dict[str, Any], what: str) -> str:
    """The clipboard text for one rec.

    ``handoff`` is the default everywhere: the rec verbatim plus where its
    evidence came from and the commands that close the loop — the thing you
    paste into an implementing session. ``body`` is the rec markdown alone,
    ``path`` its file, ``id`` the short fingerprint.
    """
    if what == "path":
        return str(rec.get("body_path") or "")
    if what == "id":
        return rec["id"][:12]
    if what == "body":
        _, body = recommend.parse_rec_file(rec.get("body_path"))
        return body or ""
    return recommend.render_handoff(conn, rec)


def copy_to_clipboard(text: str) -> bool:
    """Best-effort clipboard write. False when no clipboard tool is around."""
    for cmd in (
        ["pbcopy"],
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text, text=True, check=False, timeout=10)
                return True
            except (OSError, subprocess.SubprocessError):
                return False
    return False


# --------------------------------------------------------------------------
# The picker
# --------------------------------------------------------------------------


def _self_cmd() -> str:
    """Shell-quoted re-invocation of *this* undrudge.

    ``python -m undrudge`` and not ``which undrudge``: the picker must call
    back into the interpreter it is already running under, or a stale copy
    earlier on PATH answers the key bindings.
    """
    return " ".join(shlex.quote(p) for p in (sys.executable, "-m", "undrudge"))


def _filter_args(filters: Filters) -> str:
    """Filter flags, re-passed to __browse-list so Ctrl-R reloads the same set."""
    args = []
    if filters.status:
        args += ["--status", filters.status]
    if filters.since_ms is not None:
        args += ["--since-ms", str(filters.since_ms)]
    if filters.scope:
        args += ["--scope", filters.scope]
    args += ["--limit", str(filters.limit)]
    return " ".join(shlex.quote(a) for a in args)


def _renderer(width: str) -> str:
    """glow if it's installed, plain cat if not. The preview is markdown either
    way; glow only makes it pretty."""
    if shutil.which("glow"):
        # glow drops ANSI styling when stdout isn't a TTY (which it isn't, in an
        # fzf preview or a pipe into less). CLICOLOR_FORCE=1 keeps the colour.
        return f"CLICOLOR_FORCE=1 glow -s dark -w {width} -"
    return "cat"


def pick_one(conn, filters: Filters) -> str | None:
    """Choose a single rec and return its id (None if the user quits).

    The narrow cousin of ``run_picker``: same rows, same preview, no actions.
    It exists so ``undrudge copy`` never makes you read an id off one terminal
    and retype it into another — the thing you're looking at is the thing you
    get.
    """
    if not shutil.which("fzf"):
        return None
    rows = fetch_rows(conn, filters)
    if not rows:
        return None

    fd, flag = tempfile.mkstemp(prefix="undrudge-pick-")
    os.close(fd)
    Path(flag).write_text("rec", encoding="utf-8")
    preview = (
        f"{_self_cmd()} __browse-preview --id {{1}} --flag {shlex.quote(flag)} "
        f"| {_renderer('${FZF_PREVIEW_COLUMNS:-90}')}"
    )
    fzf = [
        "fzf",
        "--ansi",
        "--delimiter", "\t",
        "--with-nth", "2",
        "--no-sort",
        "--layout", "reverse",
        "--border",
        "--prompt", "copy> ",
        "--header", "pick a recommendation  ·  Enter copy · Esc cancel",
        "--preview", preview,
        "--preview-window", "right,60%,wrap,border-left",
        "--bind", "?:toggle-preview",
        "--bind", "shift-up:preview-up",
        "--bind", "shift-down:preview-down",
        "--bind", "home:first,alt-g:first",
        "--bind", "end:last,alt-G:last",
        "--bind", "ctrl-f:page-down,ctrl-b:page-up",
    ]
    try:
        result = subprocess.run(
            fzf, input=render_rows(rows), text=True, capture_output=True
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(flag)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.rstrip("\n").split("\t", 1)[0]


def run_picker(conn, cfg: config_mod.Config, filters: Filters) -> int:
    """Launch fzf over the recommendations. Returns a process exit code."""
    if not shutil.which("fzf"):
        print(
            "undrudge browse: fzf not found on PATH.\n"
            "  install it (brew install fzf / apt install fzf), or use "
            "`undrudge list` + `undrudge show <id>`.",
            file=sys.stderr,
        )
        return 127

    rows = fetch_rows(conn, filters)
    if not rows:
        print("(no recommendations)")
        return 0
    open_count = sum(1 for r in rows if r["status"] in OPEN_STATUSES)

    # Per-run flag file holding the preview mode ("rec" | "trail"), flipped by ^T.
    fd, flag = tempfile.mkstemp(prefix="undrudge-browse-")
    os.close(fd)
    Path(flag).write_text("rec", encoding="utf-8")
    flag_q = shlex.quote(flag)

    s = _self_cmd()
    filt = _filter_args(filters)
    list_cmd = f"{s} __browse-list {filt}"
    preview_src = f"{s} __browse-preview --id {{1}} --flag {flag_q}"
    preview_cmd = f"{preview_src} | {_renderer('${FZF_PREVIEW_COLUMNS:-90}')}"
    # less -R doesn't grab the mouse, so the full rec stays selectable and
    # searchable — unlike the preview pane or a TUI pager.
    open_cmd = f"{preview_src} | {_renderer('$(tput cols)')} | less -R"
    help_cmd = f"{s} __browse-help | less -R"
    def act(action: str) -> str:
        # {+f}: fzf writes the Tab-selected rows (or just the highlighted one)
        # to a temp file and substitutes its path.
        return f"{s} __browse-act --action {action} --rows {{+f}}"

    def copy(what: str) -> str:
        return f"{s} __browse-copy --id {{1}} --what {what}"

    header = (
        f"undrudge — {len(rows)} rec(s), {open_count} open  ·  ^H help · "
        f"Tab select · ⏎ open · ^A implement · ^D dismiss · ^X reject · "
        f"^L reopen · ^T trail · Esc quit"
    )

    fzf = [
        "fzf",
        "--ansi",
        "--multi",
        "--delimiter", "\t",
        "--with-nth", "2",
        "--no-sort",
        "--layout", "reverse",
        "--border",
        "--prompt", "rec> ",
        "--header", header,
        "--preview", preview_cmd,
        "--preview-window", "right,60%,wrap,border-left",
        "--bind", "?:toggle-preview",
        "--bind", f"ctrl-h:execute({help_cmd})",
        "--bind", f"enter:execute({open_cmd})",
        "--bind", f"ctrl-o:execute({open_cmd})",
        # Reason-prompting verbs need the terminal, so plain execute(); the
        # instant ones stay silent. All of them reload so the row's status
        # glyph updates under the cursor.
        "--bind", f"ctrl-d:execute({act('dismiss')})+reload({list_cmd})+clear-selection+refresh-preview+bell",
        "--bind", f"alt-d:execute({act('dismiss')})+reload({list_cmd})+clear-selection+refresh-preview+bell",
        "--bind", f"ctrl-x:execute({act('reject')})+reload({list_cmd})+clear-selection+refresh-preview+bell",
        "--bind", f"alt-x:execute({act('reject')})+reload({list_cmd})+clear-selection+refresh-preview+bell",
        "--bind", f"ctrl-a:execute-silent({act('implement')})+reload({list_cmd})+clear-selection+refresh-preview+bell",
        "--bind", f"alt-a:execute-silent({act('implement')})+reload({list_cmd})+clear-selection+refresh-preview+bell",
        "--bind", f"ctrl-l:execute-silent({act('reopen')})+reload({list_cmd})+clear-selection+refresh-preview+bell",
        "--bind", f"alt-l:execute-silent({act('reopen')})+reload({list_cmd})+clear-selection+refresh-preview+bell",
        "--bind", f"ctrl-y:execute-silent({copy('handoff')})+bell",
        "--bind", f"ctrl-p:execute-silent({copy('path')})+bell",
        "--bind", f"ctrl-t:execute-silent({s} __browse-flip --flag {flag_q})+refresh-preview",
        "--bind", f"ctrl-r:reload({list_cmd})",
        "--bind", "shift-up:preview-up",
        "--bind", "shift-down:preview-down",
        # MacBooks have no Home/End keys, so every jump has a chord alias.
        "--bind", "home:first,alt-g:first",
        "--bind", "end:last,alt-G:last",
        "--bind", "ctrl-f:page-down,ctrl-b:page-up",
    ]

    try:
        subprocess.run(fzf, input=render_rows(rows), text=True)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(flag)
    return 0
