"""Listing and status mutation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from undrudge import recommend, store


def _seed(conn: sqlite3.Connection, recs_dir: Path) -> list[recommend.WriteResult]:
    items = [
        recommend.Recommendation(title="A", body_markdown="b", signature="alpha <n>"),
        recommend.Recommendation(title="B", body_markdown="b", signature="beta <str>"),
        recommend.Recommendation(title="C", body_markdown="b", signature="gamma <path>", scope="weekly"),
    ]
    out = []
    for i, rec in enumerate(items):
        # Stagger created_at so order is deterministic.
        result = recommend.write(conn, rec, recs_dir=recs_dir)
        # Nudge created_at backwards by i seconds for stable list order.
        conn.execute(
            "UPDATE recommendations SET created_at = created_at - ? WHERE id = ?",
            (i * 1000, result.fingerprint),
        )
        out.append(result)
    return out


def _seed_evidence_rows(conn: sqlite3.Connection) -> tuple[str, str]:
    """Insert one shell command and one claude message; return their ids."""
    conn.execute(
        "INSERT INTO commands(source, external_id, ts, command) VALUES (?, ?, ?, ?)",
        ("atuin", "abc12345-9999-aaaa-bbbb-cccccccccccc", 1, "ls"),
    )
    conn.execute(
        "INSERT INTO sessions(id, project, started_at, last_seen) "
        "VALUES (?, ?, ?, ?)",
        ("ses-1", "/x", 1, 1),
    )
    conn.execute(
        "INSERT INTO messages(id, session_id, seq, ts, role, text) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("def67890-1111-2222-3333-444444444444", "ses-1", 0, 1, "user", "hi"),
    )
    return ("abc12345-9999-aaaa-bbbb-cccccccccccc",
            "def67890-1111-2222-3333-444444444444")


def test_resolve_evidence_refs_counts_unique_prefix_matches(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    _seed_evidence_rows(conn)
    refs = [
        {"source": "atuin",  "external_id": "abc12345"},   # 8-char handle, hits
        {"source": "claude", "external_id": "def67890"},   # 8-char handle, hits
        {"source": "atuin",  "external_id": "ffffffff"},   # no row, miss
        {"source": "claude", "external_id": ""},           # malformed, drop entirely
        {"source": "weird",  "external_id": "abc"},        # unknown source, drop
        "not even a dict",                                 # drop
    ]
    resolved, total = recommend.resolve_evidence_refs(conn, refs)
    # Two well-formed refs hit, one well-formed ref missed.
    assert (resolved, total) == (2, 3)


def test_resolve_evidence_refs_resolves_session_source(tmp_path: Path):
    """source=session looks up sessions.id, not commands or messages."""
    conn = store.init(tmp_path / "u.sqlite")
    conn.execute(
        "INSERT INTO sessions(id, project, started_at, last_seen) "
        "VALUES (?, ?, ?, ?)",
        ("b8d77c49-feed-face-cafe-decadebabe00", "/x", 1, 1),
    )
    refs = [
        {"source": "session", "external_id": "b8d77c49feed"},   # 12-char prefix
        {"source": "session", "external_id": "00000000dead"},   # no match
    ]
    resolved, total = recommend.resolve_evidence_refs(conn, refs)
    assert (resolved, total) == (1, 2)


def test_resolve_evidence_refs_strips_dashes_in_handles(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    _seed_evidence_rows(conn)
    # Some LLMs may parrot the dashes back; the resolver should still match.
    refs = [{"source": "claude", "external_id": "def6-7890"}]
    resolved, total = recommend.resolve_evidence_refs(conn, refs)
    assert (resolved, total) == (1, 1)


def test_resolve_evidence_refs_filters_provider_aliases(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    conn.execute(
        "INSERT INTO sessions(id, source, project, started_at, last_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        ("codex-session", "codex", "/x", 1, 1),
    )
    conn.execute(
        "INSERT INTO messages(id, session_id, seq, ts, role, text) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("c0de1234-feed-face-cafe-decadebabe00", "codex-session", 0, 1, "user", "hi"),
    )
    assert recommend.resolve_evidence_refs(
        conn, [{"source": "codex", "external_id": "c0de1234"}]
    ) == (1, 1)
    assert recommend.resolve_evidence_refs(
        conn, [{"source": "claude", "external_id": "c0de1234"}]
    ) == (0, 1)


def test_resolve_evidence_refs_treats_ambiguous_prefix_as_unresolved(
    tmp_path: Path,
):
    conn = store.init(tmp_path / "u.sqlite")
    # Two commands sharing a short prefix.
    conn.execute(
        "INSERT INTO commands(source, external_id, ts, command) VALUES (?, ?, ?, ?)",
        ("atuin", "abcd1111", 1, "ls"),
    )
    conn.execute(
        "INSERT INTO commands(source, external_id, ts, command) VALUES (?, ?, ?, ?)",
        ("atuin", "abcd2222", 1, "pwd"),
    )
    refs = [{"source": "atuin", "external_id": "abcd"}]   # ambiguous
    resolved, total = recommend.resolve_evidence_refs(conn, refs)
    assert (resolved, total) == (0, 1)


def test_frontmatter_includes_target_scope(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    rec = recommend.Recommendation(
        title="x", body_markdown="b", signature="sig",
        target_scope="cross_cutting",
    )
    result = recommend.write(conn, rec, recs_dir=tmp_path / "recs")
    text = result.path.read_text()
    fm = json.loads(text.split("```\n", 2)[0].removeprefix("```json\n"))
    assert fm["target_scope"] == "cross_cutting"


def test_frontmatter_defaults_target_scope_to_single_repo(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    rec = recommend.Recommendation(title="x", body_markdown="b", signature="sig")
    result = recommend.write(conn, rec, recs_dir=tmp_path / "recs")
    text = result.path.read_text()
    fm = json.loads(text.split("```\n", 2)[0].removeprefix("```json\n"))
    assert fm["target_scope"] == "single_repo"


def test_frontmatter_includes_evidence_refs_when_present(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    rec = recommend.Recommendation(
        title="x",
        body_markdown="b",
        signature="sig",
        evidence_refs=[
            {"source": "atuin", "external_id": "abc12345"},
        ],
    )
    result = recommend.write(conn, rec, recs_dir=tmp_path / "recs")
    text = result.path.read_text()
    fm = json.loads(text.split("```\n", 2)[0].removeprefix("```json\n"))
    assert fm.get("evidence_refs") == [
        {"source": "atuin", "external_id": "abc12345"}
    ]


def test_frontmatter_omits_evidence_refs_when_absent(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    rec = recommend.Recommendation(
        title="x", body_markdown="b", signature="sig"
    )
    result = recommend.write(conn, rec, recs_dir=tmp_path / "recs")
    text = result.path.read_text()
    fm = json.loads(text.split("```\n", 2)[0].removeprefix("```json\n"))
    assert "evidence_refs" not in fm


def test_list_returns_all_by_default(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    _seed(conn, tmp_path / "recs")
    rows = recommend.list_recs(conn)
    assert len(rows) == 3
    titles = {r["title"] for r in rows}
    assert titles == {"A", "B", "C"}


def test_list_filters_by_status(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    seeded = _seed(conn, tmp_path / "recs")
    conn.execute(
        "UPDATE recommendations SET status = 'dismissed' WHERE id = ?",
        (seeded[0].fingerprint,),
    )
    logged = recommend.list_recs(conn, status="logged")
    dismissed = recommend.list_recs(conn, status="dismissed")
    assert {r["title"] for r in logged} == {"B", "C"}
    assert {r["title"] for r in dismissed} == {"A"}


def test_list_filters_by_scope(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    _seed(conn, tmp_path / "recs")
    daily = recommend.list_recs(conn, scope="daily")
    weekly = recommend.list_recs(conn, scope="weekly")
    assert {r["title"] for r in daily} == {"A", "B"}
    assert {r["title"] for r in weekly} == {"C"}


def test_list_filters_by_since(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    seeded = _seed(conn, tmp_path / "recs")
    # Push the first rec well into the past.
    conn.execute(
        "UPDATE recommendations SET created_at = ? WHERE id = ?",
        (1, seeded[0].fingerprint),
    )
    rows = recommend.list_recs(conn, since_ms=store.now_ms() - 3600 * 1000)
    assert {r["title"] for r in rows} == {"B", "C"}


def test_set_status_dismissed(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    seeded = _seed(conn, tmp_path / "recs")
    target = seeded[0]

    update = recommend.set_status(conn, target.fingerprint[:8], "dismissed")
    assert update.matched_id == target.fingerprint
    assert update.old_status == "logged"
    assert update.new_status == "dismissed"

    row = conn.execute(
        "SELECT status FROM recommendations WHERE id = ?", (target.fingerprint,)
    ).fetchone()
    assert row["status"] == "dismissed"


def test_set_status_implemented_rewrites_frontmatter(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    seeded = _seed(conn, tmp_path / "recs")
    target = seeded[0]

    update = recommend.set_status(conn, target.fingerprint, "implemented")
    assert update.body_path is not None
    assert update.body_path.exists()

    text = update.body_path.read_text()
    assert text.startswith("```json\n")
    fm = json.loads(text.split("```\n", 2)[0].removeprefix("```json\n"))
    assert fm["status"] == "implemented"


def test_set_status_migrates_legacy_dash_fence(tmp_path: Path):
    """A rec written before the ```json change uses --- fences;
    set_status reads it correctly and migrates the file to the new fence."""
    conn = store.init(tmp_path / "u.sqlite")
    seeded = _seed(conn, tmp_path / "recs")
    target = seeded[0]

    # Rewrite the on-disk file in the legacy --- format. Keep the same
    # JSON payload the writer produced so set_status's read path is the
    # only thing under test.
    body_path = Path(
        conn.execute(
            "SELECT body_path FROM recommendations WHERE id = ?",
            (target.fingerprint,),
        ).fetchone()[0]
    )
    text = body_path.read_text()
    parts = text.split("```\n", 2)
    head = parts[0].removeprefix("```json\n").rstrip("\n")
    rest = parts[1]
    body_path.write_text(f"---\n{head}\n---\n{rest}")

    update = recommend.set_status(conn, target.fingerprint, "dismissed")
    assert update.body_path is not None

    new_text = update.body_path.read_text()
    assert new_text.startswith("```json\n")
    fm = json.loads(new_text.split("```\n", 2)[0].removeprefix("```json\n"))
    assert fm["status"] == "dismissed"


def test_set_status_no_match(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    _seed(conn, tmp_path / "recs")
    update = recommend.set_status(conn, "deadbeef", "dismissed")
    assert update.matched_id is None


def test_set_status_ambiguous_prefix_raises(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    # Insert two recs that we *force* to share a prefix.
    conn.execute(
        "INSERT INTO recommendations(id, scope, title, signature, body_path, "
        "evidence, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("aaaa1111", "daily", "x", "sx", "/tmp/x.md", "[]", "logged", 1, 1),
    )
    conn.execute(
        "INSERT INTO recommendations(id, scope, title, signature, body_path, "
        "evidence, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("aaaa2222", "daily", "y", "sy", "/tmp/y.md", "[]", "logged", 1, 1),
    )
    with pytest.raises(LookupError):
        recommend.set_status(conn, "aaaa", "dismissed")


def test_set_status_invalid_value_raises(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    seeded = _seed(conn, tmp_path / "recs")
    with pytest.raises(ValueError):
        recommend.set_status(conn, seeded[0].fingerprint, "frobnicated")


def test_set_status_accepts_dispatched_and_rejected(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    seeded = _seed(conn, tmp_path / "recs")
    d = recommend.set_status(conn, seeded[0].fingerprint, "dispatched")
    assert d.new_status == "dispatched"
    r = recommend.set_status(conn, seeded[1].fingerprint, "rejected")
    assert r.new_status == "rejected"
    statuses = {
        row["id"]: row["status"]
        for row in conn.execute("SELECT id, status FROM recommendations")
    }
    assert statuses[seeded[0].fingerprint] == "dispatched"
    assert statuses[seeded[1].fingerprint] == "rejected"


def test_set_status_persists_reason_to_db_and_frontmatter(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    seeded = _seed(conn, tmp_path / "recs")
    target = seeded[0]

    update = recommend.set_status(
        conn, target.fingerprint, "rejected",
        reason="signature is mis-framed; the real pattern is X",
    )
    assert update.reason == "signature is mis-framed; the real pattern is X"

    row = conn.execute(
        "SELECT status, reason FROM recommendations WHERE id = ?",
        (target.fingerprint,),
    ).fetchone()
    assert row["status"] == "rejected"
    assert row["reason"] == "signature is mis-framed; the real pattern is X"

    fm = json.loads(
        update.body_path.read_text().split("```\n", 2)[0].removeprefix("```json\n")
    )
    assert fm["status"] == "rejected"
    assert fm["reason"] == "signature is mis-framed; the real pattern is X"


def test_set_status_without_reason_leaves_existing_reason(tmp_path: Path):
    """A plain status flip (reason=None) must not wipe a previously
    recorded reason — the dispatch pipeline flips status first and records
    the verdict separately, so a follow-up flip shouldn't clobber."""
    conn = store.init(tmp_path / "u.sqlite")
    seeded = _seed(conn, tmp_path / "recs")
    target = seeded[0]

    recommend.set_status(conn, target.fingerprint, "dismissed", reason="too niche")
    recommend.set_status(conn, target.fingerprint, "logged")  # re-open, no reason

    row = conn.execute(
        "SELECT status, reason FROM recommendations WHERE id = ?",
        (target.fingerprint,),
    ).fetchone()
    assert row["status"] == "logged"
    assert row["reason"] == "too niche"


def test_recent_dismissed_returns_negative_statuses_with_reasons(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    seeded = _seed(conn, tmp_path / "recs")
    recommend.set_status(conn, seeded[0].fingerprint, "dismissed", reason="r1")
    recommend.set_status(conn, seeded[1].fingerprint, "rejected", reason="r2")
    # seeded[2] stays logged — must not appear.

    rows = recommend.recent_dismissed(conn)
    by_title = {r["title"]: r for r in rows}
    assert set(by_title) == {"A", "B"}
    assert by_title["A"]["reason"] == "r1"
    assert by_title["B"]["reason"] == "r2"


def test_render_dismissed_for_prompt_includes_reason():
    rows = [
        {"title": "Wrap rg", "signature": "rg <str>", "status": "dismissed",
         "reason": "we don't want a wrapper"},
        {"title": "Auto inbox", "signature": "loop <cmd>", "status": "rejected",
         "reason": None},
    ]
    out = recommend.render_dismissed_for_prompt(rows)
    assert "we don't want a wrapper" in out
    assert "[dismissed] Wrap rg" in out
    assert "[rejected] Auto inbox" in out
    assert "(no reason given)" in out


def test_render_dismissed_for_prompt_empty():
    assert "no recently dismissed" in recommend.render_dismissed_for_prompt([])


def test_recent_logged_includes_dispatched_excludes_negative(tmp_path: Path):
    """recent_logged spans the active set (logged/implemented/dispatched)
    and must exclude the negative statuses, which recent_dismissed owns."""
    conn = store.init(tmp_path / "u.sqlite")
    items = [
        recommend.Recommendation(title="L", body_markdown="b", signature="s-l"),
        recommend.Recommendation(title="D", body_markdown="b", signature="s-d"),
        recommend.Recommendation(title="X", body_markdown="b", signature="s-x"),
        recommend.Recommendation(title="R", body_markdown="b", signature="s-r"),
    ]
    fps = {}
    for rec in items:
        fps[rec.title] = recommend.write(
            conn, rec, recs_dir=tmp_path / "recs"
        ).fingerprint
    recommend.set_status(conn, fps["D"], "dispatched")
    recommend.set_status(conn, fps["X"], "dismissed", reason="no")
    recommend.set_status(conn, fps["R"], "rejected", reason="mis-framed")
    # "L" stays logged.

    titles = {r["title"] for r in recommend.recent_logged(conn)}
    assert titles == {"L", "D"}
    assert "X" not in titles and "R" not in titles


def test_recent_dismissed_filters_on_updated_at_not_created_at(tmp_path: Path):
    """A rec created long ago but dismissed recently must appear (proves
    updated_at filtering); a rec dismissed long ago must drop out."""
    conn = store.init(tmp_path / "u.sqlite")
    fresh = recommend.write(
        conn,
        recommend.Recommendation(title="Fresh", body_markdown="b", signature="s-f"),
        recs_dir=tmp_path / "recs",
    ).fingerprint
    stale = recommend.write(
        conn,
        recommend.Recommendation(title="Stale", body_markdown="b", signature="s-s"),
        recs_dir=tmp_path / "recs",
    ).fingerprint

    recommend.set_status(conn, fresh, "dismissed", reason="r")
    recommend.set_status(conn, stale, "dismissed", reason="r")

    long_ago = store.now_ms() - 90 * 86400 * 1000
    # Fresh: created 90d ago, but dismissed (updated) just now → in window.
    conn.execute(
        "UPDATE recommendations SET created_at = ? WHERE id = ?", (long_ago, fresh)
    )
    # Stale: dismissed 90d ago → out of the 30-day window.
    conn.execute(
        "UPDATE recommendations SET updated_at = ? WHERE id = ?", (long_ago, stale)
    )

    titles = {r["title"] for r in recommend.recent_dismissed(conn)}
    assert titles == {"Fresh"}


def test_set_status_empty_reason_preserves_existing(tmp_path: Path):
    """A blank --reason ("" or whitespace) is treated as no reason — it
    must not clobber a previously recorded reason."""
    conn = store.init(tmp_path / "u.sqlite")
    seeded = _seed(conn, tmp_path / "recs")
    target = seeded[0].fingerprint

    recommend.set_status(conn, target, "dismissed", reason="real reason")
    recommend.set_status(conn, target, "rejected", reason="   ")  # blank

    row = conn.execute(
        "SELECT status, reason FROM recommendations WHERE id = ?", (target,)
    ).fetchone()
    assert row["status"] == "rejected"
    assert row["reason"] == "real reason"
