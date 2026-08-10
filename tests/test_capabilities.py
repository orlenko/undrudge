"""Capability inventory: schema, parsers, fetch shape, backfill, gap.

Everything here runs against synthetic text and file:// fixtures. The
autouse conftest fixture stubs binary discovery to "absent"; tests that
exercise ``refresh`` re-patch the pieces they need explicitly. No test
touches a real binary or the network.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from pathlib import Path

import pytest

from undrudge import analyze, capabilities, config, store


def _mk_cfg(tmp_path: Path) -> config.Config:
    return config.Config(
        paths=config.Paths(
            db=tmp_path / "u.sqlite",
            recs_dir=tmp_path / "recs",
            digests_dir=tmp_path / "digests",
            logs_dir=tmp_path / "logs",
            events_log=tmp_path / "events.jsonl",
        ),
        claude=config.Claude(projects_root=tmp_path / "claude" / "projects"),
        atuin=config.Atuin(db=tmp_path / "atuin.db"),
    )


def _row(provider="claude", kind="tool", name="SendMessage", **kw):
    return capabilities.Row(provider=provider, kind=kind, name=name, **kw)


# --------------------------------------------------------------------------
# Schema: fresh and existing DBs (AGENTS.md three-part rule)
# --------------------------------------------------------------------------


def test_fresh_db_has_capabilities_table(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(capabilities)")}
    assert {"id", "provider", "kind", "name", "description", "source",
            "version", "gate", "first_seen", "last_seen", "retired_at",
            "probe_misses"} <= cols


def test_existing_db_gains_capabilities_table(tmp_path):
    """A DB created before the table existed must gain it on apply_schema."""
    p = tmp_path / "old.sqlite"
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE cursors (
          source TEXT PRIMARY KEY, position TEXT NOT NULL,
          updated_at INTEGER NOT NULL
        );
        INSERT INTO cursors VALUES ('analyze:daily', '123', 1);
        """
    )
    conn.commit()
    conn.close()

    conn = store.open_db(p)
    store.apply_schema(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "capabilities" in tables
    # Pre-existing rows undisturbed.
    row = conn.execute(
        "SELECT position FROM cursors WHERE source='analyze:daily'"
    ).fetchone()
    assert row[0] == "123"
    conn.close()


# --------------------------------------------------------------------------
# Help parsing
# --------------------------------------------------------------------------


_COMMANDER_HELP = """\
Usage: claude [options] [command] [prompt]

Claude Code - starts an interactive session by default.

Arguments:
  prompt                          Your prompt

Options:
  -d, --debug [filter]            Enable debug mode
  --betas <betas...>              Beta headers to include in API requests
  --autocompact                   Enable automatic conversation compaction
                                  with a wrapped continuation line here
  -v, --version                   Output the version number
  -h, --help                      Display help for command

Commands:
  mcp                             Configure and manage MCP servers
  plugin [options]                Manage Claude Code plugins
  ultrareview [options] [target]  Run a multi-agent review
  help [command]                  display help for command
"""


def test_parse_help_flags_and_subcommands():
    rows = capabilities.parse_help_text("claude", _COMMANDER_HELP, version="1.0.0")
    by_kind: dict[str, dict[str, str | None]] = {"flag": {}, "subcommand": {}}
    for r in rows:
        by_kind[r.kind][r.name] = r.description

    assert by_kind["flag"]["--betas"] == "Beta headers to include in API requests"
    assert by_kind["flag"]["--debug"] == "Enable debug mode"
    assert "--autocompact" in by_kind["flag"]
    # --help/--version are surface noise, and the wrapped continuation line
    # must not be parsed as anything.
    assert "--help" not in by_kind["flag"]
    assert "--version" not in by_kind["flag"]
    assert "with" not in by_kind["subcommand"]

    assert by_kind["subcommand"]["mcp"] == "Configure and manage MCP servers"
    assert by_kind["subcommand"]["plugin"] == "Manage Claude Code plugins"
    assert "help" not in by_kind["subcommand"]


def test_parse_help_subcommand_prefix():
    text = "Options:\n  --transport <t>    Transport to use\n"
    rows = capabilities.parse_help_text("claude", text, version="1.0.0", prefix="mcp")
    assert rows[0].name == "mcp --transport"
    assert rows[0].kind == "flag"


# --------------------------------------------------------------------------
# Settings env gates — names only
# --------------------------------------------------------------------------


def test_settings_env_names_reads_names_never_values(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "env": {
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
            "SOME_SECRET_TOKEN": "hunter2-do-not-store",
        },
        "model": "opus",
    }))
    names = capabilities.settings_env_names(settings)
    assert names == {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "SOME_SECRET_TOKEN"}


def test_settings_env_names_missing_or_bad_file(tmp_path):
    assert capabilities.settings_env_names(tmp_path / "nope.json") == set()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert capabilities.settings_env_names(bad) == set()


# --------------------------------------------------------------------------
# Changelog parse + backfill selection
# --------------------------------------------------------------------------


def _changelog(versions: list[str], body_char="x", body_len=50) -> str:
    return "\n".join(
        f"## {v}\n\n- {body_char * body_len} ({v})\n" for v in versions
    )


def test_parse_changelog_entries_in_file_order():
    text = _changelog(["2.1.226", "2.1.225", "2.1.32"])
    entries = capabilities.parse_changelog(text)
    assert [v for v, _ in entries] == ["2.1.226", "2.1.225", "2.1.32"]
    assert "2.1.32" in entries[2][1]


def test_parse_changelog_without_headers_is_one_content_keyed_entry():
    entries = capabilities.parse_changelog("just some release prose")
    assert len(entries) == 1
    assert entries[0][1] == "just some release prose"
    assert entries[0][0].startswith("unversioned-")
    # Content-keyed: an edited headerless file reads as a *new* entry
    # instead of freezing behind a stable name.
    edited = capabilities.parse_changelog("now with more prose")
    assert edited[0][0] != entries[0][0]


def test_select_entries_first_run_starts_at_top_and_respects_budget():
    entries = capabilities.parse_changelog(_changelog(["0.5", "0.4", "0.3", "0.2", "0.1"],
                                                      body_len=100))
    selected, state = capabilities.select_entries(entries, {}, budget_bytes=450)
    versions = [v for v, _ in selected]
    assert versions[0] == "0.5"
    assert len(versions) < 5          # budget stopped the walk
    assert state["hi"] == "0.5"
    assert state["lo"] == versions[-1]
    assert state["done"] is False


def test_select_entries_backfill_completes_across_runs():
    entries = capabilities.parse_changelog(_changelog(["0.5", "0.4", "0.3", "0.2", "0.1"],
                                                      body_len=100))
    state: dict = {}
    seen: list[str] = []
    for _ in range(10):
        selected, state = capabilities.select_entries(entries, state,
                                                      budget_bytes=450)
        seen += [v for v, _ in selected]
        if state.get("done"):
            break
    assert state["done"] is True
    assert set(seen) == {"0.5", "0.4", "0.3", "0.2", "0.1"}
    assert len(seen) == 5  # nothing processed twice


def test_select_entries_new_release_after_done():
    entries = capabilities.parse_changelog(_changelog(["0.5", "0.4", "0.3"]))
    _, state = capabilities.select_entries(entries, {}, budget_bytes=10_000)
    assert state["done"] is True

    grown = capabilities.parse_changelog(_changelog(["0.6", "0.5", "0.4", "0.3"]))
    selected, state = capabilities.select_entries(grown, state,
                                                  budget_bytes=10_000)
    assert [v for v, _ in selected] == ["0.6"]
    assert state["hi"] == "0.6"
    assert state["done"] is True


def test_select_entries_picks_up_entries_appended_below_after_done():
    """Oldest-first mirrors grow at the bottom; done must not freeze them."""
    entries = capabilities.parse_changelog(_changelog(["0.1", "0.2"]))
    _, state = capabilities.select_entries(entries, {}, budget_bytes=10_000)
    assert state["done"] is True

    grown = capabilities.parse_changelog(_changelog(["0.1", "0.2", "0.3"]))
    selected, state = capabilities.select_entries(grown, state,
                                                  budget_bytes=10_000)
    assert [v for v, _ in selected] == ["0.3"]
    assert state["lo"] == "0.3"
    assert state["done"] is True


def test_select_entries_resets_on_history_rewrite():
    entries = capabilities.parse_changelog(_changelog(["0.5", "0.4", "0.3"]))
    _, state = capabilities.select_entries(entries, {}, budget_bytes=10_000)
    rewritten = capabilities.parse_changelog(_changelog(["0.9", "0.8"]))
    selected, state = capabilities.select_entries(rewritten, state,
                                                  budget_bytes=10_000)
    assert [v for v, _ in selected] == ["0.9", "0.8"]
    assert state["hi"] == "0.9"


def test_notes_rows_extract_gate():
    rows = capabilities.notes_rows("claude", [
        ("2.1.32", "Added agent teams (requires setting "
                   "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1)"),
        ("2.1.33", "Fixed a bug"),
    ])
    assert rows[0].gate == "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"
    assert rows[0].kind == "note"
    assert rows[1].gate is None


# --------------------------------------------------------------------------
# Fetch: request shape and file:// mirrors
# --------------------------------------------------------------------------


def test_fetch_notes_request_carries_no_local_state(monkeypatch):
    captured: dict = {}

    class _Resp:
        def __init__(self):
            self.headers = {"ETag": 'W/"abc"'}

        def read(self, n):
            return b"## 1.0\nbody"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    resp = _Resp()

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["timeout"] = timeout
        return resp

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    url = "https://example.invalid/CHANGELOG.md"
    status, text, etag = capabilities.fetch_notes(url, timeout_s=7, etag="old")

    assert status == "ok"
    assert "body" in text
    assert etag == 'W/"abc"'
    # The request is exactly the configured URL — no query params, no
    # local state. Only a static UA and the previous fetch's validator.
    assert captured["url"] == url
    assert captured["timeout"] == 7
    header_names = {k.lower().replace("_", "-") for k in captured["headers"]}
    assert header_names <= {"user-agent", "if-none-match", "host"}
    assert captured["headers"].get("If-none-match") == "old"


def test_fetch_notes_file_url(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("## 1.2.3\n- a thing\n")
    status, text, etag = capabilities.fetch_notes(
        f.as_uri(), timeout_s=5, etag="ignored-for-file"
    )
    assert status == "ok"
    assert "1.2.3" in text
    assert etag is None


def test_fetch_notes_failure_is_silent(tmp_path):
    status, text, _ = capabilities.fetch_notes(
        (tmp_path / "missing.md").as_uri(), timeout_s=5
    )
    assert status == "failed"
    assert text is None


# --------------------------------------------------------------------------
# Upsert, revival, retirement
# --------------------------------------------------------------------------


def test_upsert_then_revive_after_retirement(db):
    n, u, names = capabilities.upsert_rows(db, [_row()], now_ms=1000)
    assert (n, u) == (1, 0)
    assert names == ["tool:SendMessage"]

    db.execute("UPDATE capabilities SET retired_at=2000, probe_misses=2")
    n, u, _ = capabilities.upsert_rows(db, [_row()], now_ms=3000)
    assert (n, u) == (0, 1)
    row = db.execute("SELECT * FROM capabilities").fetchone()
    assert row["retired_at"] is None
    assert row["probe_misses"] == 0
    assert row["first_seen"] == 1000   # survives the revival
    assert row["last_seen"] == 3000


def test_help_scrape_retires_missing_rows(db):
    capabilities.upsert_rows(db, [
        _row(kind="flag", name="--old", source="help"),
        _row(kind="flag", name="--kept", source="help"),
        _row(kind="tool", name="SendMessage", source="probe"),
    ])
    # Fix sources: upsert stores what the Row says.
    db.execute("UPDATE capabilities SET source='probe' WHERE kind='tool'")
    retired = capabilities.retire_missing_help(
        db, "claude", {("flag", "--kept")}, now_ms=99,
    )
    assert retired == 1
    gone = db.execute(
        "SELECT retired_at FROM capabilities WHERE name='--old'"
    ).fetchone()
    kept = db.execute(
        "SELECT retired_at FROM capabilities WHERE name='--kept'"
    ).fetchone()
    probe = db.execute(
        "SELECT retired_at FROM capabilities WHERE name='SendMessage'"
    ).fetchone()
    assert gone["retired_at"] == 99
    assert kept["retired_at"] is None
    assert probe["retired_at"] is None  # help retirement never touches probe rows


def test_probe_rows_retire_after_three_consecutive_misses(db):
    capabilities.upsert_rows(db, [_row(source="probe")])
    db.execute("UPDATE capabilities SET source='probe'")
    for i in range(1, 3):
        retired = capabilities.bump_probe_misses(db, "claude", set(), now_ms=i)
        assert retired == 0
    retired = capabilities.bump_probe_misses(db, "claude", set(), now_ms=3)
    assert retired == 1
    row = db.execute("SELECT * FROM capabilities").fetchone()
    assert row["retired_at"] == 3
    assert row["probe_misses"] == 3


def test_one_probe_hit_resets_the_miss_counter(db):
    capabilities.upsert_rows(db, [_row(source="probe")])
    db.execute("UPDATE capabilities SET source='probe'")
    capabilities.bump_probe_misses(db, "claude", set(), now_ms=1)
    capabilities.bump_probe_misses(db, "claude", set(), now_ms=2)
    # The capability shows up again — upsert revives the counter.
    capabilities.upsert_rows(db, [_row(source="probe")])
    retired = capabilities.bump_probe_misses(
        db, "claude", {("tool", "SendMessage")}, now_ms=3
    )
    assert retired == 0
    assert db.execute(
        "SELECT probe_misses FROM capabilities"
    ).fetchone()[0] == 0


# --------------------------------------------------------------------------
# Sanitize on ingest
# --------------------------------------------------------------------------


def test_sanitize_failure_drops_the_row(db, monkeypatch):
    from undrudge import sanitize as sanitize_mod

    def boom(text, **kw):
        raise RuntimeError("sanitizer exploded")

    monkeypatch.setattr(sanitize_mod, "redact", boom)
    out = capabilities.sanitize_rows(db, [_row()])
    assert out == []
    failures = db.execute("SELECT COUNT(*) FROM redaction_failures").fetchone()[0]
    assert failures == 1


def test_sanitize_redacts_secrets_in_descriptions(db):
    from fixtures import GITHUB_TOKEN

    out = capabilities.sanitize_rows(db, [
        _row(description=f"uses token {GITHUB_TOKEN} internally"),
    ])
    assert len(out) == 1
    assert GITHUB_TOKEN not in (out[0].description or "")


# --------------------------------------------------------------------------
# Usage inventory and the gap
# --------------------------------------------------------------------------


def _seed_usage(db):
    db.execute(
        "INSERT INTO sessions(id, source, project) VALUES ('s1','claude','/r')"
    )
    db.execute(
        """INSERT INTO messages(id, session_id, seq, ts, role, text, tool_name)
           VALUES ('m1','s1',1,1,'assistant',NULL,'Read')"""
    )
    # A *registered* slash command lands wrapped in transcript markup —
    # this is the shape real histories carry, and the one that regressed.
    db.execute(
        """INSERT INTO messages(id, session_id, seq, ts, role, text)
           VALUES ('m2','s1',2,2,'user',
                   '<command-name>/review</command-name><command-message>review</command-message>')"""
    )
    # Unregistered slash text arrives verbatim.
    db.execute(
        """INSERT INTO messages(id, session_id, seq, ts, role, text)
           VALUES ('m3','s1',3,3,'user','/adhoc-thing please')"""
    )
    db.execute(
        """INSERT INTO commands(source, external_id, ts, command)
           VALUES ('atuin','c1',3,'claude mcp list --transport http')"""
    )
    # Flag words inside a quoted prompt argument are not flag usage.
    db.execute(
        """INSERT INTO commands(source, external_id, ts, command)
           VALUES ('atuin','c2',4,'claude -p "explain what --betas does"')"""
    )


def test_gap_excludes_used_capabilities(db, tmp_path):
    cfg = _mk_cfg(tmp_path)
    _seed_usage(db)
    capabilities.upsert_rows(db, [
        _row(kind="tool", name="Read"),           # used as a tool
        _row(kind="tool", name="SendMessage"),    # never used → gap
        _row(kind="command", name="/review"),     # registered slash command
        _row(kind="command", name="/adhoc-thing"),  # bare slash text
        _row(kind="subcommand", name="mcp"),      # shell usage
        _row(kind="subcommand", name="mcp list"),   # exact nested usage
        _row(kind="subcommand", name="mcp add"),  # sibling never used → gap
        _row(kind="flag", name="mcp --transport"),
        _row(kind="flag", name="--betas"),        # only inside a quoted prompt → gap
    ])
    rows = capabilities.gap_rows(db, cfg)
    names = {g.name for g in rows}
    assert names == {"SendMessage", "--betas", "mcp add"}
    assert all(g.is_new for g in rows)


def test_gap_marks_enabled_gate_from_settings(db, tmp_path):
    cfg = _mk_cfg(tmp_path)
    settings_dir = cfg.claude.projects_root.parent
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text(json.dumps({
        "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
    }))
    capabilities.upsert_rows(db, [
        _row(kind="note", name="2.1.32",
             description="Added agent teams",
             gate="CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"),
        _row(kind="note", name="2.1.40",
             description="Other gated thing", gate="CLAUDE_CODE_OTHER_GATE"),
    ])
    rows = {g.name: g for g in capabilities.gap_rows(db, cfg)}
    assert rows["2.1.32"].gate_enabled is True
    assert rows["2.1.40"].gate_enabled is False


def test_render_gap_section_and_consumption(db, tmp_path):
    cfg = _mk_cfg(tmp_path)
    capabilities.upsert_rows(db, [_row(description="message another session")])
    section = capabilities.render_gap_section(db, cfg)
    assert "SendMessage" in section
    assert "{new_rows}" not in section
    assert "{older_rows}" not in section

    capabilities.mark_consumed(db)
    # Nothing new anymore → the section disappears entirely; the old row
    # only comes back alongside a future new one.
    assert capabilities.render_gap_section(db, cfg) == ""

    capabilities.upsert_rows(
        db, [_row(name="NewTool", description="brand new")],
        now_ms=store.now_ms() + 1,
    )
    section = capabilities.render_gap_section(db, cfg)
    assert "NewTool" in section
    assert "SendMessage" in section   # long-standing row, lower priority


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------


def test_build_prompt_injects_capability_section():
    out = analyze.build_prompt(
        "DIGEST", "RECS", scope="daily",
        capability_section="# Capability gap\nCAPS HERE",
    )
    assert "CAPS HERE" in out
    assert "{capability_gap_section}" not in out


def test_build_prompt_empty_capability_section_leaves_no_placeholder():
    out = analyze.build_prompt("DIGEST", "RECS", scope="daily")
    assert "{capability_gap_section}" not in out


# --------------------------------------------------------------------------
# refresh() orchestration — fakes only
# --------------------------------------------------------------------------


def _fake_probe(items):
    def prober(cfg, *, workdir):
        return items
    return prober


def test_refresh_end_to_end_with_fakes(db, tmp_path, monkeypatch):
    cfg = _mk_cfg(tmp_path)
    notes = tmp_path / "changelog.md"
    notes.write_text(_changelog(["1.1.0", "1.0.0"]))
    cfg.capabilities.release_notes_urls = {"claude": notes.as_uri(), "codex": ""}

    monkeypatch.setattr(
        capabilities, "binary_version",
        lambda binary: "1.1.0" if binary == "claude" else None,
    )
    monkeypatch.setattr(
        capabilities, "scrape_help",
        lambda provider, version: [
            capabilities.Row(provider, "flag", "--betas", "Beta headers",
                             "help", version),
        ],
    )
    probers = {"claude": _fake_probe([
        {"kind": "tool", "name": "SendMessage", "description": "send msg"},
    ])}

    results = capabilities.refresh(db, cfg, with_probe=True, probers=probers)
    by_provider = {r.provider: r for r in results}
    assert by_provider["codex"].skipped_reason
    r = by_provider["claude"]
    assert r.version_changed is True
    assert r.new_rows == 4          # 1 flag + 1 tool + 2 note entries
    assert not r.errors

    kinds = {row["kind"] for row in db.execute("SELECT kind FROM capabilities")}
    assert kinds == {"flag", "tool", "note"}

    state = capabilities.load_state(db, "claude")
    assert state["version"] == "1.1.0"
    assert state["notes"]["done"] is True
    assert state["probed_at"] > 0

    # Second run same version: no scrape, notes done, probe within the
    # daily window → nothing happens. The prober records calls so a broken
    # gate shows up as a recorded call, not as a swallowed exception.
    monkeypatch.setattr(
        capabilities, "scrape_help",
        lambda provider, version: pytest.fail("scrape must be version-gated"),
    )
    probe_calls: list = []

    def recording_prober(cfg, *, workdir):
        probe_calls.append(workdir)
        return []

    results = capabilities.refresh(db, cfg, with_probe=True, probers={
        "claude": recording_prober,
    })
    r = {x.provider: x for x in results}["claude"]
    assert probe_calls == []        # daily gate held
    assert r.new_rows == 0
    assert not r.errors


def test_refresh_probe_failure_is_isolated(db, tmp_path, monkeypatch):
    cfg = _mk_cfg(tmp_path)
    cfg.capabilities.release_notes_urls = {"claude": "", "codex": ""}
    monkeypatch.setattr(
        capabilities, "binary_version",
        lambda binary: "1.0.0" if binary == "claude" else None,
    )
    monkeypatch.setattr(
        capabilities, "scrape_help",
        lambda provider, version: [
            capabilities.Row(provider, "flag", "--ok", None, "help", version),
        ],
    )

    def exploding_prober(cfg, *, workdir):
        raise RuntimeError("probe blew up")

    results = capabilities.refresh(
        db, cfg, with_probe=True, probers={"claude": exploding_prober},
    )
    r = {x.provider: x for x in results}["claude"]
    assert r.new_rows == 1          # the scrape still landed
    assert any("probe" in e for e in r.errors)
    events_text = cfg.paths.events_log.read_text()
    assert "capability_probe_failed" in events_text


def test_refresh_sends_no_etag_until_backfill_done(db, tmp_path, monkeypatch):
    """A 304 mid-backfill would stall the walk forever — the conditional
    GET must only start once history has been seen."""
    cfg = _mk_cfg(tmp_path)
    notes = tmp_path / "changelog.md"
    notes.write_text(_changelog(["0.3", "0.2", "0.1"], body_len=200))
    cfg.capabilities.release_notes_urls = {"claude": notes.as_uri(), "codex": ""}
    cfg.capabilities.max_notes_bytes = 400   # one entry per run → 3 runs
    monkeypatch.setattr(capabilities, "binary_version", lambda b: "0.3")
    monkeypatch.setattr(capabilities, "scrape_help", lambda p, version: [])
    monkeypatch.setattr(capabilities, "_PROBERS", {})

    etags_seen: list = []
    real_fetch = capabilities.fetch_notes

    def spying_fetch(url, *, timeout_s, etag=None):
        etags_seen.append(etag)
        status, text, _ = real_fetch(url, timeout_s=timeout_s, etag=etag)
        return status, text, "server-etag"

    monkeypatch.setattr(capabilities, "fetch_notes", spying_fetch)

    state: dict = {}
    for _ in range(4):
        capabilities.refresh(db, cfg, with_probe=False)
        state = capabilities.load_state(db, "claude")
        if (state.get("notes") or {}).get("done"):
            break
    assert state["notes"]["done"] is True
    assert all(e is None for e in etags_seen)     # never conditional mid-backfill

    # Backfill finished (and version unchanged) → no further fetch at all
    # until the next version bump, where the stored etag rides along.
    capabilities.refresh(db, cfg, with_probe=False)
    count_after_done = len(etags_seen)
    monkeypatch.setattr(capabilities, "binary_version", lambda b: "0.4")
    capabilities.refresh(db, cfg, with_probe=False)
    assert len(etags_seen) == count_after_done + 1
    assert etags_seen[-1] == "server-etag"


def test_adopt_capability_form_survives_normalize():
    from undrudge import recommend

    rec = recommend.Recommendation(
        title="Adopt SendMessage",
        body_markdown="body",
        signature="adopt:claude:tool:SendMessage",
        automation_form="adopt_capability",
    )
    assert recommend.normalize(rec).automation_form == "adopt_capability"


def test_refresh_disabled_fetch_stays_local(db, tmp_path, monkeypatch):
    cfg = _mk_cfg(tmp_path)
    cfg.capabilities.fetch_release_notes = False

    def no_fetch(*a, **kw):
        pytest.fail("fetch_release_notes=false must mean no fetch call")

    monkeypatch.setattr(capabilities, "fetch_notes", no_fetch)
    monkeypatch.setattr(capabilities, "binary_version", lambda b: "1.0.0")
    monkeypatch.setattr(capabilities, "scrape_help", lambda p, version: [])
    capabilities.refresh(db, cfg, with_probe=False)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_config_capabilities_defaults():
    cfg = config.default_config()
    assert cfg.capabilities.enabled is True
    assert cfg.capabilities.probe is True
    assert cfg.capabilities.fetch_release_notes is True
    assert "claude" in cfg.capabilities.release_notes_urls
    assert "codex" in cfg.capabilities.release_notes_urls


def test_config_capabilities_overrides(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(
        """
[capabilities]
enabled = true
probe = false
fetch_release_notes = false
fetch_timeout_s = 3
max_notes_bytes = 1000

[capabilities.claude]
release_notes_url = "file:///mirror/claude.md"
"""
    )
    cfg = config.load(p)
    assert cfg.capabilities.probe is False
    assert cfg.capabilities.fetch_release_notes is False
    assert cfg.capabilities.fetch_timeout_s == 3
    assert cfg.capabilities.max_notes_bytes == 1000
    assert cfg.capabilities.release_notes_urls["claude"] == "file:///mirror/claude.md"
    # Unset provider keeps its default.
    assert cfg.capabilities.release_notes_urls["codex"].startswith("https://")


def test_render_default_config_includes_capabilities_block():
    text = config.render_default_config_toml()
    assert "[capabilities]" in text
    assert "fetch_release_notes" in text
