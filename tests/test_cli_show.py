"""CLI behavior for reading one recommendation by id."""

from __future__ import annotations

from pathlib import Path

from undrudge import cli, recommend, store
from undrudge import config as cfg_mod


def _seed(monkeypatch, tmp_path: Path) -> tuple[cfg_mod.Config, str, Path]:
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
    conn = store.init(cfg.paths.db)
    try:
        result = recommend.write(
            conn,
            recommend.Recommendation(
                title="Make the useful thing visible",
                body_markdown="The recommendation body.\n\n- one\n- two",
                signature="undrudge show <hex>",
            ),
            recs_dir=cfg.paths.recs_dir,
        )
    finally:
        conn.close()
    monkeypatch.setattr(cfg_mod, "load", lambda path=None: cfg)
    assert result.path is not None
    return cfg, result.fingerprint, result.path


def test_show_prints_markdown_body_by_default(monkeypatch, tmp_path, capsys):
    _, rec_id, _ = _seed(monkeypatch, tmp_path)

    assert cli.main(["show", rec_id[:12]]) == 0

    assert capsys.readouterr().out == (
        "# Make the useful thing visible\n\n"
        "The recommendation body.\n\n- one\n- two\n"
    )


def test_show_path_preserves_one_line_path_output(monkeypatch, tmp_path, capsys):
    _, rec_id, path = _seed(monkeypatch, tmp_path)

    assert cli.main(["show", rec_id[:12], "--path"]) == 0

    assert capsys.readouterr().out == f"{path}\n"


def test_show_reports_a_missing_body_file(monkeypatch, tmp_path, capsys):
    _, rec_id, path = _seed(monkeypatch, tmp_path)
    path.unlink()

    assert cli.main(["show", rec_id[:12]]) == 1

    assert "body file is missing" in capsys.readouterr().err


def test_show_reports_a_non_utf8_body_file(monkeypatch, tmp_path, capsys):
    _, rec_id, path = _seed(monkeypatch, tmp_path)
    path.write_bytes(b"# broken\n\xff")

    assert cli.main(["show", rec_id[:12]]) == 1

    assert "body file is not valid UTF-8" in capsys.readouterr().err


def test_show_reports_an_unreadable_body_file(monkeypatch, tmp_path, capsys):
    _, rec_id, path = _seed(monkeypatch, tmp_path)
    original_read_text = Path.read_text

    def deny_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic denial")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_read)

    assert cli.main(["show", rec_id[:12]]) == 1

    assert "body file could not be read" in capsys.readouterr().err
