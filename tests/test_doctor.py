"""Doctor's gather-health check.

The check must surface a *currently broken* gather (silent failure is how
the atuin WAL race festered) without crying wolf over transient failures
that self-heal on the next hourly run. It keys off the latest
``gather_complete`` event, not "any failure in the window".
"""

from __future__ import annotations

import json
from pathlib import Path

from undrudge import cli, store
from undrudge import config as cfg_mod


def _cfg(tmp_path: Path) -> cfg_mod.Config:
    data = tmp_path / "data"
    data.mkdir()
    return cfg_mod.Config(
        paths=cfg_mod.Paths(
            db=tmp_path / "u.sqlite",
            recs_dir=data / "recs",
            digests_dir=data / "digests",
            logs_dir=data / "logs",
            events_log=data / "events.jsonl",
        ),
        claude=cfg_mod.Claude(projects_root=tmp_path / "claude"),
        atuin=cfg_mod.Atuin(db=tmp_path / "atuin.db"),
    )


def _write_events(cfg: cfg_mod.Config, events: list[dict]) -> None:
    cfg.paths.events_log.parent.mkdir(parents=True, exist_ok=True)
    with cfg.paths.events_log.open("w", encoding="utf-8") as fp:
        for e in events:
            fp.write(json.dumps(e) + "\n")


def _run_check(cfg: cfg_mod.Config) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        results.append((label, cond, detail))

    cli._check_gather_health(cfg, check)
    return results


def test_history_sources_expose_missing_codex_when_claude_exists(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.claude.projects_root.mkdir()
    cfg.codex = cfg_mod.Codex(home=tmp_path / "missing-codex")

    availability = {
        label: available for label, available, _ in cli._history_sources(cfg)
    }

    assert availability == {"claude history": True, "codex history": False}


def test_doctor_fails_when_latest_run_still_failing(tmp_path: Path):
    cfg = _cfg(tmp_path)
    now = store.now_ms()
    _write_events(cfg, [
        {"ts": now - 1000, "event": "gather_failed", "source": "shell"},
        {"ts": now - 900, "event": "gather_complete", "failed_sources": ["shell"]},
    ])
    [(_, cond, detail)] = _run_check(cfg)
    assert cond is False
    assert "shell" in detail


def test_doctor_ok_when_latest_clean_after_transient(tmp_path: Path):
    """A transient failure followed by a clean run is OK (self-healed),
    surfaced as an info note rather than a FAIL — the morning-noise fix."""
    cfg = _cfg(tmp_path)
    now = store.now_ms()
    _write_events(cfg, [
        {"ts": now - 5000, "event": "gather_failed", "source": "shell"},
        {"ts": now - 4000, "event": "gather_complete", "failed_sources": ["shell"]},
        {"ts": now - 1000, "event": "gather_complete", "failed_sources": []},
    ])
    [(_, cond, detail)] = _run_check(cfg)
    assert cond is True
    assert "self-healed" in detail or "transient" in detail


def test_doctor_clean_when_no_failures(tmp_path: Path):
    cfg = _cfg(tmp_path)
    now = store.now_ms()
    _write_events(cfg, [
        {"ts": now - 1000, "event": "gather_complete", "failed_sources": []},
    ])
    [(_, cond, detail)] = _run_check(cfg)
    assert cond is True
    assert "transient" not in detail


def test_doctor_falls_back_when_no_gather_complete(tmp_path: Path):
    """Pre-upgrade events (gather_failed, no gather_complete) still flag."""
    cfg = _cfg(tmp_path)
    now = store.now_ms()
    _write_events(cfg, [
        {"ts": now - 1000, "event": "gather_failed", "source": "shell"},
    ])
    [(_, cond, _)] = _run_check(cfg)
    assert cond is False


def test_doctor_old_failures_outside_window_ignored(tmp_path: Path):
    cfg = _cfg(tmp_path)
    now = store.now_ms()
    old = now - 48 * 3600 * 1000
    _write_events(cfg, [
        {"ts": old, "event": "gather_failed", "source": "shell"},
        {"ts": old, "event": "gather_complete", "failed_sources": ["shell"]},
        {"ts": now - 1000, "event": "gather_complete", "failed_sources": []},
    ])
    [(_, cond, _)] = _run_check(cfg)
    assert cond is True


def test_doctor_missing_events_log(tmp_path: Path):
    cfg = _cfg(tmp_path)
    [(_, cond, detail)] = _run_check(cfg)
    assert cond is False
    assert "not created" in detail or "not " in detail


def test_gather_emits_gather_complete(
    claude_projects: Path, atuin_db: Path, monkeypatch, tmp_path: Path
):
    """A real gather run records one gather_complete with the per-source
    outcome — the signal doctor keys off."""
    cfg = _cfg(tmp_path)
    cfg.claude = cfg_mod.Claude(projects_root=claude_projects)
    cfg.atuin = cfg_mod.Atuin(db=atuin_db)
    store.init(cfg.paths.db).close()
    monkeypatch.setattr(cfg_mod, "load", lambda path=None: cfg)

    rc = cli.main(["gather"])
    assert rc == 0

    events = [
        json.loads(line)
        for line in cfg.paths.events_log.read_text().splitlines()
        if line.strip()
    ]
    completes = [e for e in events if e.get("event") == "gather_complete"]
    assert len(completes) == 1
    assert completes[0]["failed_sources"] == []
    assert "shell_rows" in completes[0]


def test_gather_isolates_codex_failure(
    claude_projects: Path, atuin_db: Path, monkeypatch, tmp_path: Path
):
    cfg = _cfg(tmp_path)
    cfg.claude = cfg_mod.Claude(projects_root=claude_projects)
    cfg.codex = cfg_mod.Codex(home=tmp_path / "codex")
    cfg.atuin = cfg_mod.Atuin(db=atuin_db)
    store.init(cfg.paths.db).close()
    monkeypatch.setattr(cfg_mod, "load", lambda path=None: cfg)

    def fail_codex(*_args, **_kwargs):
        raise RuntimeError("synthetic codex failure")

    monkeypatch.setattr(cli.ingest_codex, "ingest", fail_codex)
    rc = cli.main(["gather"])

    assert rc == 1
    events = [
        json.loads(line)
        for line in cfg.paths.events_log.read_text().splitlines()
        if line.strip()
    ]
    complete = next(e for e in events if e.get("event") == "gather_complete")
    assert complete["failed_sources"] == ["codex"]
    assert complete["claude_rows"] is not None
    assert complete["shell_rows"] is not None
    assert complete["codex_rows"] is None
