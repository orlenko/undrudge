"""Smoke tests for ``-v``/``--verbose`` logging.

We don't assert on specific messages — those are free to change. We
assert the wiring: default = silent, ``-v`` = INFO on stderr, ``-vv`` =
DEBUG. The verbose flag must work both with `main(argv)` and via the
module loggers it configures.
"""

from __future__ import annotations

import logging

from undrudge import cli, ingest_claude


def test_default_is_silent(capsys, claude_projects, atuin_db, monkeypatch, tmp_path):
    _prep_env(monkeypatch, tmp_path, claude_projects, atuin_db)
    rc = cli.main(["gather"])
    captured = capsys.readouterr()
    assert rc == 0
    # The gather summary still prints to stdout — that's the existing
    # behavior we're preserving. Stderr should hold no log lines.
    assert "INFO" not in captured.err
    assert "DEBUG" not in captured.err


def test_v_enables_info(capsys, claude_projects, atuin_db, monkeypatch, tmp_path):
    _prep_env(monkeypatch, tmp_path, claude_projects, atuin_db)
    rc = cli.main(["-v", "gather"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "INFO" in captured.err
    assert "DEBUG" not in captured.err
    # Sanity: the ingest modules narrated something.
    assert "ingest_claude" in captured.err or "ingest_shell" in captured.err


def test_vv_enables_debug(capsys, claude_projects, atuin_db, monkeypatch, tmp_path):
    _prep_env(monkeypatch, tmp_path, claude_projects, atuin_db)
    rc = cli.main(["-vv", "gather"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "DEBUG" in captured.err


def test_logger_namespace_is_undrudge():
    # The setup function attaches the handler to the ``undrudge`` logger,
    # not the root logger, so we don't capture noise from third-party libs.
    cli._setup_logging(1)
    assert logging.getLogger("undrudge").level == logging.INFO
    assert logging.getLogger("undrudge").handlers, "expected at least one handler"
    # Module loggers inherit from the namespace logger.
    assert ingest_claude.logger.getEffectiveLevel() == logging.INFO


def _prep_env(monkeypatch, tmp_path, claude_projects, atuin_db):
    """Build a temp config that points at the test fixtures and patch
    ``config.load`` so the CLI sees it without touching ``$HOME``."""
    from undrudge import config as cfg_mod
    from undrudge import store

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
        claude=cfg_mod.Claude(projects_root=claude_projects),
        atuin=cfg_mod.Atuin(db=atuin_db),
    )
    store.init(cfg.paths.db).close()
    monkeypatch.setattr(cfg_mod, "load", lambda path=None: cfg)
