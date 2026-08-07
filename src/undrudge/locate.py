"""Locate: which open recommendations belong to the repo you're in.

The pull counterpart to ``dispatch``. Dispatch pushes briefs into
clones and opens a session there, which can collide with a session
already working in that directory. ``undrudge here`` answers the
inverse question from *inside* that session — "what does undrudge have
for this repo?" — and leaves the doing to the caller. Zero LLM calls,
no route table, no config: the only inputs are the rec store and the
git repo you're standing in.

Matching runs off evidence, not prose. ``recommend.evidence_cwds``
resolves a rec's ``evidence_refs`` to the directories its shell
commands and agent sessions were observed in — captured at ingest and
authoritative, unlike the repo/branch labels a digest renders
best-effort at render time. Three tiers:

``this_clone``  an evidence cwd is inside this working tree. Path
                prefix, so it still matches when the directory is long
                gone (a deleted worktree, a `/tmp` run).
``same_repo``   an evidence cwd is a different checkout with the same
                remote origin. ops, ops2 … ops6 are one repo in six
                clones; the toplevel basename would call them six
                repos.
``named``       no evidence points here, but the rec names this repo in
                its title. See ``names_repo`` — this is the tool-fix
                case, and it is a hint, not proof.

Only ``single_repo`` recs are considered by default. ``cross_cutting``
and ``agent_global`` ones span directories by definition — their
evidence would match several repos weakly and none of them well — so
they stay with ``undrudge browse`` + ``undrudge copy`` and a human
deciding where they land.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import recommend

logger = logging.getLogger(__name__)

# Best-effort git lookups: a dead cwd or a slow network remote must
# never hang the query. Generous next to the digest's 2s budget because
# a timeout here degrades a *gate*, not a label — see repo_at.
_GIT_TIMEOUT_S = 10.0

DEFAULT_SCOPES = ("single_repo",)
TIER_ORDER = {"this_clone": 0, "same_repo": 1, "named": 2}

# The `named` tier exists because cwd records where the *pain* was felt,
# which is not always where the *fix* goes. "undrudge show should print
# the rec body" was observed in homebrew-tap and thelocalring.com — the
# fix belongs in the undrudge repo, which appears in no evidence cwd at
# all. It ranks below both cwd tiers and is explicitly a hint: the
# caller verifies the rec applies before acting on it.
#
# Two guards, both learned from this corpus. A length floor, because
# short names match English words by accident; and a stoplist of
# directory names so generic that naming one says nothing about which
# repo a rec targets. `dispatch.route_by_title` stops on the same
# cliff, and additionally excludes `undrudge` — it matches a title
# against *every* configured route, where a passing mention misroutes.
# Here the question is narrower ("does this rec name the repo I am
# standing in?"), so a tool's own name is the signal rather than noise.
_MIN_NAME_MATCH_LEN = 4
_GENERIC_REPO_NAMES = frozenset({
    "code", "config", "data", "docs", "dotfiles", "home", "lib", "main",
    "notes", "personal", "project", "projects", "repo", "repos", "sandbox",
    "scratch", "scripts", "site", "source", "src", "temp", "test", "tests",
    "tmp", "tools", "vault", "web", "work", "workspace",
})

# Markers git leaves for an operation stopped midway. A worktree can be
# spotless and still be mid-rebase, and branching out from under one
# strands resolved conflicts — so these count as dirty.
_IN_PROGRESS_MARKERS = (
    "rebase-merge", "rebase-apply", "MERGE_HEAD",
    "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG",
)

# scp-style (git@host:owner/repo), URL-style, optional port, optional .git.
_ORIGIN_RE = re.compile(
    r"^(?:(?:https?|ssh|git|file)://)?(?:[^@/]+@)?"
    r"([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$"
)


@dataclass(frozen=True)
class RepoIdent:
    """The repo a query is anchored to."""

    root: str
    origin: str | None = None
    dirty: bool = True
    dirty_reason: str = ""

    @property
    def name(self) -> str:
        return Path(self.root).name


@dataclass
class Candidate:
    """An open rec that belongs to the anchor repo."""

    id: str
    title: str
    body_path: str
    created_at: int
    tier: str
    cwds: list[str] = field(default_factory=list)

    @property
    def id12(self) -> str:
        return self.id[:12]


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def normalize_origin(url: str | None) -> str | None:
    """``git@github.com:o/r.git`` and ``https://github.com/o/r`` alike
    become ``github.com/o/r``, so clones compare equal regardless of the
    protocol — or the SSH port — each was cloned over."""
    if not url or not url.strip():
        return None
    u = url.strip()
    m = _ORIGIN_RE.match(u)
    if not m:
        # Local-path or otherwise unparseable remote: compare it whole
        # rather than giving up.
        return u.lower().removesuffix(".git").rstrip("/") or None
    host, path = m.group(1), m.group(2)
    return f"{host.lower()}/{path.strip('/')}"


def match_tier(
    cwds: Iterable[str],
    repo: RepoIdent,
    *,
    origin_of: Callable[[str], str | None],
) -> tuple[str | None, list[str]]:
    """Classify a rec's evidence cwds against ``repo``.

    Returns the strongest tier and the cwds that earned it (this-clone
    hits first). ``(None, [])`` when nothing matches.
    """
    root = repo.root.rstrip("/")
    if not root:
        # A repo rooted at "/" would claim every absolute path.
        return (None, [])
    here: list[str] = []
    same: list[str] = []
    for cwd in cwds:
        if not cwd:
            continue
        c = cwd.rstrip("/")
        if c == root or c.startswith(root + "/"):
            here.append(cwd)
        elif repo.origin and origin_of(cwd) == repo.origin:
            same.append(cwd)
    if here:
        return ("this_clone", here + same)
    if same:
        return ("same_repo", same)
    return (None, [])


def names_repo(text: str, repo: RepoIdent) -> bool:
    """True when ``text`` names this repo as a whole word.

    Matches the toplevel basename and, when the origin parses, the
    ``owner/repo`` slug's repo half — the same string for most clones,
    different when a checkout was renamed locally. Names that are too
    short or too generic never match; see ``_GENERIC_REPO_NAMES``.
    """
    if not text:
        return False
    names = {repo.name}
    if repo.origin and "/" in repo.origin:
        names.add(repo.origin.rsplit("/", 1)[-1])
    haystack = text.lower()
    for raw in names:
        n = (raw or "").strip().lower()
        if len(n) < _MIN_NAME_MATCH_LEN or n in _GENERIC_REPO_NAMES:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", haystack):
            return True
    return False


def rank(cands: list[Candidate]) -> list[Candidate]:
    """This clone before sibling clones before a bare name match, then
    weight of evidence, then newest."""
    return sorted(
        cands,
        key=lambda c: (
            TIER_ORDER.get(c.tier, 9),
            -len(c.cwds),
            -c.created_at,
        ),
    )


# --------------------------------------------------------------------------
# Git I/O
# --------------------------------------------------------------------------


def _git(args: list[str]) -> tuple[bool, str]:
    """Run git. Returns ``(ok, stdout)``.

    ``ok`` distinguishes "git said nothing" from "git never answered" —
    the difference between a clean tree and an unknown one.
    """
    try:
        r = subprocess.run(
            ["git", *args],
            capture_output=True, text=True,
            timeout=_GIT_TIMEOUT_S, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.debug("git %s failed: %s", " ".join(args[:3]), e)
        return (False, "")
    if r.returncode != 0:
        return (False, "")
    return (True, r.stdout.strip())


def _git_out(args: list[str]) -> str | None:
    ok, out = _git(args)
    return out if (ok and out) else None


def _worktree_state(root: str) -> str:
    """Non-empty when the tree is unsafe to branch from.

    Fails *closed*: if git can't be asked — a timeout on a huge repo, a
    corrupt index — the answer is "dirty", never "clean". This gates
    whether an agent starts committing on top of someone's work, so an
    unknown must never read as permission.
    """
    ok, out = _git(
        ["-C", root, "status", "--porcelain", "--untracked-files=no"]
    )
    if not ok:
        return "git status unavailable (timeout or error)"
    if out:
        n = len(out.splitlines())
        return f"{n} modified tracked file(s)"
    git_dir_ok, git_dir = _git(["-C", root, "rev-parse", "--git-dir"])
    if not git_dir_ok:
        return "git dir unavailable"
    base = Path(git_dir) if Path(git_dir).is_absolute() else Path(root) / git_dir
    for marker in _IN_PROGRESS_MARKERS:
        if (base / marker).exists():
            return f"operation in progress ({marker})"
    return ""


def repo_at(path: str | Path) -> RepoIdent | None:
    """Identify the repo containing ``path``; None if it isn't one."""
    p = str(path)
    root = _git_out(["-C", p, "rev-parse", "--show-toplevel"])
    if not root:
        return None
    origin = normalize_origin(
        _git_out(["-C", p, "remote", "get-url", "origin"])
    )
    state = _worktree_state(root)
    return RepoIdent(
        root=root, origin=origin,
        dirty=bool(state), dirty_reason=state,
    )


def origin_resolver() -> Callable[[str], str | None]:
    """A memoized cwd → normalized-origin lookup.

    Walks up to the nearest surviving ancestor, because the evidence
    that most needs resolving comes from directories that no longer
    exist — a `.qc/dev-worktrees/<name>` deleted after its branch
    merged still identifies its repo through the clone above it.
    """
    cache: dict[str, str | None] = {}

    def resolve(cwd: str) -> str | None:
        if cwd in cache:
            return cache[cwd]
        origin: str | None = None
        probe = Path(cwd) if cwd else None
        seen: list[str] = []
        # Bounded walk: a handful of levels covers worktrees and
        # subdirectories without marching to /.
        for _ in range(6):
            if probe is None or str(probe) in ("/", "", probe.anchor):
                break
            key = str(probe)
            if key in cache:
                origin = cache[key]
                break
            if probe.is_dir():
                origin = normalize_origin(
                    _git_out(["-C", key, "remote", "get-url", "origin"])
                )
                seen.append(key)
                break
            seen.append(key)
            probe = probe.parent
        for key in seen:
            cache[key] = origin
        cache[cwd] = origin
        return origin

    return resolve


# --------------------------------------------------------------------------
# Store query
# --------------------------------------------------------------------------


@dataclass
class HereResult:
    repo: RepoIdent
    candidates: list[Candidate]
    matched: int = 0
    considered: int = 0
    unreadable: int = 0


def here(
    conn: sqlite3.Connection,
    repo: RepoIdent,
    *,
    origin_of: Callable[[str], str | None] | None = None,
    scopes: Iterable[str] = DEFAULT_SCOPES,
    limit: int = 5,
    status: str = "logged",
) -> HereResult:
    """Open recs belonging to ``repo``, strongest match first."""
    origin_of = origin_of or origin_resolver()
    scope_set = set(scopes)
    rows = conn.execute(
        "SELECT id, title, body_path, created_at FROM recommendations "
        "WHERE status = ? ORDER BY created_at DESC",
        (status,),
    ).fetchall()

    cands: list[Candidate] = []
    unreadable = 0
    considered = 0
    for rec_id, title, body_path, created_at in rows:
        fm, _body = recommend.parse_rec_file(body_path)
        if not isinstance(fm, dict) or not fm:
            # parse_rec_file degrades to an empty dict for a missing,
            # fenceless, or malformed file (and a hand-edited fence can
            # parse to a list). Without frontmatter there are no
            # evidence refs and no scope, so the rec can't be placed —
            # count it, so a caller reporting "nothing for this repo"
            # can say whether it actually looked at everything.
            unreadable += 1
            continue
        # `target_scope` postdates the earliest recs. A rec that predates
        # it is judged on its evidence rather than dropped silently —
        # the alternative undercounts without telling anyone.
        scope = fm.get("target_scope")
        if scope and scope_set and scope not in scope_set:
            continue
        considered += 1
        cwds = recommend.evidence_cwds(conn, fm)
        tier, matched = match_tier(cwds, repo, origin_of=origin_of)
        if tier is None:
            # No cwd evidence points here, but the rec may still target
            # this repo — a tool's own fixes are felt wherever the tool
            # is used, never in its source tree. Deliberately not
            # conditioned on the evidence pointing *nowhere*: those recs
            # do have cwds, just in the repos where the tool was run.
            if not names_repo(str(title), repo):
                continue
            tier, matched = "named", []
        cands.append(Candidate(
            id=str(rec_id), title=str(title), body_path=str(body_path),
            created_at=int(created_at or 0), tier=tier, cwds=matched,
        ))

    ranked = rank(cands)
    logger.info(
        "here: repo=%s origin=%s considered=%d matched=%d",
        repo.name, repo.origin, considered, len(ranked),
    )
    return HereResult(
        repo=repo,
        candidates=ranked[:limit] if limit and limit > 0 else ranked,
        matched=len(ranked),
        considered=considered,
        unreadable=unreadable,
    )
