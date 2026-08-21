"""Pipeline health summaries from synthetic audit events and databases."""

from __future__ import annotations

import json
from pathlib import Path

from undrudge import cli, health, recommend, store
from undrudge import config as cfg_mod


def _cfg(monkeypatch, tmp_path: Path, *, with_db: bool = True) -> cfg_mod.Config:
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
    if with_db:
        store.init(cfg.paths.db).close()
    monkeypatch.setattr(cfg_mod, "load", lambda path=None: cfg)
    return cfg


def _write(path: Path, entries: list[dict], *, malformed: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(entry) for entry in entries]
    if malformed:
        lines.insert(1, "{not json")
    path.write_text("\n".join(lines) + "\n")


def test_event_summary_keeps_latest_runs_and_windows_totals(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    _write(log, [
        {"ts": 100, "event": "gather_complete", "failed_sources": ["shell"]},
        {"ts": 110, "event": "analyze_complete", "scope": "daily",
         "parsed": 1, "written": 1, "skipped": 0},
        {"ts": 200, "event": "gather_failed", "source": "shell"},
        {"ts": 210, "event": "gather_complete", "failed_sources": [],
         "claude_rows": 2, "codex_rows": 3, "shell_rows": 4},
        {"ts": 220, "event": "analyze_complete", "scope": "daily",
         "parsed": 3, "written": 2, "skipped": 1},
        {"ts": 230, "event": "rec_written", "title": "do not leak this"},
        {"ts": 240, "event": "prune_failed", "error_msg": "also private"},
        {"ts": 250, "event": "secret-name_failed"},
        {"ts": 260, "event": "analyze_complete", "scope": "secret-scope"},
        {"ts": 270, "event": "gather_complete", "failed_sources": ["secret-source"]},
    ], malformed=True)

    result = health.summarize_events(log, cutoff_ms=205)

    assert result["latest"]["gather"] == {
        "ts": 270,
        "legacy_failure": False,
        "failed_sources": ["unknown"],
        "rows": {"claude": None, "codex": None, "shell": None},
    }
    assert result["latest"]["analyze"]["daily"] == {
        "ts": 220, "parsed": 3, "written": 2, "skipped": 1,
    }
    assert result["latest"]["analyze"]["weekly"] is None
    assert result["window"]["gather_runs"] == 2
    assert result["window"]["gather_failed_runs"] == 1
    assert result["window"]["analyze"]["daily"] == {
        "runs": 1, "parsed": 3, "written": 2, "skipped": 1,
    }
    assert result["window"]["recommendations_written"] == 1
    assert result["window"]["failures"] == {"other_failed": 1, "prune_failed": 1}
    assert result["malformed_lines"] == 1
    assert "do not leak" not in json.dumps(result)
    assert "also private" not in json.dumps(result)
    assert "secret-name" not in json.dumps(result)
    assert "secret-scope" not in json.dumps(result)
    assert "secret-source" not in json.dumps(result)


def test_event_summary_ignores_partial_non_utf8_tail(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    valid = json.dumps({
        "ts": 100, "event": "gather_complete", "failed_sources": [],
    }).encode()
    log.write_bytes(valid + b"\n{\"ts\": 200, \"event\": \"\xff")

    result = health.summarize_events(log, cutoff_ms=0)

    assert result["latest"]["gather"]["ts"] == 100
    assert result["malformed_lines"] == 1


def test_event_summary_ignores_over_limit_integer_and_timestamp(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    valid = json.dumps({
        "ts": 100, "event": "gather_complete", "failed_sources": [],
    })
    over_limit_integer = '{"ts": ' + ("9" * 5000) + ', "event": "gather_complete"}'
    huge_timestamp = json.dumps({"ts": 10**30, "event": "gather_complete"})
    log.write_text("\n".join((valid, over_limit_integer, huge_timestamp)) + "\n")

    result = health.summarize_events(log, cutoff_ms=0)

    assert result["latest"]["gather"]["ts"] == 100
    assert result["malformed_lines"] == 2


def test_legacy_gather_failure_is_not_reported_as_never(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    _write(log, [
        {"ts": 100, "event": "gather_failed", "source": "shell",
         "error_msg": "do not leak"},
    ])

    result = health.summarize_events(log, cutoff_ms=0)

    assert result["latest"]["gather"] == {
        "ts": 100,
        "legacy_failure": True,
        "failed_sources": ["shell"],
        "rows": {"claude": None, "codex": None, "shell": None},
    }
    assert "do not leak" not in json.dumps(result)


def test_current_statuses_come_from_db_even_without_a_status_event(tmp_path: Path):
    db = tmp_path / "undrudge.sqlite"
    conn = store.init(db)
    try:
        result = recommend.write(
            conn,
            recommend.Recommendation(title="A", body_markdown="b", signature="sig"),
            recs_dir=tmp_path / "recs",
        )
        recommend.set_status(conn, result.fingerprint, "implemented")
        created_at = conn.execute(
            "SELECT created_at FROM recommendations WHERE id=?", (result.fingerprint,)
        ).fetchone()[0]
    finally:
        conn.close()

    counts = health.current_status_counts(db, created_since_ms=created_at - 1)

    assert counts["implemented"] == 1
    assert counts["logged"] == 0


def test_health_json_is_aggregate_and_does_not_leak_raw_payloads(
    monkeypatch, tmp_path, capsys
):
    cfg = _cfg(monkeypatch, tmp_path)
    now = 2_000_000_000_000
    monkeypatch.setattr(cli.store, "now_ms", lambda: now)
    _write(cfg.paths.events_log, [
        {"ts": now - 1000, "event": "gather_failed", "source": "shell",
         "error_msg": "private failure detail"},
        {"ts": now - 900, "event": "gather_complete", "failed_sources": ["shell"]},
        {"ts": now - 800, "event": "rec_written", "path": "/private/path"},
    ])

    assert cli.main(["health", "--since", "24h", "--json"]) == 0

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["since"] == "24h"
    assert payload["latest"]["gather"]["failed_sources"] == ["shell"]
    assert payload["window"]["recommendations_written"] == 1
    assert payload["window"]["failures"] == {"gather_failed": 1}
    assert "private failure detail" not in output
    assert "/private/path" not in output


def test_health_human_output_distinguishes_outcomes_from_environment(
    monkeypatch, tmp_path, capsys
):
    cfg = _cfg(monkeypatch, tmp_path)
    now = 2_000_000_000_000
    monkeypatch.setattr(cli.store, "now_ms", lambda: now)
    _write(cfg.paths.events_log, [
        {"ts": now - 3000, "event": "gather_complete", "failed_sources": [],
         "claude_rows": 1, "codex_rows": 2, "shell_rows": 3},
        {"ts": now - 2000, "event": "analyze_complete", "scope": "weekly",
         "parsed": 4, "written": 2, "skipped": 2},
    ])

    assert cli.main(["health"]) == 0

    output = capsys.readouterr().out
    assert "gather:" in output and "clean" in output
    assert "claude 1, codex 2, shell 3" in output
    assert "weekly" in output and "4 parsed, 2 written, 2 duplicate" in output
    assert "recorded failures: none" in output


def test_health_missing_log_is_a_clear_error(monkeypatch, tmp_path, capsys):
    _cfg(monkeypatch, tmp_path, with_db=False)

    assert cli.main(["health"]) == 1

    assert "events log not found" in capsys.readouterr().err
