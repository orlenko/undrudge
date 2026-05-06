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
