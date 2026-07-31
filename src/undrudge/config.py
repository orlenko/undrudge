"""Config loading. Single source of truth for paths.

Defaults follow XDG: config in ``~/.config/undrudge``, data in
``~/.local/share/undrudge``. Override with the ``UNDRUDGE_CONFIG`` env var
(useful for tests).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _expand(p: str | os.PathLike[str]) -> Path:
    return Path(os.path.expanduser(str(p))).resolve()


def default_config_path() -> Path:
    override = os.environ.get("UNDRUDGE_CONFIG")
    if override:
        return _expand(override)
    xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return _expand(f"{xdg}/undrudge/config.toml")


def default_data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return _expand(f"{xdg}/undrudge")


@dataclass
class Paths:
    db: Path
    recs_dir: Path
    digests_dir: Path
    logs_dir: Path
    events_log: Path  # append-only JSONL audit trail


@dataclass
class Claude:
    projects_root: Path


@dataclass
class Codex:
    home: Path

    @property
    def session_roots(self) -> tuple[Path, Path]:
        return (self.home / "sessions", self.home / "archived_sessions")


@dataclass
class Atuin:
    db: Path


@dataclass
class Llm:
    command: str = "@bundled"  # see undrudge.llm.resolve_command
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 8000
    # Headless claude with a chunky digest prompt routinely needs minutes,
    # not seconds. The wrapper polls a marker file, so a generous ceiling
    # costs nothing on fast runs.
    timeout_seconds: int = 900


@dataclass
class Output:
    on_write: str = ""


@dataclass
class Privacy:
    fail_loud: bool = True


@dataclass
class Retention:
    """How long ingested rows stay in the query cache.

    The DB is rebuildable and every consumer reads a 24h/7d trailing window,
    so old messages and commands are pure disk cost. ``days = 0`` keeps
    everything. Pruning runs at the end of each ``gather``; recommendations
    are never pruned.
    """

    days: int = 30


@dataclass
class Config:
    paths: Paths
    claude: Claude
    atuin: Atuin
    # Programmatic/test configs opt in explicitly. default_config() enables
    # the user's real Codex home; this default keeps isolated tests from ever
    # wandering into it by accident.
    codex: Codex | None = None
    llm: Llm = field(default_factory=Llm)
    output: Output = field(default_factory=Output)
    privacy: Privacy = field(default_factory=Privacy)
    retention: Retention = field(default_factory=Retention)


def default_config() -> Config:
    data = default_data_dir()
    codex_home = _expand(os.environ.get("CODEX_HOME") or "~/.codex")
    return Config(
        paths=Paths(
            db=data / "undrudge.sqlite",
            recs_dir=data / "recommendations",
            digests_dir=data / "digests",
            logs_dir=data / "logs",
            events_log=data / "events.jsonl",
        ),
        claude=Claude(projects_root=_expand("~/.claude/projects")),
        atuin=Atuin(db=_expand("~/.local/share/atuin/history.db")),
        codex=Codex(home=codex_home),
    )


def load(path: Path | None = None) -> Config:
    """Read ``config.toml`` if present; fall back to defaults for missing fields."""
    cfg = default_config()
    target = path or default_config_path()
    if not target.exists():
        return cfg

    raw = tomllib.loads(target.read_text())

    if "paths" in raw:
        p = raw["paths"]
        cfg.paths = Paths(
            db=_expand(p.get("db", cfg.paths.db)),
            recs_dir=_expand(p.get("recs_dir", cfg.paths.recs_dir)),
            digests_dir=_expand(p.get("digests_dir", cfg.paths.digests_dir)),
            logs_dir=_expand(p.get("logs_dir", cfg.paths.logs_dir)),
            events_log=_expand(p.get("events_log", cfg.paths.events_log)),
        )
    if "claude" in raw:
        c = raw["claude"]
        cfg.claude = Claude(projects_root=_expand(c.get("projects_root", cfg.claude.projects_root)))
    if "codex" in raw:
        c = raw["codex"]
        current_home = cfg.codex.home if cfg.codex else _expand("~/.codex")
        cfg.codex = Codex(home=_expand(c.get("home", current_home)))
    if "atuin" in raw:
        a = raw["atuin"]
        cfg.atuin = Atuin(db=_expand(a.get("db", cfg.atuin.db)))
    if "llm" in raw:
        ll = raw["llm"]
        cfg.llm = Llm(
            command=ll.get("command", cfg.llm.command),
            model=ll.get("model", cfg.llm.model),
            max_tokens=int(ll.get("max_tokens", cfg.llm.max_tokens)),
            timeout_seconds=int(ll.get("timeout_seconds", cfg.llm.timeout_seconds)),
        )
    if "output" in raw:
        o = raw["output"]
        cfg.output = Output(on_write=str(o.get("on_write", "")))
    if "privacy" in raw:
        pr = raw["privacy"]
        cfg.privacy = Privacy(fail_loud=bool(pr.get("fail_loud", True)))
    if "retention" in raw:
        rt = raw["retention"]
        cfg.retention = Retention(days=max(int(rt.get("days", cfg.retention.days)), 0))
    return cfg


def render_default_config_toml() -> str:
    """Render a default config using the current runtime defaults.

    Honors ``XDG_*`` env vars at call time so test harnesses can redirect
    paths cleanly.
    """
    cfg = default_config()
    return f"""\
# undrudge config — generated by `undrudge init`. Edit freely.

[paths]
db          = "{cfg.paths.db}"
recs_dir    = "{cfg.paths.recs_dir}"
digests_dir = "{cfg.paths.digests_dir}"
logs_dir    = "{cfg.paths.logs_dir}"
# Append-only JSONL audit trail. One line per event: rec_written,
# rec_<status> on a status flip (rec_dismissed / rec_implemented /
# rec_dispatched / rec_rejected / rec_logged, carrying an optional
# reason), analyze_complete, and gather_failed.
events_log  = "{cfg.paths.events_log}"

[claude]
projects_root = "{cfg.claude.projects_root}"

[codex]
# Both active sessions/ and archived_sessions/ are scanned below this home.
home = "{cfg.codex.home if cfg.codex else _expand('~/.codex')}"

[atuin]
db = "{cfg.atuin.db}"

[llm]
# `@bundled` (default) uses the claude-sandboxed.sh wrapper shipped with
# undrudge — runs claude under nono if available, otherwise bare claude.
# Override with an absolute path to your own wrapper, or with `claude` to
# disable any wrapping.
command         = "{cfg.llm.command}"
model           = "{cfg.llm.model}"
max_tokens      = {cfg.llm.max_tokens}
timeout_seconds = {cfg.llm.timeout_seconds}

[output]
# Optional shell command run after each recommendation is written.
# Executed under /bin/sh -c, with the absolute path of the new .md
# file bound to $1 (and to "$@"). Examples:
#   on_write = 'cp "$1" ~/notes/undrudge/'
#   on_write = '~/bin/notify-undrudge "$1"'
on_write = ""

[privacy]
# When true (default) any sanitization failure drops the row and logs to
# redaction_failures. Never store unredacted text.
fail_loud = true

[retention]
# Drop ingested messages and shell commands older than this many days at the
# end of each `gather`. The DB is a rebuildable cache over your Claude/Codex
# JSONL and atuin history, and analysis only ever reads a 24h/7d window — old
# rows cost disk and buy nothing. Recommendations are never pruned.
# 0 = keep everything. Run `undrudge prune --dry-run` to see what a change
# would remove, and `undrudge prune --vacuum` to hand the freed pages back to
# the filesystem.
days = {cfg.retention.days}
"""
