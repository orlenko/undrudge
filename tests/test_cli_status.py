"""CLI-level tests for the status verbs (dismiss / implement / mark).

These go through ``cli.main`` so the argparse wiring and the
events.jsonl side effects are under test — the library-level
``recommend.set_status`` tests don't touch either. Config is patched to
a temp dir so ``$HOME`` is never read or written.
"""

from __future__ import annotations

import json
from pathlib import Path

from undrudge import cli, recommend, store


def _prep_cfg(monkeypatch, tmp_path: Path):
    from undrudge import config as cfg_mod

    data = tmp_path / "data"
    data.mkdir()
    cfg = cfg_mod.Config(
        paths=cfg_mod.Paths(
            db=tmp_path / "udb.sqlite",
            recs_dir=data / "recs",
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


def _seed_one(cfg) -> str:
    conn = store.open_db(cfg.paths.db)
    try:
        store.apply_schema(conn)
        res = recommend.write(
            conn,
            recommend.Recommendation(
                title="Wrap find/grep", body_markdown="b",
                signature="find . -name <str> | xargs grep <str>",
            ),
            recs_dir=cfg.paths.recs_dir,
        )
    finally:
        conn.close()
    return res.fingerprint


def _events(cfg) -> list[dict]:
    if not cfg.paths.events_log.exists():
        return []
    return [
        json.loads(line)
        for line in cfg.paths.events_log.read_text().splitlines()
        if line.strip()
    ]


def test_mark_dispatched_flips_status(monkeypatch, tmp_path, capsys):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    fp = _seed_one(cfg)

    rc = cli.main(["mark", fp[:12], "dispatched"])
    assert rc == 0

    conn = store.open_db(cfg.paths.db)
    try:
        status = conn.execute(
            "SELECT status FROM recommendations WHERE id = ?", (fp,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "dispatched"


def test_mark_rejects_invalid_status_via_argparse(monkeypatch, tmp_path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    fp = _seed_one(cfg)
    # argparse choices=[...] should reject before any DB work; SystemExit
    # with a nonzero code is how argparse signals a bad choice.
    try:
        cli.main(["mark", fp[:12], "frobnicated"])
    except SystemExit as e:
        assert e.code != 0
    else:  # pragma: no cover - the call must not succeed
        raise AssertionError("expected SystemExit from argparse on bad status")


def test_mark_reason_lands_in_events_jsonl(monkeypatch, tmp_path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    fp = _seed_one(cfg)

    rc = cli.main(["mark", fp[:12], "rejected", "--reason", "signature mis-framed"])
    assert rc == 0

    events = _events(cfg)
    rejected = [e for e in events if e.get("event") == "rec_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "signature mis-framed"
    assert rejected[0]["from_status"] == "logged"


def test_dismiss_reason_lands_in_events_jsonl(monkeypatch, tmp_path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    fp = _seed_one(cfg)

    rc = cli.main(["dismiss", fp[:12], "--reason", "too niche to automate"])
    assert rc == 0

    events = _events(cfg)
    dismissed = [e for e in events if e.get("event") == "rec_dismissed"]
    assert len(dismissed) == 1
    assert dismissed[0]["reason"] == "too niche to automate"
