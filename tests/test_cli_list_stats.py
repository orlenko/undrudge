"""Machine-readable listing and durable recommendation statistics."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from undrudge import cli, recommend, store
from undrudge import config as cfg_mod


def _cfg(monkeypatch, tmp_path: Path) -> cfg_mod.Config:
    data = tmp_path / "data"
    cfg = cfg_mod.Config(
        paths=cfg_mod.Paths(
            db=data / "undrudge.sqlite",
            recs_dir=data / "recommendations",
            digests_dir=data / "digests",
            logs_dir=data / "logs",
            events_log=data / "events.jsonl",
        ),
        claude=cfg_mod.Claude(projects_root=tmp_path / "claude"),
        atuin=cfg_mod.Atuin(db=tmp_path / "atuin.db"),
    )
    store.init(cfg.paths.db).close()
    monkeypatch.setattr(cfg_mod, "load", lambda path=None: cfg)
    return cfg


def _write(
    cfg: cfg_mod.Config,
    *,
    title: str,
    signature: str,
    status: str = "logged",
    scope: str = "daily",
    confidence: str = "medium",
    target_scope: str = "single_repo",
) -> tuple[str, Path]:
    conn = store.open_db(cfg.paths.db)
    try:
        result = recommend.write(
            conn,
            recommend.Recommendation(
                title=title,
                body_markdown="Private explanatory body.",
                signature=signature,
                evidence=["private prose evidence"],
                confidence=confidence,
                target_scope=target_scope,
                scope=scope,
            ),
            recs_dir=cfg.paths.recs_dir,
        )
        if status != "logged":
            recommend.set_status(conn, result.fingerprint, status)
        assert result.path is not None
        return result.fingerprint, result.path
    finally:
        conn.close()


def test_list_json_has_stable_privacy_bounded_shape(monkeypatch, tmp_path, capsys):
    cfg = _cfg(monkeypatch, tmp_path)
    rec_id, path = _write(
        cfg,
        title="Visible title",
        signature="private signature fragment",
        confidence="high",
        target_scope="agent_global",
    )

    assert cli.main(["list", "--json"]) == 0

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload == [{
        "id": rec_id,
        "id12": rec_id[:12],
        "title": "Visible title",
        "status": "logged",
        "scope": "daily",
        "confidence": "high",
        "target_scope": "agent_global",
        "created_at": payload[0]["created_at"],
        "updated_at": payload[0]["updated_at"],
    }]
    assert isinstance(payload[0]["created_at"], int)
    assert isinstance(payload[0]["updated_at"], int)
    assert str(path) not in output
    assert "private signature" not in output
    assert "private prose" not in output


def test_list_json_failure_isolates_unreadable_frontmatter(
    monkeypatch, tmp_path, capsys
):
    cfg = _cfg(monkeypatch, tmp_path)
    bad_id, bad_path = _write(
        cfg, title="Unreadable", signature="non utf recommendation metadata"
    )
    good_id, _ = _write(
        cfg, title="Readable", signature="ordinary readable recommendation"
    )
    bad_path.write_bytes(b"```json\n\xff\n```\n")

    assert cli.main(["list", "--json", "--limit", "0"]) == 0

    payload = {row["id"]: row for row in json.loads(capsys.readouterr().out)}
    assert set(payload) == {bad_id, good_id}
    assert payload[bad_id]["confidence"] is None
    assert payload[bad_id]["target_scope"] is None
    assert payload[good_id]["confidence"] == "medium"


def test_list_json_rejects_malformed_metadata_types(monkeypatch, tmp_path, capsys):
    cfg = _cfg(monkeypatch, tmp_path)
    rec_id, path = _write(
        cfg, title="Malformed metadata", signature="wrong metadata value types"
    )
    path.write_text(
        "```json\n"
        + json.dumps({"confidence": ["high"], "target_scope": 42})
        + "\n```\n\nbody\n"
    )

    assert cli.main(["list", "--json"]) == 0

    row = json.loads(capsys.readouterr().out)[0]
    assert row["id"] == rec_id
    assert row["confidence"] is None
    assert row["target_scope"] is None


def test_list_json_limit_zero_and_tie_order_are_deterministic(
    monkeypatch, tmp_path, capsys
):
    cfg = _cfg(monkeypatch, tmp_path)
    ids = [
        _write(cfg, title="One", signature="first distinct listing pattern")[0],
        _write(cfg, title="Two", signature="second unrelated listing pattern")[0],
    ]
    conn = store.open_db(cfg.paths.db)
    try:
        conn.execute("UPDATE recommendations SET created_at = 1000")
        conn.commit()
    finally:
        conn.close()

    assert cli.main(["list", "--json", "--limit", "0"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in payload] == sorted(ids)


def test_list_json_empty_is_an_empty_array(monkeypatch, tmp_path, capsys):
    _cfg(monkeypatch, tmp_path)

    assert cli.main(["list", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == []


def test_list_human_output_is_unchanged(monkeypatch, tmp_path, capsys):
    cfg = _cfg(monkeypatch, tmp_path)
    rec_id, _ = _write(cfg, title="Human title", signature="human-list")
    created_at = 1_767_225_600_000  # 2026-01-01T00:00:00Z
    conn = store.open_db(cfg.paths.db)
    try:
        conn.execute(
            "UPDATE recommendations SET created_at=?, updated_at=? WHERE id=?",
            (created_at, created_at, rec_id),
        )
        conn.commit()
    finally:
        conn.close()

    assert cli.main(["list"]) == 0

    assert capsys.readouterr().out == (
        f"2026-01-01  {rec_id[:12]}  logged      daily  Human title\n"
    )


def test_stats_has_deterministic_status_and_scope_counts(
    monkeypatch, tmp_path, capsys
):
    cfg = _cfg(monkeypatch, tmp_path)
    _write(cfg, title="Daily logged", signature="collect daily deployment warnings")
    _write(
        cfg,
        title="Weekly implemented",
        signature="summarize weekly automation outcomes",
        status="implemented",
        scope="weekly",
    )
    _write(
        cfg,
        title="Weekly dismissed",
        signature="archive repetitive weekly scratch work",
        status="dismissed",
        scope="weekly",
    )

    assert cli.main(["stats"]) == 0

    assert capsys.readouterr().out == (
        "recommendations: 3\n"
        "status:\n"
        "  logged      1\n"
        "  dispatched  0\n"
        "  implemented 1\n"
        "  dismissed   1\n"
        "  rejected    0\n"
        "scope:\n"
        "  daily       1\n"
        "  weekly      2\n"
    )


def test_stats_empty_data_has_fixed_zero_counts(monkeypatch, tmp_path, capsys):
    _cfg(monkeypatch, tmp_path)

    assert cli.main(["stats"]) == 0

    assert capsys.readouterr().out == (
        "recommendations: 0\n"
        "status:\n"
        "  logged      0\n"
        "  dispatched  0\n"
        "  implemented 0\n"
        "  dismissed   0\n"
        "  rejected    0\n"
        "scope:\n"
        "  daily       0\n"
        "  weekly      0\n"
    )


def test_stats_does_not_create_a_missing_database(monkeypatch, tmp_path, capsys):
    cfg = _cfg(monkeypatch, tmp_path)
    cfg.paths.db.unlink()

    assert cli.main(["stats"]) == 1

    assert not cfg.paths.db.exists()
    assert "recommendation DB unavailable" in capsys.readouterr().err


def test_readonly_store_connection_rejects_writes(tmp_path):
    db = tmp_path / "undrudge.sqlite"
    store.init(db).close()

    conn = store.open_db_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM recommendations")
    finally:
        conn.close()
