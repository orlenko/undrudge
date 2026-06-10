"""Dispatch: the deterministic courier for logged recommendations.

A port of the standalone ``undrudge-dispatch`` prototype into undrudge
proper, exposed as ``undrudge dispatch run [--dry-run] | status``. It
selects ``logged`` recs from the store (read-only), gates and routes
them, writes self-contained briefs into ``<clone>/.undrudge-inbox/``,
and reconciles session verdicts + draft-PR outcomes back into rec
statuses. **Zero LLM calls** — interactive Claude sessions (the global
``/undrudge-check`` command) do the thinking; this only moves paper.

Status flips go through ``recommend.set_status`` only, so the sqlite +
frontmatter + events.jsonl stay coherent (invariant #1). The prototype
shelled out to the ``undrudge`` CLI for that; in-tree we call the
function directly.

This module is split so the deterministic decision logic (routing,
gating, approval-queue grammar, dismissal similarity) is pure and unit
testable, independent of any filesystem or git I/O.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import recommend

# Verdict dispositions a session can record (pull-based, invariant #2).
DISMISS_CLASS = ("dismiss-stale", "reject-as-framed", "dismiss")
NEEDS_HUMAN = ("needs-human", "convert-to-task")


# --------------------------------------------------------------------------
# Config model
# --------------------------------------------------------------------------


@dataclass
class Route:
    """A dispatch destination.

    A git route without an explicit ``dir`` becomes a *managed clone* at
    ``<clones_dir>/<name>`` (addendum 1): triage must never collide with
    a human's working checkout. Explicit ``dir`` is the exception — a
    pinned existing clone, or an in-place-only non-git target.
    """

    name: str
    dir: str | None = None
    clone_url: str | None = None
    branch: str = "main"
    sync: list[str] | None = None
    cwd_prefixes: list[str] = field(default_factory=list)
    origins: list[str] = field(default_factory=list)
    auto: bool = False
    stage: str | None = None
    # Set during resolution when dir was derived from clones_dir.
    managed_clone: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Route:
        return cls(
            name=d["name"],
            dir=d.get("dir"),
            clone_url=d.get("clone_url"),
            branch=d.get("branch", "main"),
            sync=d.get("sync"),
            cwd_prefixes=list(d.get("cwd_prefixes", [])),
            origins=list(d.get("origins", [])),
            auto=bool(d.get("auto", False)),
            stage=d.get("stage"),
        )


@dataclass
class DispatchConfig:
    routes: list[Route]
    clones_dir: str = ""
    # Gating (invariant #5): calibrated from the rec corpus.
    gate_confidence: tuple[str, ...] = ("high", "medium")
    gate_forms: tuple[str, ...] = ("extend_existing", "script", "slash_command")
    deny_patterns: list[str] = field(default_factory=list)
    apply_denylist: bool = False  # shadow mode until flipped on
    similarity_threshold: float = recommend.SIGNATURE_SIMILARITY_THRESHOLD
    similarity_min_sig_len: int = 12
    max_dispatch_per_run: int = 6

    def resolve_managed_clones(self) -> None:
        """Give every dir-less git route a managed clone dir + default sync.

        Mirrors the prototype's ``Dispatcher.__init__``: a route without a
        ``dir`` is materialized under ``clones_dir``; never mkdir the repo
        path manually — only ``git clone`` creates it (handled in the I/O
        layer). Idempotent.
        """
        for route in self.routes:
            if not route.dir:
                route.dir = f"{self.clones_dir.rstrip('/')}/{route.name}"
                route.managed_clone = True
                if route.sync is None:
                    route.sync = ["fetch", f"switch:{route.branch}", "pull"]

    def route_by_name(self, name: str | None) -> Route | None:
        if not name:
            return None
        for route in self.routes:
            if route.name == name:
                return route
        return None


# --------------------------------------------------------------------------
# Pure helpers (faithful ports of the prototype's free functions)
# --------------------------------------------------------------------------


def normalize_origin(url: str | None) -> str:
    """Canonicalize a git remote URL to ``host/owner/repo`` for matching."""
    u = (url or "").strip().lower()
    u = re.sub(r"^(https?://|ssh://)", "", u)
    if u.startswith("git@"):
        u = u[4:].replace(":", "/", 1)
    u = re.sub(r"^[^/@]+@", "", u)
    u = re.sub(r"\.git$", "", u)
    u = re.sub(r"/+", "/", u)
    return u


def path_under(cwd: str, prefix: str) -> bool:
    return cwd == prefix or cwd.startswith(prefix.rstrip("/") + "/")


# --------------------------------------------------------------------------
# Routing (invariant #4 precedence)
# --------------------------------------------------------------------------


def title_route_hint(title: str, routes: list[Route]) -> Route | None:
    """A route whose name appears verbatim (word-boundary) in the rec
    title is a strong signal — undrudge evidence cwds often point at where
    a command *ran*, not what it was *about* (e.g. volpe-lite work done
    from an ops clone). Returns the unique hit, or None if 0 or >1.
    """
    hits = []
    for route in routes:
        name = route.name
        if name in ("vault", "undrudge") or len(name) < 3:
            continue  # too generic in this corpus
        if re.search(rf"\b{re.escape(name)}\b", title or "", re.I):
            hits.append(route)
    return hits[0] if len(hits) == 1 else None


@dataclass
class RouteDecision:
    route: Route | None
    note: str | None = None  # e.g. "routed by title over evidence cwd"


def route_for(
    fm: dict[str, Any],
    title: str,
    cwds: list[str],
    cfg: DispatchConfig,
    *,
    origin_of: Callable[[str], str | None] | None = None,
    isdir: Callable[[str], bool] | None = None,
) -> RouteDecision:
    """Resolve a rec to a route by the invariant-#4 precedence:

    1. ``target_scope == agent_global`` → the ``vault`` route.
    2. A route name appearing in the rec TITLE overrides evidence-cwd
       resolution.
    3. cwd-prefix match (first evidence cwd under a route's prefixes).
    4. normalized git origin of the first evidence cwd that is a dir.

    ``origin_of(cwd) -> str|None`` and ``isdir(cwd) -> bool`` are injected
    so this stays pure/testable; the I/O layer passes real git/fs calls.
    """
    if fm.get("target_scope") == "agent_global":
        return RouteDecision(cfg.route_by_name("vault"))

    hint = title_route_hint(title, cfg.routes)

    cwd_route: Route | None = None
    for cwd in cwds:
        for route in cfg.routes:
            if any(path_under(cwd, p) for p in route.cwd_prefixes):
                cwd_route = route
                break
        if cwd_route:
            break

    if cwd_route is None and origin_of is not None:
        for cwd in cwds:
            if isdir is not None and not isdir(cwd):
                continue
            origin = normalize_origin(origin_of(cwd))
            if origin:
                for route in cfg.routes:
                    if origin in route.origins:
                        cwd_route = route
                        break
            if cwd_route:
                break

    if hint and cwd_route and hint.name != cwd_route.name:
        return RouteDecision(
            hint,
            note=f"title names `{hint.name}`, evidence cwds suggested "
                 f"`{cwd_route.name}` — routed by title",
        )
    return RouteDecision(cwd_route or hint)


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------


def deny_hit(title: str, signature: str, body: str, patterns: list[str]) -> str | None:
    """Return the first deny pattern matching the rec's text, or None."""
    hay = " ".join([title or "", signature or "", body or ""])
    for pat in patterns:
        if re.search(pat, hay, re.I):
            return pat
    return None


def similar_dismissed(
    signature: str,
    dismissed: list[tuple[str, str, str]],
    cfg: DispatchConfig,
) -> tuple[str, str] | None:
    """If the rec's signature is >= threshold similar to a dismissed rec's,
    return ``(dismissed_id12, dismissed_title)``.

    ``dismissed`` is a list of ``(id12, title, signature)``. Reuses
    undrudge's own ``signature_similarity`` so the dispatcher's
    "looks like something already said no to" judgment matches the
    analyzer's write-time near-dup gate.
    """
    canon = recommend.canonicalize_signature(signature)
    if len(canon) < cfg.similarity_min_sig_len:
        return None
    for d_id, d_title, d_sig in dismissed:
        if len(recommend.canonicalize_signature(d_sig)) < cfg.similarity_min_sig_len:
            continue
        if recommend.signature_similarity(signature, d_sig) >= cfg.similarity_threshold:
            return (d_id, d_title)
    return None


# --------------------------------------------------------------------------
# Approval-queue grammar (invariant #3)
# --------------------------------------------------------------------------

# - [x] approve <id-prefix> [-> <route>]
# - [x] dismiss <id-prefix> [— reason: ...]
_APPROVAL_RE = re.compile(
    r"^\s*-\s*\[[xX]\]\s*(approve|dismiss)\s+([0-9a-f]{6,64})"
    r"(?:\s*(?:->|route:)\s*([A-Za-z0-9_-]+))?"
    r"(?:\s*[—-]+\s*reason:\s*(.*))?\s*$"
)
_CHECKED_RE = re.compile(r"^\s*-\s*\[[xX]\]")


@dataclass
class Approval:
    action: str            # "approve" | "dismiss"
    route: str | None      # explicit route for unrouted approvals
    reason: str            # free text (dismiss)


def parse_approvals(text: str) -> tuple[dict[str, Approval], list[str]]:
    """Parse checked boxes from a dispatch-queue.md body.

    Returns ``({id_prefix: Approval}, [malformed_lines])``. A checked box
    that doesn't match the grammar is surfaced as malformed rather than
    silently dropped (invariant #6 — nothing fails silently).
    """
    actions: dict[str, Approval] = {}
    malformed: list[str] = []
    for line in text.splitlines():
        if not _CHECKED_RE.match(line):
            continue
        m = _APPROVAL_RE.match(line)
        if m:
            actions[m.group(2)] = Approval(
                action=m.group(1),
                route=m.group(3),
                reason=(m.group(4) or "").strip(),
            )
        else:
            malformed.append(line.strip())
    return actions, malformed


def resolve_approvals(
    raw: dict[str, Approval], rec_ids: list[str]
) -> tuple[dict[str, Approval], list[str]]:
    """Prefix-match approval keys against logged rec ids (CLI semantics).

    Returns ``({id12: Approval}, [failures])``. An approval that resolves
    to 0 or >1 recs becomes a failure line, never a silent no-op
    (invariant #3 — an approval that produces no dispatch must surface).
    """
    resolved: dict[str, Approval] = {}
    failures: list[str] = []
    for key, approval in raw.items():
        matches = [rid for rid in rec_ids if rid.startswith(key)]
        if len(matches) == 1:
            resolved[matches[0][:12]] = approval
        elif len(matches) > 1:
            failures.append(f"approval `{key}` is ambiguous ({len(matches)} matches)")
        else:
            failures.append(
                f"approval `{key}` matches no logged rec (already closed?)"
            )
    return resolved, failures
