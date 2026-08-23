"""Recommendation persistence: markdown out, DB row, optional hook.

A "recommendation" is the unit of useful output from this whole system.
We write one markdown file per recommendation under
``<recs_dir>/<YYYY-MM-DD>/<NN>-<slug>.md`` with frontmatter capturing the
fingerprint, scope, status, evidence, and signature. Idempotent on
re-write — fingerprint is the primary key.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import store

# When two signatures' SequenceMatcher ratio meets this threshold we treat
# the new recommendation as a near-duplicate of the existing one. 0.85 is
# tight enough to keep "wrap repeated find/grep" and "wrap repeated cd"
# distinct, loose enough to fold rephrasings of the same pattern.
SIGNATURE_SIMILARITY_THRESHOLD = 0.85


@dataclass
class Recommendation:
    title: str
    body_markdown: str
    signature: str
    automation_form: str = "other"
    confidence: str = "medium"
    rationale: str = ""
    evidence: list[Any] | None = None
    # Probe-phase: structured citations from the LLM. Each item is a dict
    # with `source` ("atuin" / "msg" / "session"), `external_id` (8+ char prefix
    # of the row id from the digest), and optional `note`. Validated
    # at write time; unresolved refs are kept as-is and counted for
    # tuning the digest format.
    evidence_refs: list[dict[str, Any]] | None = None
    # Where the proposed automation should live. Mirrors autoharness's
    # `target_class` axis but framed for undrudge's location-aware
    # analysis. "single_repo" = the rec belongs in one repo's scripts/;
    # "cross_cutting" = the rec belongs in ~/bin or a shared dotfile;
    # "agent_global" = a provider-appropriate Claude/Codex agent surface.
    # Defaults to "single_repo" when the LLM doesn't specify.
    target_scope: str = "single_repo"
    scope: str = "daily"

    def fingerprint(self) -> str:
        h = hashlib.sha256(
            f"{self.scope}::{canonicalize_signature(self.signature)}".encode()
        ).hexdigest()
        return h


_SIGNATURE_STRIP_RE = re.compile(r"[`*_'\"~]+")
_SIGNATURE_PUNCT_RE = re.compile(r"\s*([(){}|;,])\s*")
_SIGNATURE_PLACEHOLDER_RE = re.compile(r"<\s*([a-z]+)\s*>", re.IGNORECASE)


def canonicalize_signature(sig: str) -> str:
    """Aggressive normalization of an LLM-emitted signature for stable hashing.

    Goals: same *pattern* hashes the same regardless of:
      - Case ("FIND" vs "find")
      - Quoting backticks/asterisks/underscores around tokens
      - Whitespace around pipes/parens/commas
      - Placeholder casing/spacing (``<Path>`` vs ``< path >`` vs ``<PATH>``)

    Doesn't reorder tokens — order matters in commands.
    """
    s = sig.strip().lower()
    s = _SIGNATURE_STRIP_RE.sub("", s)
    s = _SIGNATURE_PLACEHOLDER_RE.sub(lambda m: f"<{m.group(1).lower()}>", s)
    s = _SIGNATURE_PUNCT_RE.sub(r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def signature_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(
        None, canonicalize_signature(a), canonicalize_signature(b)
    ).ratio()


# Active statuses block a new rec as an ordinary duplicate: the same
# pattern is already live (logged), already built (implemented), or
# already sent out as a brief (dispatched).
ACTIVE_STATUSES = ("logged", "implemented", "dispatched")
# Negative statuses mean a human (or a triage session) already said "no"
# to this pattern. A close rephrasing should be suppressed at write time
# AND surfaced to the analyze prompt as "previously dismissed".
NEGATIVE_STATUSES = ("dismissed", "rejected")


def find_similar(
    conn: sqlite3.Connection,
    *,
    scope: str,
    signature: str,
    threshold: float = SIGNATURE_SIMILARITY_THRESHOLD,
    statuses: tuple[str, ...] = ACTIVE_STATUSES,
) -> dict[str, Any] | None:
    """Return the closest existing recommendation if its signature is
    near-identical to ``signature`` under the canonical form.

    ``statuses`` selects which rows are eligible to match. Defaults to the
    active set (``logged``/``implemented``/``dispatched``) — the ordinary
    "don't write a duplicate of a live rec" gate. Pass
    ``NEGATIVE_STATUSES`` to instead find a near-match among
    dismissed/rejected recs, so the writer can suppress re-proposed
    variants of ideas a human already declined. Returns ``None`` if
    nothing crosses the threshold.
    """
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"""SELECT id, signature, body_path, status, reason
             FROM recommendations
            WHERE scope = ? AND status IN ({placeholders})""",
        (scope, *statuses),
    ).fetchall()

    best_ratio = 0.0
    best: dict[str, Any] | None = None
    canon_target = canonicalize_signature(signature)
    for row in rows:
        canon_existing = canonicalize_signature(row["signature"])
        ratio = difflib.SequenceMatcher(None, canon_target, canon_existing).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = dict(row)
    if best and best_ratio >= threshold:
        best["similarity"] = best_ratio
        return best
    return None


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_LEN = 60
_VALID_FORM = {
    "slash_command", "script", "hook", "shell_alias", "extend_existing",
    # Capability-gap recs (docs/capability-gap.md). Deliberately absent
    # from dispatch's default gate_forms — release-note text feeds these,
    # so they must stay out of auto-dispatch without a separate decision.
    "adopt_capability",
    "other",
}
_VALID_CONFIDENCE = {"high", "medium", "low"}
_VALID_SCOPE = {"single_repo", "cross_cutting", "agent_global"}


def _slug(title: str) -> str:
    s = _SLUG_RE.sub("-", title.lower()).strip("-")
    return (s or "rec")[:_MAX_SLUG_LEN]


def _next_seq(day_dir: Path) -> int:
    if not day_dir.exists():
        return 1
    used = []
    for entry in day_dir.iterdir():
        m = re.match(r"^(\d+)-", entry.name)
        if m:
            try:
                used.append(int(m.group(1)))
            except ValueError:
                continue
    return (max(used) + 1) if used else 1


def _frontmatter(rec: Recommendation, *, fingerprint: str, created: datetime) -> str:
    payload = {
        "id": fingerprint,
        "scope": rec.scope,
        "target_scope": rec.target_scope,
        "status": "logged",
        "created": created.isoformat(timespec="seconds"),
        "confidence": rec.confidence,
        "automation_form": rec.automation_form,
        "signature": rec.signature,
        "evidence": rec.evidence or [],
    }
    if rec.evidence_refs:
        payload["evidence_refs"] = rec.evidence_refs
    if rec.rationale:
        payload["rationale"] = rec.rationale
    body = json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    # Fence as a ```json code block rather than YAML-style ---. The
    # payload is JSON, and bare --- gets rendered by some markdown
    # viewers as an H2 underline applied to the line above.
    return f"```json\n{body}\n```\n"


def render_markdown(rec: Recommendation, *, fingerprint: str, created: datetime) -> str:
    head = _frontmatter(rec, fingerprint=fingerprint, created=created)
    return head + f"\n# {rec.title.strip()}\n\n{rec.body_markdown.strip()}\n"


def parse_rec_file(body_path: str | Path | None) -> tuple[dict[str, Any], str]:
    """Split a rec markdown file into (frontmatter, body).

    Reads the current ```json fence and the legacy YAML-style ``---`` fence
    (recs written before that change). A missing, unreadable, or malformed
    file degrades to ``({}, whatever text was there)`` — callers render what
    they can rather than failing on a hand-edited rec.
    """
    if not body_path:
        return {}, ""
    try:
        text = Path(body_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}, ""
    return parse_rec_text(text)


def parse_rec_text(text: str) -> tuple[dict[str, Any], str]:
    """Split already-read recommendation markdown into metadata and body."""
    if text.startswith("```json\n"):
        open_len, close = len("```json\n"), "\n```"
    elif text.startswith("---\n"):
        open_len, close = len("---\n"), "\n---"
    else:
        return {}, text
    end = text.find(close, open_len)
    if end < 0:
        return {}, text
    try:
        fm = json.loads(text[open_len:end])
    except json.JSONDecodeError:
        return {}, text
    return fm, text[end + len(close):].lstrip("\n")


def normalize(rec: Recommendation) -> Recommendation:
    """Tighten free-form LLM output to known enums and trimmed strings."""
    form = rec.automation_form.strip().lower().replace(" ", "_") if rec.automation_form else "other"
    if form not in _VALID_FORM:
        form = "other"
    confidence = rec.confidence.strip().lower() if rec.confidence else "medium"
    if confidence not in _VALID_CONFIDENCE:
        confidence = "medium"
    target_scope = (
        rec.target_scope.strip().lower().replace(" ", "_")
        if rec.target_scope else "single_repo"
    )
    if target_scope not in _VALID_SCOPE:
        target_scope = "single_repo"
    return Recommendation(
        title=rec.title.strip()[:200],
        body_markdown=rec.body_markdown.strip(),
        signature=rec.signature.strip(),
        automation_form=form,
        confidence=confidence,
        rationale=rec.rationale.strip(),
        evidence=rec.evidence or [],
        evidence_refs=rec.evidence_refs,
        target_scope=target_scope,
        scope=rec.scope,
    )


@dataclass
class WriteResult:
    fingerprint: str
    path: Path | None
    inserted: bool
    skipped_reason: str | None = None


def write(
    conn: sqlite3.Connection,
    rec: Recommendation,
    *,
    recs_dir: Path,
    on_write: str | None = None,
    now: datetime | None = None,
) -> WriteResult:
    """Persist a recommendation. Idempotent on the fingerprint.

    Returns a ``WriteResult`` with the path of the newly written file (or
    ``None`` if the rec already existed) and whether a DB row was inserted.
    """
    rec = normalize(rec)
    if not rec.title or not rec.signature:
        return WriteResult(fingerprint="", path=None, inserted=False,
                           skipped_reason="empty title or signature")

    fp = rec.fingerprint()
    existing = conn.execute(
        "SELECT body_path FROM recommendations WHERE id = ?", (fp,)
    ).fetchone()
    if existing:
        return WriteResult(fingerprint=fp, path=Path(existing["body_path"]),
                           inserted=False, skipped_reason="duplicate fingerprint")

    near = find_similar(conn, scope=rec.scope, signature=rec.signature)
    if near is not None:
        return WriteResult(
            fingerprint=near["id"],
            path=Path(near["body_path"]) if near["body_path"] else None,
            inserted=False,
            skipped_reason=(
                f"near-duplicate of {near['id'][:12]} "
                f"(similarity={near['similarity']:.2f})"
            ),
        )

    # Suppress re-proposed variants of recs a human already declined.
    # A dismissed/rejected rec near-matching this signature means the LLM
    # is rephrasing an idea that's already been said "no" to — don't
    # re-surface it. The reason (if any) is carried in the skip message
    # so a -v run shows *why* it was suppressed.
    declined = find_similar(
        conn, scope=rec.scope, signature=rec.signature,
        statuses=NEGATIVE_STATUSES,
    )
    if declined is not None:
        why = f"; reason: {declined['reason']}" if declined.get("reason") else ""
        return WriteResult(
            fingerprint=declined["id"],
            path=Path(declined["body_path"]) if declined["body_path"] else None,
            inserted=False,
            skipped_reason=(
                f"near-duplicate of {declined['status']} rec "
                f"{declined['id'][:12]} (similarity={declined['similarity']:.2f})"
                f"{why}"
            ),
        )

    created = now or datetime.now(UTC)
    day_dir = recs_dir / created.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    seq = _next_seq(day_dir)
    path = day_dir / f"{seq:03d}-{_slug(rec.title)}.md"

    md = render_markdown(rec, fingerprint=fp, created=created)
    path.write_text(md)

    ts = store.now_ms()
    conn.execute(
        """INSERT INTO recommendations(
               id, scope, title, signature, body_path, evidence,
               status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'logged', ?, ?)""",
        (
            fp, rec.scope, rec.title, rec.signature, str(path),
            json.dumps(rec.evidence or [], default=str), ts, ts,
        ),
    )

    if on_write:
        try:
            # Run the hook through /bin/sh so users can write things like
            # `cp "$1" /some/dir/` in config.toml — the path is bound to
            # `$1` (and "$@"). The placeholder "_" fills $0 so $1 lines up.
            subprocess.run(
                ["/bin/sh", "-c", on_write, "_", str(path)],
                check=False,
                timeout=30,
                env=os.environ.copy(),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            store.log_redaction_failure(conn, "on_write_hook", f"{type(e).__name__}: {e}")

    return WriteResult(fingerprint=fp, path=path, inserted=True)


def recent_logged(
    conn: sqlite3.Connection, *, days: int = 30, limit: int = 50
) -> list[dict[str, Any]]:
    """Recent active recs for prompt-time dedupe context.

    Active = logged / implemented / dispatched. A dispatched rec is
    in-flight as a brief, so it's just as much a "don't re-suggest this"
    signal as a logged one. Dismissed/rejected recs are handled
    separately by ``recent_dismissed``.
    """
    cutoff = store.now_ms() - days * 86400 * 1000
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    rows = conn.execute(
        f"""SELECT id, title, signature, status, created_at
             FROM recommendations
            WHERE created_at >= ?
              AND status IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ?""",
        (cutoff, *ACTIVE_STATUSES, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def render_recent_for_prompt(rows: Iterable[dict[str, Any]]) -> str:
    rows = list(rows)
    if not rows:
        return "_(no recent recommendations)_"
    lines = []
    for r in rows:
        lines.append(f"- [{r['status']}] {r['title']} — `{r['signature']}`")
    return "\n".join(lines)


def recent_dismissed(
    conn: sqlite3.Connection, *, days: int = 30, limit: int = 50
) -> list[dict[str, Any]]:
    """Recently declined recs (dismissed/rejected) with their reasons.

    Filtered on ``updated_at`` (when the rec was declined), not
    ``created_at`` — a rec created months ago but dismissed yesterday is
    fresh, recurrence-relevant signal. Feeds the analyze prompt's
    "previously dismissed — do not re-propose variants" section.
    """
    cutoff = store.now_ms() - days * 86400 * 1000
    placeholders = ",".join("?" for _ in NEGATIVE_STATUSES)
    rows = conn.execute(
        f"""SELECT id, title, signature, status, reason, updated_at
             FROM recommendations
            WHERE updated_at >= ?
              AND status IN ({placeholders})
            ORDER BY updated_at DESC
            LIMIT ?""",
        (cutoff, *NEGATIVE_STATUSES, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def render_dismissed_for_prompt(rows: Iterable[dict[str, Any]]) -> str:
    rows = list(rows)
    if not rows:
        return "_(no recently dismissed recommendations)_"
    lines = []
    for r in rows:
        reason = (r.get("reason") or "").strip() or "(no reason given)"
        lines.append(
            f"- [{r['status']}] {r['title']} — `{r['signature']}` — {reason}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Evidence-ref resolution (probe phase)
# --------------------------------------------------------------------------


def resolve_evidence_refs(
    conn: sqlite3.Connection, refs: list[dict[str, Any]] | None
) -> tuple[int, int]:
    """Count how many of an LLM-produced ``evidence_refs`` list resolve.

    A ref is resolved if its ``external_id`` is a unique-prefix match
    against ``commands.external_id`` (for ``source == 'atuin'`` /
    ``'shell'``) or ``messages.id`` (for ``source == 'msg'`` /
    provider-specific aliases). Returns ``(resolved, total_well_formed)``. Malformed
    refs (missing source or id) are excluded from both counts.

    The 8-char handle the digest emits is normalized by stripping
    dashes; UUIDs in the DB get the same treatment for the comparison.
    Ambiguous prefixes (>1 hit) count as unresolved — that's a signal
    the digest needs longer handles.
    """
    if not refs:
        return (0, 0)
    resolved = 0
    total = 0
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        source = (ref.get("source") or "").strip().lower()
        ext_id_raw = ref.get("external_id") or ""
        ext_id = str(ext_id_raw).strip().replace("-", "")
        if not source or not ext_id:
            continue
        params: tuple[str, ...]
        if source in ("atuin", "shell"):
            query = (
                "SELECT 1 FROM commands "
                "WHERE REPLACE(external_id, '-', '') LIKE ? LIMIT 2"
            )
            params = (ext_id + "%",)
        elif source in ("msg", "message"):
            query = (
                "SELECT 1 FROM messages "
                "WHERE REPLACE(id, '-', '') LIKE ? LIMIT 2"
            )
            params = (ext_id + "%",)
        elif source in ("claude", "codex"):
            query = (
                "SELECT 1 FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE s.source = ? AND REPLACE(m.id, '-', '') LIKE ? LIMIT 2"
            )
            params = (source, ext_id + "%")
        elif source == "session":
            query = (
                "SELECT 1 FROM sessions "
                "WHERE REPLACE(id, '-', '') LIKE ? LIMIT 2"
            )
            params = (ext_id + "%",)
        elif source in ("claude-session", "codex-session"):
            provider = source.removesuffix("-session")
            query = (
                "SELECT 1 FROM sessions WHERE source = ? "
                "AND REPLACE(id, '-', '') LIKE ? LIMIT 2"
            )
            params = (provider, ext_id + "%")
        else:
            # Unknown source — can't resolve, don't count toward total either.
            continue
        total += 1
        hits = conn.execute(query, params).fetchall()
        if len(hits) == 1:
            resolved += 1
    return (resolved, total)


def evidence_cwds(conn: sqlite3.Connection, fm: dict[str, Any]) -> list[str]:
    """Directories the rec's evidence came from, most-cited first.

    Resolves each ``evidence_refs`` handle back to the ``cwd`` of the shell
    command, or the ``project`` of the session, that produced it. Used to tell
    a reader (or an implementing agent) *where* this pattern was happening.
    """
    counts: dict[str, int] = {}
    for ref in fm.get("evidence_refs") or []:
        if not isinstance(ref, dict):
            continue
        eid = "".join(
            c for c in str(ref.get("external_id", "")).lower()
            if c in "0123456789abcdef"
        )
        if len(eid) < 6:
            continue
        src = ref.get("source", "")
        if src in ("atuin", "shell"):
            q = ("SELECT cwd FROM commands WHERE "
                 "replace(external_id,'-','') LIKE ? LIMIT 5")
        elif src in ("claude", "codex", "msg", "message"):
            q = ("SELECT s.project FROM messages m JOIN sessions s "
                 "ON m.session_id=s.id WHERE replace(m.id,'-','') LIKE ? LIMIT 5")
        elif src == "session":
            q = ("SELECT project FROM sessions WHERE "
                 "replace(id,'-','') LIKE ? LIMIT 5")
        else:
            continue
        for row in conn.execute(q, (eid + "%",)).fetchall():
            cwd = row[0]
            if cwd:
                counts[cwd] = counts.get(cwd, 0) + 1
    return [c for c, _ in sorted(counts.items(), key=lambda kv: -kv[1])]


def render_handoff(conn: sqlite3.Connection, rec: dict[str, Any]) -> str:
    """A rec as a paste-ready hand-off for an implementing session.

    The chat-shaped sibling of ``dispatch.render_brief``: same idea — the rec
    verbatim, where its evidence came from, and the contract for closing the
    loop — minus everything that only exists inside a synced repo clone (route,
    branch, git log, verdict files). What you paste into an agent's chat when
    you want *this one* built now.

    The closing commands carry the id so the session can flip the status
    itself. That's the part that keeps the analyzer honest: an implemented or
    dismissed rec (with a reason) stops being re-proposed.
    """
    fm, body = parse_rec_file(rec.get("body_path"))
    id12 = rec["id"][:12]
    facts = []
    for key, label in (("confidence", "confidence"),
                       ("automation_form", "form"),
                       ("target_scope", "scope")):
        if fm.get(key):
            facts.append(f"{label} {fm[key]}")
    created = datetime.fromtimestamp(rec["created_at"] / 1000, tz=UTC)
    facts.append(f"surfaced {created.strftime('%Y-%m-%d')}")

    cwds = evidence_cwds(conn, fm)
    where = "\n".join(f"- `{c}`" for c in cwds[:6]) or "- (no evidence locations resolved)"

    parts = [
        f"# undrudge recommendation {id12}",
        "",
        " · ".join(facts),
        "",
        f"Rec file: `{rec.get('body_path')}`",
        "",
        "## The recommendation (verbatim)",
        "",
        body.strip() or f"# {rec['title']}\n\n_(body file missing)_",
        "",
        "## Where the evidence came from",
        "",
        where,
        "",
        "## Before building it",
        "",
        "undrudge watches history, so a rec can lag the tree by days and its "
        "evidence can be wrong. Check first:",
        "",
        "1. The cited files, commands, and paths exist in this tree.",
        "2. The pain is steady-state, not a burst that already ended.",
        "3. It isn't already solved.",
        "",
        "## When you're done",
        "",
        "```bash",
        f'undrudge implement {id12} --reason "<what you built>"   # built it',
        f'undrudge dismiss   {id12} --reason "<why not>"          # stale or wrong',
        "```",
        "",
        "The reason feeds the analyzer's do-not-re-propose list, so it's worth "
        "a sentence either way.",
    ]
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------
# Listing and status mutation
# --------------------------------------------------------------------------


STATUS_ORDER = ("logged", "dispatched", "implemented", "dismissed", "rejected")
_VALID_STATUS = set(STATUS_ORDER)


def list_recs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    since_ms: int | None = None,
    scope: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = [
        "SELECT id, scope, title, signature, body_path, status,",
        "       created_at, updated_at",
        "FROM recommendations WHERE 1=1",
    ]
    args: list[Any] = []
    if status:
        sql.append("AND status = ?")
        args.append(status)
    if since_ms is not None:
        sql.append("AND created_at >= ?")
        args.append(since_ms)
    if scope:
        sql.append("AND scope = ?")
        args.append(scope)
    sql.append("ORDER BY created_at DESC, id")
    if limit > 0:
        sql.append("LIMIT ?")
        args.append(limit)

    rows = conn.execute(" ".join(sql), args).fetchall()
    return [dict(r) for r in rows]


def public_rec(row: dict[str, Any]) -> dict[str, Any]:
    """Stable, privacy-bounded representation used by machine consumers.

    Deliberately omit the signature and evidence columns. They are analysis
    inputs, not part of the recommendation-listing contract, and can contain
    fragments derived from private activity.
    """
    rec_id = str(row["id"])
    fm, _body = parse_rec_file(row.get("body_path"))
    if not isinstance(fm, dict):
        fm = {}
    confidence = fm.get("confidence")
    if not isinstance(confidence, str) or confidence not in _VALID_CONFIDENCE:
        confidence = None
    target_scope = fm.get("target_scope")
    if not isinstance(target_scope, str) or target_scope not in _VALID_SCOPE:
        target_scope = None
    return {
        "id": rec_id,
        "id12": rec_id[:12],
        "title": str(row["title"]),
        "status": str(row["status"]),
        "scope": str(row["scope"]),
        "confidence": confidence,
        "target_scope": target_scope,
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
    }


@dataclass
class StatusUpdate:
    matched_id: str | None
    old_status: str | None
    new_status: str
    body_path: Path | None
    reason: str | None = None


def find_by_id_prefix(
    conn: sqlite3.Connection, id_prefix: str
) -> list[dict[str, Any]]:
    """Match a fingerprint by full id or any unique-enough prefix.

    Four hex characters usually resolve; the whole row comes back so callers
    that need more than the status don't have to re-query.
    """
    if not id_prefix:
        return []
    rows = conn.execute(
        "SELECT * FROM recommendations WHERE id LIKE ?",
        (id_prefix + "%",),
    ).fetchall()
    return [dict(r) for r in rows]


def set_status(
    conn: sqlite3.Connection,
    id_prefix: str,
    new_status: str,
    *,
    reason: str | None = None,
) -> StatusUpdate:
    """Flip a rec's status. The single status-mutation path.

    ``reason`` (free text, e.g. why a rec was dismissed/rejected) is
    persisted to the DB ``reason`` column AND the on-disk frontmatter,
    and surfaces back through the analyze prompt so the LLM stops
    re-proposing variants of declined ideas. Passing ``reason=None``
    leaves any existing reason untouched (a plain status flip).
    """
    if new_status not in _VALID_STATUS:
        raise ValueError(
            f"invalid status {new_status!r}; expected one of {sorted(_VALID_STATUS)}"
        )
    # A blank reason is treated as "no reason" — preserve any existing one
    # rather than clobbering it with an empty string. This keeps `--reason ""`
    # symmetric with omitting the flag, so the dispatch pipeline's
    # flip-first-then-record flow can't accidentally wipe a recorded reason.
    if reason is not None and not reason.strip():
        reason = None
    matches = find_by_id_prefix(conn, id_prefix)
    if not matches:
        return StatusUpdate(matched_id=None, old_status=None,
                            new_status=new_status, body_path=None, reason=reason)
    if len(matches) > 1:
        raise LookupError(
            f"id prefix {id_prefix!r} is ambiguous "
            f"({len(matches)} matches: {[m['id'][:12] for m in matches]})"
        )
    target = matches[0]
    if reason is not None:
        conn.execute(
            "UPDATE recommendations SET status = ?, reason = ?, updated_at = ? "
            "WHERE id = ?",
            (new_status, reason, store.now_ms(), target["id"]),
        )
    else:
        conn.execute(
            "UPDATE recommendations SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, store.now_ms(), target["id"]),
        )
    body_path = Path(target["body_path"]) if target["body_path"] else None

    # Best-effort: rewrite the frontmatter status field on disk so the
    # markdown reflects reality. Don't fail the operation if the file
    # has been hand-edited or moved.
    if body_path and body_path.exists():
        with contextlib.suppress(OSError, ValueError):
            _rewrite_frontmatter_status(body_path, new_status, reason=reason)

    return StatusUpdate(
        matched_id=target["id"],
        old_status=target["status"],
        new_status=new_status,
        body_path=body_path,
        reason=reason,
    )


def _rewrite_frontmatter_status(
    path: Path, new_status: str, *, reason: str | None = None
) -> None:
    """Update the ``status`` (and optionally ``reason``) field in a rec
    file's JSON header in-place.

    Reads either the current ```json fence or the legacy YAML-style
    ``---`` fence (recs written before that change). Always writes back
    in the new format, so updates passively migrate old files.
    """
    text = path.read_text()
    if text.startswith("```json\n"):
        open_len, close_marker = len("```json\n"), "\n```\n"
    elif text.startswith("---\n"):
        open_len, close_marker = len("---\n"), "\n---\n"
    else:
        return
    end = text.find(close_marker, open_len)
    if end < 0:
        return
    head = text[open_len:end]
    rest = text[end + len(close_marker):]
    fm = json.loads(head)
    fm["status"] = new_status
    if reason is not None:
        fm["reason"] = reason
    new_head = json.dumps(fm, indent=2, default=str, ensure_ascii=False)
    path.write_text(f"```json\n{new_head}\n```\n{rest}")
