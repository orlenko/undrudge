"""The browse picker: row rendering, contextual preview, and the actions the
fzf key bindings call back into.

fzf itself isn't exercised (there's no terminal in CI) — everything it invokes
is a plain subcommand, and those are what's tested here, through ``cli.main``
where the argparse wiring and events.jsonl side effects matter.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from undrudge import browse, cli, recommend, store
from undrudge import config as cfg_mod


def _prep_cfg(monkeypatch, tmp_path: Path) -> cfg_mod.Config:
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


def _write_rec(conn, recs_dir: Path, title: str, signature: str, **fm) -> str:
    rec = recommend.Recommendation(
        title=title, body_markdown="Because you did it seven times.",
        signature=signature, **fm,
    )
    return recommend.write(conn, rec, recs_dir=recs_dir).fingerprint


def _seed(cfg) -> dict[str, str]:
    """One rec per interesting status."""
    conn = store.open_db(cfg.paths.db)
    try:
        store.apply_schema(conn)
        ids = {
            "logged": _write_rec(conn, cfg.paths.recs_dir, "Wrap find/grep", "find <str>"),
            "dismissed": _write_rec(conn, cfg.paths.recs_dir, "Alias cd", "cd <path>"),
            "implemented": _write_rec(conn, cfg.paths.recs_dir, "Test script", "pytest <str>"),
            "dispatched": _write_rec(conn, cfg.paths.recs_dir, "Lint hook", "ruff <path>"),
        }
        for status in ("dismissed", "implemented", "dispatched"):
            recommend.set_status(conn, ids[status], status, reason=f"because {status}")
        # Closed recs are older than the open one, so ordering isn't accidental.
        conn.execute(
            "UPDATE recommendations SET created_at = created_at - 100000 "
            "WHERE status != 'logged'"
        )
    finally:
        conn.close()
    return ids


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


def test_fetch_rows_puts_open_recs_first(monkeypatch, tmp_path: Path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    conn = store.open_db(cfg.paths.db)
    try:
        rows = browse.fetch_rows(conn, browse.Filters())
    finally:
        conn.close()

    statuses = [r["status"] for r in rows]
    assert set(statuses[:2]) == {"logged", "dispatched"}
    assert set(statuses[2:]) == {"dismissed", "implemented"}
    assert rows[0]["id"] in ids.values()


def test_render_row_keeps_the_id_in_field_one(monkeypatch, tmp_path: Path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    conn = store.open_db(cfg.paths.db)
    try:
        rows = browse.fetch_rows(conn, browse.Filters())
    finally:
        conn.close()

    for line in browse.render_rows(rows).splitlines():
        rec_id, visible = line.split("\t", 1)
        assert rec_id in ids.values()
        assert rec_id[:12] in visible
    logged_line = next(
        line for line in browse.render_rows(rows).splitlines()
        if line.startswith(ids["logged"])
    )
    assert "logged" in logged_line
    assert "Wrap find/grep" in logged_line


def test_ids_from_rows_file_reads_fzf_selection(tmp_path: Path):
    f = tmp_path / "rows"
    f.write_text("abc123\t2026-07-30  abc123  ○ logged\ndef456\t…\n\n")
    assert browse.ids_from_rows_file(str(f)) == ["abc123", "def456"]
    assert browse.ids_from_rows_file(str(tmp_path / "missing")) == []


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------


def test_preview_offers_only_the_actions_that_fit_the_status(monkeypatch, tmp_path: Path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    conn = store.open_db(cfg.paths.db)
    try:
        logged = browse.preview_markdown(conn, ids["logged"])
        dismissed = browse.preview_markdown(conn, ids["dismissed"])
        dispatched = browse.preview_markdown(conn, ids["dispatched"])
    finally:
        conn.close()

    assert "^D dismiss" in logged and "^A implement" in logged
    assert "^L reopen" not in logged
    # Nothing to dismiss twice: a closed rec only offers the way back.
    assert "^L reopen" in dismissed
    assert "^D dismiss" not in dismissed
    assert "because dismissed" in dismissed  # the recorded reason
    assert "^A implement" in dispatched and "^L back to logged" in dispatched
    # Body of the rec file is rendered, not just the DB columns.
    assert "Because you did it seven times." in logged


def test_preview_survives_a_missing_body_file(monkeypatch, tmp_path: Path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    conn = store.open_db(cfg.paths.db)
    try:
        path = conn.execute(
            "SELECT body_path FROM recommendations WHERE id = ?", (ids["logged"],)
        ).fetchone()["body_path"]
        Path(path).unlink()
        out = browse.preview_markdown(conn, ids["logged"])
        missing = browse.preview_markdown(conn, "0" * 64)
    finally:
        conn.close()

    assert "body file missing" in out
    assert "Wrap find/grep" in out
    assert "no recommendation" in missing


def test_preview_trail_mode_shows_the_audit_history(monkeypatch, tmp_path: Path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    cfg.paths.events_log.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.events_log.write_text(
        json.dumps({"ts": store.now_ms(), "event": "rec_written",
                    "id": ids["logged"], "path": "/x.md"}) + "\n"
        + json.dumps({"ts": store.now_ms(), "event": "rec_dismissed",
                      "id": ids["logged"][:12], "from_status": "logged",
                      "reason": "no daemons"}) + "\n"
        + json.dumps({"ts": store.now_ms(), "event": "rec_written",
                      "id": ids["dismissed"]}) + "\n"
    )
    conn = store.open_db(cfg.paths.db)
    try:
        out = browse.preview_markdown(
            conn, ids["logged"], mode="trail", events_log=cfg.paths.events_log
        )
    finally:
        conn.close()

    assert "rec_written" in out and "rec_dismissed" in out
    assert "no daemons" in out
    assert "2 event(s)" in out  # the third belongs to another rec


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


def test_apply_action_flips_status_and_records_an_event(monkeypatch, tmp_path: Path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    conn = store.open_db(cfg.paths.db)
    try:
        lines = browse.apply_action(
            conn, cfg, [ids["logged"]], "dismiss", reason="we don't want a daemon"
        )
        status = conn.execute(
            "SELECT status, reason FROM recommendations WHERE id = ?", (ids["logged"],)
        ).fetchone()
    finally:
        conn.close()

    assert lines == [f"{ids['logged'][:12]}: logged → dismissed"]
    assert status["status"] == "dismissed"
    assert status["reason"] == "we don't want a daemon"

    events = [json.loads(x) for x in cfg.paths.events_log.read_text().splitlines()]
    assert events[-1]["event"] == "rec_dismissed"
    assert events[-1]["reason"] == "we don't want a daemon"
    assert events[-1]["via"] == "browse"
    # The reason lands in the frontmatter too, so the file and DB agree.
    fm, _ = recommend.parse_rec_file(events[-1]["body_path"])
    assert fm["status"] == "dismissed"
    assert fm["reason"] == "we don't want a daemon"


def test_apply_action_skips_recs_already_in_that_status(monkeypatch, tmp_path: Path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    conn = store.open_db(cfg.paths.db)
    try:
        before = conn.execute(
            "SELECT updated_at FROM recommendations WHERE id = ?", (ids["dismissed"],)
        ).fetchone()["updated_at"]
        lines = browse.apply_action(conn, cfg, [ids["dismissed"], "nope"], "dismiss")
        after = conn.execute(
            "SELECT updated_at FROM recommendations WHERE id = ?", (ids["dismissed"],)
        ).fetchone()["updated_at"]
    finally:
        conn.close()

    assert lines == [f"{ids['dismissed'][:12]}: already dismissed", "nope: not found"]
    assert after == before
    assert not cfg.paths.events_log.exists()


def test_prompt_reason_cancels_on_interrupt(monkeypatch, capsys):
    def boom(_prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", boom)
    assert browse.prompt_reason("dismiss", ["A"]) == (False, None)

    monkeypatch.setattr("builtins.input", lambda _p: "   ")
    assert browse.prompt_reason("dismiss", ["A"]) == (True, None)

    monkeypatch.setattr("builtins.input", lambda _p: " stale ")
    assert browse.prompt_reason("dismiss", ["A"]) == (True, "stale")


# --------------------------------------------------------------------------
# The internal subcommands fzf calls
# --------------------------------------------------------------------------


def test_browse_list_subcommand_emits_picker_rows(monkeypatch, tmp_path: Path, capsys):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)

    assert cli.main(["__browse-list"]) == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 4
    assert out.splitlines()[0].split("\t", 1)[0] in ids.values()

    assert cli.main(["__browse-list", "--status", "logged"]) == 0
    only = capsys.readouterr().out.splitlines()
    assert len(only) == 1 and only[0].startswith(ids["logged"])


def test_browse_act_subcommand_flips_the_selection(monkeypatch, tmp_path: Path, capsys):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    rows = tmp_path / "rows"
    rows.write_text(f"{ids['logged']}\tvisible column\n")
    monkeypatch.setattr("builtins.input", lambda _p: "already have a helper")

    assert cli.main(["__browse-act", "--action", "dismiss", "--rows", str(rows)]) == 0

    conn = store.open_db(cfg.paths.db)
    try:
        row = conn.execute(
            "SELECT status, reason FROM recommendations WHERE id = ?", (ids["logged"],)
        ).fetchone()
    finally:
        conn.close()
    assert (row["status"], row["reason"]) == ("dismissed", "already have a helper")


def test_browse_act_subcommand_honours_a_cancelled_prompt(monkeypatch, tmp_path: Path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    rows = tmp_path / "rows"
    rows.write_text(f"{ids['logged']}\tvisible column\n")

    def boom(_prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", boom)
    assert cli.main(["__browse-act", "--action", "dismiss", "--rows", str(rows)]) == 0

    conn = store.open_db(cfg.paths.db)
    try:
        status = conn.execute(
            "SELECT status FROM recommendations WHERE id = ?", (ids["logged"],)
        ).fetchone()["status"]
    finally:
        conn.close()
    assert status == "logged"
    assert not cfg.paths.events_log.exists()


def test_browse_implement_needs_no_prompt(monkeypatch, tmp_path: Path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    rows = tmp_path / "rows"
    rows.write_text(f"{ids['logged']}\tvisible\n{ids['dispatched']}\tvisible\n")

    def boom(_prompt):  # implement must never reach for the terminal
        raise AssertionError("implement should not prompt")

    monkeypatch.setattr("builtins.input", boom)
    assert cli.main(["__browse-act", "--action", "implement", "--rows", str(rows)]) == 0

    conn = store.open_db(cfg.paths.db)
    try:
        statuses = {
            r["id"]: r["status"]
            for r in conn.execute("SELECT id, status FROM recommendations")
        }
    finally:
        conn.close()
    assert statuses[ids["logged"]] == "implemented"
    assert statuses[ids["dispatched"]] == "implemented"


def test_browse_flip_toggles_the_preview_mode(tmp_path: Path):
    flag = tmp_path / "flag"
    flag.write_text("rec")
    assert cli.main(["__browse-flip", "--flag", str(flag)]) == 0
    assert flag.read_text() == "trail"
    assert cli.main(["__browse-flip", "--flag", str(flag)]) == 0
    assert flag.read_text() == "rec"
    # A vanished flag file must not take the picker down with it.
    gone = tmp_path / "gone"
    assert cli.main(["__browse-flip", "--flag", str(gone)]) == 0
    assert gone.read_text() == "trail"


def test_browse_preview_subcommand_follows_the_flag(monkeypatch, tmp_path: Path, capsys):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    flag = tmp_path / "flag"
    flag.write_text("trail")

    assert cli.main(
        ["__browse-preview", "--id", ids["logged"], "--flag", str(flag)]
    ) == 0
    assert "trail —" in capsys.readouterr().out

    flag.write_text("rec")
    assert cli.main(
        ["__browse-preview", "--id", ids["logged"], "--flag", str(flag)]
    ) == 0
    assert "Because you did it seven times." in capsys.readouterr().out


def test_browse_copy_writes_the_body_or_the_path(monkeypatch, tmp_path: Path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    copied: list[str] = []
    monkeypatch.setattr(browse, "copy_to_clipboard",
                        lambda text: copied.append(text) or True)

    assert cli.main(["__browse-copy", "--id", ids["logged"], "--what", "body"]) == 0
    assert cli.main(["__browse-copy", "--id", ids["logged"], "--what", "path"]) == 0

    assert "Because you did it seven times." in copied[0]
    assert not copied[0].startswith("```json")  # frontmatter stripped
    assert copied[1].endswith(".md") and Path(copied[1]).exists()


# --------------------------------------------------------------------------
# Launching
# --------------------------------------------------------------------------


def test_browse_says_so_when_fzf_is_missing(monkeypatch, tmp_path: Path, capsys):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    _seed(cfg)
    monkeypatch.setattr(browse.shutil, "which", lambda _name: None)

    assert cli.main(["browse"]) == 127
    err = capsys.readouterr().err
    assert "fzf not found" in err
    assert "undrudge list" in err  # points at the non-interactive path


def test_browse_with_no_recs_never_launches_fzf(monkeypatch, tmp_path: Path, capsys):
    _prep_cfg(monkeypatch, tmp_path)
    monkeypatch.setattr(browse.shutil, "which", lambda name: f"/usr/bin/{name}")

    def no_spawn(*_a, **_kw):
        raise AssertionError("fzf should not be launched with an empty list")

    monkeypatch.setattr(browse.subprocess, "run", no_spawn)
    assert cli.main(["browse"]) == 0
    assert "(no recommendations)" in capsys.readouterr().out


def test_picker_command_binds_the_documented_keys(monkeypatch, tmp_path: Path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    _seed(cfg)
    monkeypatch.setattr(browse.shutil, "which", lambda name: f"/usr/bin/{name}")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input", "")

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(browse.subprocess, "run", fake_run)
    conn = store.open_db(cfg.paths.db)
    try:
        assert browse.run_picker(conn, cfg, browse.Filters()) == 0
    finally:
        conn.close()

    argv = captured["cmd"]
    joined = " ".join(argv)
    assert argv[0] == "fzf"
    for key in ("ctrl-d:", "ctrl-a:", "ctrl-x:", "ctrl-l:", "ctrl-t:", "ctrl-h:",
                "ctrl-y:", "ctrl-p:", "enter:", "ctrl-r:"):
        assert key in joined
    # Every callback re-enters *this* interpreter, never a PATH lookup.
    assert "__browse-list" in joined and "__browse-act" in joined
    assert joined.count("undrudge") >= 1
    assert len(captured["input"].splitlines()) == 4


@pytest.mark.skipif(shutil.which("fzf") is None, reason="fzf not installed")
def test_real_fzf_accepts_every_binding(monkeypatch, tmp_path: Path):
    """Hand the actual argv to the actual fzf.

    fzf validates `--bind` grammar and action names even in `--filter` mode, so
    this catches a typo'd action or an unbalanced paren in a key binding —
    which would otherwise only show up as a picker that refuses to start.
    """
    cfg = _prep_cfg(monkeypatch, tmp_path)
    _seed(cfg)
    captured: dict = {}
    real_run = subprocess.run  # the patch below covers the whole module

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input", "")

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(browse.subprocess, "run", fake_run)
    conn = store.open_db(cfg.paths.db)
    try:
        browse.run_picker(conn, cfg, browse.Filters())
    finally:
        conn.close()

    proc = real_run(
        [*captured["cmd"], "--filter", "zzz-no-such-rec"],
        input=captured["input"], text=True, capture_output=True, timeout=30,
    )
    assert "unknown action" not in proc.stderr
    assert "invalid" not in proc.stderr.lower()
    assert proc.returncode in (0, 1), proc.stderr


# --------------------------------------------------------------------------
# Copying a rec out to an implementing session
# --------------------------------------------------------------------------


def _seed_with_evidence(cfg) -> str:
    """A rec whose evidence_refs resolve to two directories."""
    conn = store.open_db(cfg.paths.db)
    try:
        store.apply_schema(conn)
        conn.execute(
            "INSERT INTO commands(source, external_id, ts, command, cwd) "
            "VALUES ('atuin', 'aaaa1111-2222-3333-4444-555555555555', 1, 'ls', ?)",
            ("/Users/fake/repo",),
        )
        conn.execute(
            "INSERT INTO sessions(id, source, project, started_at, last_seen) "
            "VALUES ('bbbb2222-3333-4444-5555-666666666666', 'claude', ?, 1, 1)",
            ("/Users/fake/other",),
        )
        rec_id = _write_rec(
            conn, cfg.paths.recs_dir, "Wrap find/grep", "find <str>",
            confidence="high", automation_form="slash_command",
            evidence_refs=[
                {"source": "atuin", "external_id": "aaaa1111"},
                {"source": "session", "external_id": "bbbb2222"},
                {"source": "atuin", "external_id": "no"},  # too short, ignored
            ],
        )
    finally:
        conn.close()
    return rec_id


def test_handoff_carries_the_rec_its_evidence_and_the_way_to_close_it(
    monkeypatch, tmp_path: Path
):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    rec_id = _seed_with_evidence(cfg)
    conn = store.open_db(cfg.paths.db)
    try:
        rec = recommend.find_by_id_prefix(conn, rec_id)[0]
        out = recommend.render_handoff(conn, rec)
    finally:
        conn.close()

    assert rec_id[:12] in out
    assert "Wrap find/grep" in out
    assert "Because you did it seven times." in out       # the rec, verbatim
    assert "confidence high" in out and "form slash_command" in out
    assert "/Users/fake/repo" in out and "/Users/fake/other" in out
    # The closing commands carry the id, so the session can flip the status.
    assert f"undrudge implement {rec_id[:12]}" in out
    assert f"undrudge dismiss   {rec_id[:12]}" in out


def test_payload_for_covers_each_kind(monkeypatch, tmp_path: Path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    conn = store.open_db(cfg.paths.db)
    try:
        rec = recommend.find_by_id_prefix(conn, ids["logged"])[0]
        handoff = browse.payload_for(conn, rec, "handoff")
        body = browse.payload_for(conn, rec, "body")
        path = browse.payload_for(conn, rec, "path")
        short = browse.payload_for(conn, rec, "id")
    finally:
        conn.close()

    assert handoff.startswith("# undrudge recommendation")
    assert body.startswith("# Wrap find/grep")
    assert not body.startswith("```json")
    assert Path(path).exists()
    assert short == ids["logged"][:12]


def test_copy_takes_a_short_prefix_and_writes_the_clipboard(monkeypatch, tmp_path: Path, capsys):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    copied: list[str] = []
    monkeypatch.setattr(browse, "copy_to_clipboard",
                        lambda text: copied.append(text) or True)

    assert cli.main(["copy", ids["logged"][:4]]) == 0

    assert copied[0].startswith("# undrudge recommendation")
    out = capsys.readouterr().out
    assert f"copied {ids['logged'][:12]}" in out
    assert "Wrap find/grep" in out


def test_copy_reports_an_ambiguous_or_missing_prefix(monkeypatch, tmp_path: Path, capsys):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    _seed(cfg)
    conn = store.open_db(cfg.paths.db)
    try:
        # Two recs whose ids share a prefix, so the ambiguity is real.
        for suffix in ("a", "b"):
            conn.execute(
                "INSERT INTO recommendations(id, scope, title, signature, "
                "body_path, evidence, status, created_at, updated_at) "
                "VALUES (?, 'daily', ?, 's', '/x.md', '[]', 'logged', 1, 1)",
                (f"dead{suffix}" + "0" * 59, f"Rec {suffix}"),
            )
    finally:
        conn.close()

    assert cli.main(["copy", "dead"]) == 1
    assert "matches 2 recs" in capsys.readouterr().err
    assert cli.main(["copy", "zzzz"]) == 1
    assert "no recommendation matched" in capsys.readouterr().err


def test_copy_prints_when_asked_or_when_no_clipboard_exists(monkeypatch, tmp_path: Path, capsys):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)

    assert cli.main(["copy", ids["logged"][:6], "--print"]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("# undrudge recommendation")

    # No pbcopy/xclip/wl-copy anywhere: print rather than swallow it.
    monkeypatch.setattr(browse, "copy_to_clipboard", lambda _text: False)
    assert cli.main(["copy", ids["logged"][:6]]) == 0
    captured = capsys.readouterr()
    assert "no clipboard tool found" in captured.err
    assert captured.out.startswith("# undrudge recommendation")


def test_copy_without_an_id_uses_the_picker(monkeypatch, tmp_path: Path, capsys):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    copied: list[str] = []
    monkeypatch.setattr(browse.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(browse, "pick_one", lambda _conn, _filters: ids["dismissed"])
    monkeypatch.setattr(browse, "copy_to_clipboard",
                        lambda text: copied.append(text) or True)

    assert cli.main(["copy"]) == 0
    assert ids["dismissed"][:12] in copied[0]

    # Quitting the picker copies nothing and isn't an error.
    copied.clear()
    monkeypatch.setattr(browse, "pick_one", lambda _conn, _filters: None)
    assert cli.main(["copy"]) == 0
    assert copied == []


def test_copy_without_an_id_or_fzf_explains_itself(monkeypatch, tmp_path: Path, capsys):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    _seed(cfg)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    assert cli.main(["copy"]) == 2
    err = capsys.readouterr().err
    assert "fzf isn't installed" in err
    assert "undrudge copy 7f3a" in err  # shows the prefix form instead


def test_pick_one_returns_the_chosen_id(monkeypatch, tmp_path: Path):
    cfg = _prep_cfg(monkeypatch, tmp_path)
    ids = _seed(cfg)
    monkeypatch.setattr(browse.shutil, "which", lambda name: f"/usr/bin/{name}")

    class R:
        returncode = 0
        stdout = ""

    def fake_run(cmd, **kwargs):
        R.stdout = kwargs["input"].splitlines()[1] + "\n"  # "user picks" row 2
        R.cmd = cmd
        return R

    monkeypatch.setattr(browse.subprocess, "run", fake_run)
    conn = store.open_db(cfg.paths.db)
    try:
        picked = browse.pick_one(conn, browse.Filters())
    finally:
        conn.close()

    assert picked in ids.values()
    assert "--multi" not in R.cmd  # one rec, not a selection


def test_picker_falls_back_to_cat_without_glow(monkeypatch):
    monkeypatch.setattr(browse.shutil, "which", lambda name: None if name == "glow" else "/bin/x")
    assert browse._renderer("80") == "cat"
    monkeypatch.setattr(browse.shutil, "which", lambda _name: "/bin/glow")
    assert "glow" in browse._renderer("80")
