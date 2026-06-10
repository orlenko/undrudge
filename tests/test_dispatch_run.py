"""Dispatch orchestration — end-to-end with a stubbed subprocess.

Seeds a real undrudge store with a logged rec, runs the Dispatcher
against temp route/state/vault dirs, and asserts the brief + status flip
+ ledger happen (and that --dry-run touches nothing). Subprocess (git /
gh) is stubbed so the test needs no real repos or tools.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from undrudge import config as cfg_mod
from undrudge import dispatch, dispatch_run, store
from undrudge.dispatch import DispatchConfig, Route
from undrudge.dispatch_run import Dispatcher

_NOW = datetime(2026, 6, 10, 7, 0, tzinfo=UTC)


def _seed_rec(conn, recs_dir: Path, *, id_: str, title: str, fm: dict) -> None:
    day = recs_dir / "2026-06-10"
    day.mkdir(parents=True, exist_ok=True)
    body_path = day / f"{id_[:8]}.md"
    body_path.write_text(
        f"```json\n{json.dumps(fm, indent=2)}\n```\n\n# {title}\n\nthe rec body\n"
    )
    conn.execute(
        "INSERT INTO recommendations(id, scope, title, signature, body_path, "
        "evidence, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (id_, "daily", title, "find <str> | grep <str>", str(body_path),
         "[]", "logged", 1000, 1000),
    )


def _ucfg(tmp_path: Path, db: Path) -> cfg_mod.Config:
    return cfg_mod.Config(
        paths=cfg_mod.Paths(
            db=db, recs_dir=tmp_path / "recs", digests_dir=tmp_path / "dig",
            logs_dir=tmp_path / "logs", events_log=tmp_path / "events.jsonl",
        ),
        claude=cfg_mod.Claude(projects_root=tmp_path / "c"),
        atuin=cfg_mod.Atuin(db=tmp_path / "a.db"),
    )


def _dcfg(tmp_path: Path) -> DispatchConfig:
    route_dir = tmp_path / "testrepo"
    route_dir.mkdir()
    (route_dir / "README").write_text("non-empty so route_dir_ok passes")
    return DispatchConfig(
        routes=[Route(name="testrepo", dir=str(route_dir), auto=True,
                      sync=None, stage="")],
        clones_dir=str(tmp_path / "clones"),
        state_dir=str(tmp_path / "state"),
        vault_dir=str(tmp_path / "vault"),
        gate_confidence=("high", "medium"),
        gate_forms=("script", "extend_existing", "slash_command"),
        max_dispatch_per_run=6,
    )


def _make(tmp_path: Path, *, dry: bool):
    db = tmp_path / "u.sqlite"
    conn = store.init(db)
    d = Dispatcher(_ucfg(tmp_path, db), _dcfg(tmp_path), conn, dry=dry, now=_NOW)
    d._run = lambda *a, **k: (127, "", "")  # no real git/gh
    return conn, d


def test_run_dispatches_writes_brief_and_marks_dispatched(tmp_path: Path):
    conn, d = _make(tmp_path, dry=False)
    _seed_rec(conn, tmp_path / "recs", id_="a1b2c3d4e5f6aaaa",
              title="Improve testrepo caching",
              fm={"confidence": "high", "automation_form": "script",
                  "target_scope": "single_repo"})

    result = d.run()

    assert len(result.dispatched) == 1
    # Brief written to the route's inbox.
    brief = tmp_path / "testrepo" / ".undrudge-inbox" / "a1b2c3d4e5f6.md"
    assert brief.exists()
    assert "Improve testrepo caching" in brief.read_text()
    # Status flipped logged -> dispatched.
    status = conn.execute(
        "SELECT status FROM recommendations WHERE id='a1b2c3d4e5f6aaaa'"
    ).fetchone()[0]
    assert status == "dispatched"
    # Ledger records the dispatch.
    led = dispatch.Ledger.load(tmp_path / "state" / "ledger.jsonl")
    assert led.has_action("a1b2c3d4e5f6", "dispatched")


def test_dry_run_touches_nothing(tmp_path: Path):
    conn, d = _make(tmp_path, dry=True)
    _seed_rec(conn, tmp_path / "recs", id_="a1b2c3d4e5f6aaaa",
              title="Improve testrepo caching",
              fm={"confidence": "high", "automation_form": "script",
                  "target_scope": "single_repo"})

    result = d.run()

    assert len(result.dispatched) == 1            # computed
    assert not (tmp_path / "testrepo" / ".undrudge-inbox").exists()  # not written
    assert not (tmp_path / "state" / "ledger.jsonl").exists()
    status = conn.execute(
        "SELECT status FROM recommendations WHERE id='a1b2c3d4e5f6aaaa'"
    ).fetchone()[0]
    assert status == "logged"                     # not flipped


def test_low_confidence_rec_is_held_not_dispatched(tmp_path: Path):
    conn, d = _make(tmp_path, dry=False)
    _seed_rec(conn, tmp_path / "recs", id_="bbbb2222cccc3333",
              title="Tweak testrepo logging",
              fm={"confidence": "low", "automation_form": "script",
                  "target_scope": "single_repo"})

    result = d.run()

    assert result.dispatched == []
    assert any("bbbb2222cccc" in h for h in result.held)
    status = conn.execute(
        "SELECT status FROM recommendations WHERE id='bbbb2222cccc3333'"
    ).fetchone()[0]
    assert status == "logged"


def test_status_lists_logged_recs(tmp_path: Path):
    conn, d = _make(tmp_path, dry=True)
    _seed_rec(conn, tmp_path / "recs", id_="a1b2c3d4e5f6aaaa", title="X",
              fm={"confidence": "high", "automation_form": "script"})
    lines = d.status()
    assert any("logged recs: 1" in line for line in lines)
    assert any("a1b2c3d4e5f6" in line and "pending" in line for line in lines)


# --------------------------------------------------------------------------
# Config resolution: [dispatch] TOML preferred, prototype config.json fallback
# --------------------------------------------------------------------------


def _ucfg_min(tmp_path: Path) -> cfg_mod.Config:
    return _ucfg(tmp_path, tmp_path / "db.sqlite")


def test_load_config_prefers_dispatch_toml(tmp_path: Path):
    toml = tmp_path / "config.toml"
    toml.write_text(
        '[dispatch]\nclones_dir = "/c"\n'
        '[[dispatch.routes]]\nname = "r-toml"\ndir = "/d"\n'
    )
    proto = tmp_path / "config.json"
    proto.write_text(json.dumps({"clones_dir": "/x", "routes": [{"name": "r-json"}]}))
    cfg = dispatch_run.load_dispatch_config(
        _ucfg_min(tmp_path), config_toml_path=toml, prototype_json_path=str(proto))
    assert cfg is not None
    assert cfg.clones_dir == "/c"
    assert cfg.routes[0].name == "r-toml"  # TOML wins over the JSON fallback


def test_load_config_falls_back_to_prototype_json(tmp_path: Path):
    toml = tmp_path / "config.toml"
    toml.write_text('[paths]\ndb = "x"\n')  # no [dispatch] table
    proto = tmp_path / "config.json"
    proto.write_text(json.dumps(
        {"clones_dir": "/c2", "routes": [{"name": "r2", "dir": "/d2"}]}))
    cfg = dispatch_run.load_dispatch_config(
        _ucfg_min(tmp_path), config_toml_path=toml, prototype_json_path=str(proto))
    assert cfg is not None
    assert cfg.clones_dir == "/c2"
    assert cfg.routes[0].name == "r2"


def test_load_config_none_when_unconfigured(tmp_path: Path):
    cfg = dispatch_run.load_dispatch_config(
        _ucfg_min(tmp_path),
        config_toml_path=tmp_path / "missing.toml",
        prototype_json_path=str(tmp_path / "missing.json"))
    assert cfg is None
