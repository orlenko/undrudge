"""Retention pruning.

The DB is a rebuildable cache, so pruning is allowed to be blunt — but it must
never break the two things that make re-ingest safe (cursors and row identity),
and it must never touch recommendations, which are the system's actual output.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from undrudge import cli, prune, recommend, store
from undrudge import config as cfg_mod

DAY_MS = 86_400_000


def _seed(conn: sqlite3.Connection, *, now_ms: int) -> None:
    """Two sessions: one last seen 90 days ago, one active today."""
    old_ts = now_ms - 90 * DAY_MS
    new_ts = now_ms - 1 * DAY_MS

    conn.execute(
        "INSERT INTO sessions(id, source, project, started_at, last_seen) "
        "VALUES ('old-ses', 'claude', '/p', ?, ?)",
        (old_ts, old_ts),
    )
    conn.execute(
        "INSERT INTO sessions(id, source, project, started_at, last_seen) "
        "VALUES ('new-ses', 'claude', '/p', ?, ?)",
        (new_ts, new_ts),
    )
    for i in range(5):
        conn.execute(
            "INSERT INTO messages(id, session_id, seq, ts, role, text) "
            "VALUES (?, 'old-ses', ?, ?, 'user', ?)",
            (f"old-{i}", i, old_ts, f"ancient message {i}"),
        )
    for i in range(3):
        conn.execute(
            "INSERT INTO messages(id, session_id, seq, ts, role, text) "
            "VALUES (?, 'new-ses', ?, ?, 'user', ?)",
            (f"new-{i}", i, new_ts, f"recent message {i}"),
        )
    for i in range(4):
        conn.execute(
            "INSERT INTO commands(source, external_id, ts, command) "
            "VALUES ('atuin', ?, ?, ?)",
            (f"old-cmd-{i}", old_ts, f"ancient-command-{i}"),
        )
    conn.execute(
        "INSERT INTO commands(source, external_id, ts, command) "
        "VALUES ('atuin', 'new-cmd', ?, 'recent-command')",
        (new_ts,),
    )
    conn.execute(
        "INSERT INTO cursors(source, position, updated_at) VALUES (?, ?, ?)",
        ("claude:/p/old-ses.jsonl", json.dumps({"offset": 4096}), old_ts),
    )


def _counts(conn: sqlite3.Connection) -> tuple[int, int, int]:
    return (
        conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
    )


def test_prune_deletes_only_aged_rows(db: sqlite3.Connection):
    now = store.now_ms()
    _seed(db, now_ms=now)

    stats = prune.run(db, cutoff_ms=prune.cutoff_for_days(30, now_ms=now))

    assert (stats.messages, stats.commands, stats.sessions) == (5, 4, 1)
    assert not stats.truncated
    assert _counts(db) == (3, 1, 1)
    # The surviving session is the active one; the emptied old one is gone.
    assert [r[0] for r in db.execute("SELECT id FROM sessions")] == ["new-ses"]


def test_prune_dry_run_changes_nothing(db: sqlite3.Connection):
    now = store.now_ms()
    _seed(db, now_ms=now)
    before = _counts(db)

    stats = prune.run(db, cutoff_ms=prune.cutoff_for_days(30, now_ms=now), dry_run=True)

    assert (stats.messages, stats.commands, stats.sessions) == (5, 4, 1)
    assert _counts(db) == before


def test_prune_stops_at_max_rows_and_flags_the_backlog(db: sqlite3.Connection):
    now = store.now_ms()
    _seed(db, now_ms=now)

    stats = prune.run(
        db, cutoff_ms=prune.cutoff_for_days(30, now_ms=now), max_rows=2, batch=1
    )

    assert stats.rows == 2
    assert stats.truncated
    # A capped run must not orphan-delete a session whose aged messages remain,
    # or the foreign key would blow up mid-batch.
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2

    # The rest drains on the next run.
    again = prune.run(db, cutoff_ms=prune.cutoff_for_days(30, now_ms=now))
    assert not again.truncated
    assert _counts(db) == (3, 1, 1)


def test_prune_keeps_cursors_so_gather_never_re_ingests(db: sqlite3.Connection):
    now = store.now_ms()
    _seed(db, now_ms=now)

    prune.run(db, cutoff_ms=prune.cutoff_for_days(30, now_ms=now))

    row = db.execute(
        "SELECT position FROM cursors WHERE source = ?", ("claude:/p/old-ses.jsonl",)
    ).fetchone()
    assert row is not None
    assert json.loads(row["position"]) == {"offset": 4096}


def test_prune_never_touches_recommendations(db: sqlite3.Connection, tmp_path: Path):
    now = store.now_ms()
    _seed(db, now_ms=now)
    res = recommend.write(
        db,
        recommend.Recommendation(title="Old rec", body_markdown="b", signature="sig <n>"),
        recs_dir=tmp_path / "recs",
    )
    db.execute(
        "UPDATE recommendations SET created_at = ?, updated_at = ? WHERE id = ?",
        (now - 200 * DAY_MS, now - 200 * DAY_MS, res.fingerprint),
    )

    prune.run(db, cutoff_ms=prune.cutoff_for_days(30, now_ms=now))

    assert db.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 1


def test_prune_drops_pruned_text_from_the_fts_index(db: sqlite3.Connection):
    now = store.now_ms()
    _seed(db, now_ms=now)

    def fts(table: str, needle: str) -> int:
        return db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH ?", (needle,)
        ).fetchone()[0]

    assert fts("messages_fts", "ancient") == 5
    prune.run(db, cutoff_ms=prune.cutoff_for_days(30, now_ms=now))
    assert fts("messages_fts", "ancient") == 0
    assert fts("messages_fts", "recent") == 3
    # Quoted: fts5 treats a bare hyphen as an operator.
    assert fts("commands_fts", '"ancient-command-0"') == 0
    assert fts("commands_fts", '"recent-command"') == 1


def test_prune_seq_stays_monotonic_for_a_surviving_session(db: sqlite3.Connection):
    """A long-running session keeps its newest rows; the next ingest's
    ``MAX(seq) + 1`` must not walk backwards into ids it already used."""
    now = store.now_ms()
    db.execute(
        "INSERT INTO sessions(id, source, project, started_at, last_seen) "
        "VALUES ('long', 'claude', '/p', ?, ?)",
        (now - 90 * DAY_MS, now),
    )
    for seq, age_days in enumerate((90, 60, 40, 2, 1)):
        db.execute(
            "INSERT INTO messages(id, session_id, seq, ts, role, text) "
            "VALUES (?, 'long', ?, ?, 'user', 'x')",
            (f"m-{seq}", seq, now - age_days * DAY_MS),
        )

    prune.run(db, cutoff_ms=prune.cutoff_for_days(30, now_ms=now))

    next_seq = db.execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_id = 'long'"
    ).fetchone()[0]
    assert next_seq == 5
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE id = 'long'").fetchone()[0] == 1


def test_compact_leaves_a_working_db(db: sqlite3.Connection):
    now = store.now_ms()
    _seed(db, now_ms=now)
    prune.run(db, cutoff_ms=prune.cutoff_for_days(30, now_ms=now))

    prune.compact(db)

    assert _counts(db) == (3, 1, 1)
    assert db.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'recent'"
    ).fetchone()[0] == 3


# --------------------------------------------------------------------------
# Config + CLI wiring
# --------------------------------------------------------------------------


def test_retention_defaults_to_thirty_days_and_round_trips(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = tmp_path / "config.toml"
    path.write_text(cfg_mod.render_default_config_toml())

    cfg = cfg_mod.load(path)
    assert cfg.retention.days == 30

    path.write_text("[retention]\ndays = 7\n")
    assert cfg_mod.load(path).retention.days == 7

    path.write_text("[retention]\ndays = 0\n")
    assert cfg_mod.load(path).retention.days == 0


def _prep_cfg(monkeypatch, tmp_path: Path, *, retention_days: int = 30) -> cfg_mod.Config:
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
        retention=cfg_mod.Retention(days=retention_days),
    )
    conn = store.init(cfg.paths.db)
    _seed(conn, now_ms=store.now_ms())
    conn.close()
    monkeypatch.setattr(cfg_mod, "load", lambda path=None: cfg)
    return cfg


def test_cli_prune_dry_run_reports_without_deleting(monkeypatch, tmp_path: Path, capsys):
    cfg = _prep_cfg(monkeypatch, tmp_path)

    assert cli.main(["prune", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "would delete" in out
    conn = store.open_db(cfg.paths.db)
    try:
        assert _counts(conn) == (8, 5, 2)
    finally:
        conn.close()
    assert not cfg.paths.events_log.exists()


def test_cli_prune_deletes_and_records_an_event(monkeypatch, tmp_path: Path, capsys):
    cfg = _prep_cfg(monkeypatch, tmp_path)

    assert cli.main(["prune", "--days", "30"]) == 0

    conn = store.open_db(cfg.paths.db)
    try:
        assert _counts(conn) == (3, 1, 1)
    finally:
        conn.close()
    events = [json.loads(x) for x in cfg.paths.events_log.read_text().splitlines()]
    assert [e["event"] for e in events] == ["prune_complete"]
    assert events[0]["messages"] == 5
    assert events[0]["retention_days"] == 30


def test_cli_prune_refuses_when_retention_is_off(monkeypatch, tmp_path: Path, capsys):
    _prep_cfg(monkeypatch, tmp_path, retention_days=0)

    assert cli.main(["prune"]) == 2
    assert "retention is disabled" in capsys.readouterr().err
