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


def find_similar(
    conn: sqlite3.Connection,
    *,
    scope: str,
    signature: str,
    threshold: float = SIGNATURE_SIMILARITY_THRESHOLD,
) -> dict[str, Any] | None:
    """Return the closest existing recommendation if its signature is
    near-identical to ``signature`` under the canonical form.

    Only considers ``logged`` and ``implemented`` rows — dismissed recs
    don't block a re-suggestion. Returns ``None`` if nothing crosses the
    threshold.
    """
    rows = conn.execute(
        """SELECT id, signature, body_path, status
             FROM recommendations
            WHERE scope = ? AND status IN ('logged', 'implemented')""",
        (scope,),
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
_VALID_FORM = {"slash_command", "script", "hook", "shell_alias", "extend_existing", "other"}
_VALID_CONFIDENCE = {"high", "medium", "low"}


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
        "status": "logged",
        "created": created.isoformat(timespec="seconds"),
        "confidence": rec.confidence,
        "automation_form": rec.automation_form,
        "signature": rec.signature,
        "evidence": rec.evidence or [],
    }
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


def normalize(rec: Recommendation) -> Recommendation:
    """Tighten free-form LLM output to known enums and trimmed strings."""
    form = rec.automation_form.strip().lower().replace(" ", "_") if rec.automation_form else "other"
    if form not in _VALID_FORM:
        form = "other"
    confidence = rec.confidence.strip().lower() if rec.confidence else "medium"
    if confidence not in _VALID_CONFIDENCE:
        confidence = "medium"
    return Recommendation(
        title=rec.title.strip()[:200],
        body_markdown=rec.body_markdown.strip(),
        signature=rec.signature.strip(),
        automation_form=form,
        confidence=confidence,
        rationale=rec.rationale.strip(),
        evidence=rec.evidence or [],
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
    """Recent (logged or implemented) recs for prompt-time dedupe context."""
    cutoff = store.now_ms() - days * 86400 * 1000
    rows = conn.execute(
        """SELECT id, title, signature, status, created_at
             FROM recommendations
            WHERE created_at >= ?
              AND status IN ('logged', 'implemented')
            ORDER BY created_at DESC
            LIMIT ?""",
        (cutoff, limit),
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


# --------------------------------------------------------------------------
# Listing and status mutation
# --------------------------------------------------------------------------


_VALID_STATUS = {"logged", "dismissed", "implemented"}


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
    sql.append("ORDER BY created_at DESC LIMIT ?")
    args.append(limit)

    rows = conn.execute(" ".join(sql), args).fetchall()
    return [dict(r) for r in rows]


@dataclass
class StatusUpdate:
    matched_id: str | None
    old_status: str | None
    new_status: str
    body_path: Path | None


def find_by_id_prefix(
    conn: sqlite3.Connection, id_prefix: str
) -> list[dict[str, Any]]:
    """Match a fingerprint by full id or any unique-enough prefix."""
    if not id_prefix:
        return []
    rows = conn.execute(
        "SELECT id, status, body_path FROM recommendations WHERE id LIKE ?",
        (id_prefix + "%",),
    ).fetchall()
    return [dict(r) for r in rows]


def set_status(
    conn: sqlite3.Connection, id_prefix: str, new_status: str
) -> StatusUpdate:
    if new_status not in _VALID_STATUS:
        raise ValueError(
            f"invalid status {new_status!r}; expected one of {sorted(_VALID_STATUS)}"
        )
    matches = find_by_id_prefix(conn, id_prefix)
    if not matches:
        return StatusUpdate(matched_id=None, old_status=None,
                            new_status=new_status, body_path=None)
    if len(matches) > 1:
        raise LookupError(
            f"id prefix {id_prefix!r} is ambiguous "
            f"({len(matches)} matches: {[m['id'][:12] for m in matches]})"
        )
    target = matches[0]
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
            _rewrite_frontmatter_status(body_path, new_status)

    return StatusUpdate(
        matched_id=target["id"],
        old_status=target["status"],
        new_status=new_status,
        body_path=body_path,
    )


def _rewrite_frontmatter_status(path: Path, new_status: str) -> None:
    """Update the ``status`` field in a rec file's JSON header in-place.

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
    new_head = json.dumps(fm, indent=2, default=str, ensure_ascii=False)
    path.write_text(f"```json\n{new_head}\n```\n{rest}")
