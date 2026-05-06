"""Phase 3 tests: prompt assembly, response parsing, end-to-end with a
mocked LLM. The real claude shell-out is verified manually on real data
because mocking it adds nothing; the wiring is what matters here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from undrudge import analyze, config, recommend, store

# --------------------------------------------------------------------------
# Prompt template
# --------------------------------------------------------------------------


def test_prompt_template_loads_from_package():
    tpl = analyze.load_prompt_template()
    assert "{digest}" in tpl
    assert "{recent_recs}" in tpl
    # Anti-trivia guidance must survive packaging.
    assert "cd" in tpl.lower() and "git status" in tpl.lower()


def test_build_prompt_substitutes_in_order():
    prompt = analyze.build_prompt("DIGEST_BODY", "RECS_BODY")
    assert "DIGEST_BODY" in prompt
    assert "RECS_BODY" in prompt
    assert "{digest}" not in prompt
    assert "{recent_recs}" not in prompt


# --------------------------------------------------------------------------
# JSON extraction
# --------------------------------------------------------------------------


def test_extract_json_array_direct():
    out = analyze.extract_json_array('[{"x": 1}, {"x": 2}]')
    assert out == [{"x": 1}, {"x": 2}]


def test_extract_json_array_fenced():
    text = (
        "Here are the recommendations:\n\n"
        "```json\n"
        '[{"title": "x", "signature": "y"}]\n'
        "```\n"
        "let me know if you want more."
    )
    out = analyze.extract_json_array(text)
    assert out == [{"title": "x", "signature": "y"}]


def test_extract_json_array_inline():
    text = 'I think this is the answer: [{"x": 1}] and that\'s it.'
    out = analyze.extract_json_array(text)
    assert out == [{"x": 1}]


def test_extract_json_array_empty():
    assert analyze.extract_json_array("[]") == []


def test_extract_json_array_raises_on_missing():
    with pytest.raises(ValueError):
        analyze.extract_json_array("nothing useful here")


def test_extract_json_array_skips_unbalanced_garbage():
    # Garbage `[` followed by valid array later — first attempt fails, then we move on.
    text = "[oops not json\n```json\n[{\"y\": 2}]\n```"
    out = analyze.extract_json_array(text)
    assert out == [{"y": 2}]


# --------------------------------------------------------------------------
# to_recommendations
# --------------------------------------------------------------------------


def test_to_recommendations_filters_incomplete_items():
    items = [
        {"title": "good", "signature": "x", "body_markdown": "..."},
        {"title": "no sig"},
        {"signature": "no title"},
        "not a dict",
        {"title": "", "signature": "x"},
    ]
    recs = analyze.to_recommendations(items, scope="daily")
    assert len(recs) == 1
    assert recs[0].title == "good"
    assert recs[0].scope == "daily"


def test_to_recommendations_normalizes_invalid_enums():
    items = [
        {"title": "x", "signature": "y", "automation_form": "MAGIC", "confidence": "absolute"},
    ]
    recs = analyze.to_recommendations(items, scope="weekly")
    rec = recommend.normalize(recs[0])
    assert rec.automation_form == "other"
    assert rec.confidence == "medium"


# --------------------------------------------------------------------------
# Recommendation fingerprint
# --------------------------------------------------------------------------


def test_fingerprint_is_stable_across_whitespace_and_case():
    a = recommend.Recommendation(title="x", body_markdown="b", signature="find . -name <str>")
    b = recommend.Recommendation(title="x", body_markdown="b", signature=" FIND .  -NAME  <str>  ")
    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_differs_by_scope():
    a = recommend.Recommendation(title="x", body_markdown="b", signature="s", scope="daily")
    b = recommend.Recommendation(title="x", body_markdown="b", signature="s", scope="weekly")
    assert a.fingerprint() != b.fingerprint()


@pytest.mark.parametrize("a,b", [
    ("find . -name <str>", "`find . -name <str>`"),                # backticks
    ("find . -name <str>", "find . -name *<str>*"),                # asterisks
    ("find . -name <Path>", "find . -name <path>"),                # placeholder case
    ("find . -name < path >", "find . -name <path>"),              # placeholder spacing
    ("a | b | c", "a|b|c"),                                        # pipe spacing
])
def test_canonicalize_signature_collapses_superficial_drift(a, b):
    assert recommend.canonicalize_signature(a) == recommend.canonicalize_signature(b)


def test_canonicalize_signature_keeps_meaningful_difference():
    a = recommend.canonicalize_signature("find . -name <str>")
    b = recommend.canonicalize_signature("find . -path <str>")
    assert a != b


def test_signature_similarity_high_for_rephrased_pattern():
    # The two recs in Vlad's session that should have deduped but didn't.
    a = "rm -rf <path> <path> && UV_CACHE_DIR=<str> UNDRUDGE_CONFIG=<str> XDG_DATA_HOME=<str> uv run undrudge init"
    b = "rm -rf <path> <path> && UV_CACHE_DIR=<str> UNDRUDGE_CONFIG=$<str> XDG_DATA_HOME=$<str> uv run undrudge init"
    assert recommend.signature_similarity(a, b) >= 0.85


def test_signature_similarity_low_for_unrelated_patterns():
    assert recommend.signature_similarity(
        "find . -name <str> | xargs grep <str>",
        "git rebase -i HEAD~<n>",
    ) < 0.5


# --------------------------------------------------------------------------
# Fuzzy dedupe at write time
# --------------------------------------------------------------------------


def test_write_skips_near_duplicate(tmp_path: Path):
    """Two signatures that survive canonicalization but stay >= 0.85 similar."""
    conn = store.init(tmp_path / "u.sqlite")
    first = recommend.Recommendation(
        title="Wrap repeated find/grep",
        body_markdown="...",
        signature="find . -name <str> | xargs grep <str>",
    )
    second = recommend.Recommendation(
        title="Add a slash command for the find/grep one-liner",
        body_markdown="...",
        # Adds `-type f`. Canonicalizes differently from `first`, but
        # SequenceMatcher ratio stays well above the 0.85 threshold.
        signature="find . -name <str> -type f | xargs grep <str>",
    )

    a = recommend.write(conn, first, recs_dir=tmp_path / "recs")
    b = recommend.write(conn, second, recs_dir=tmp_path / "recs")
    assert a.inserted is True
    assert b.inserted is False
    assert b.skipped_reason and "near-duplicate" in b.skipped_reason
    assert b.path == a.path
    count = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
    assert count == 1


def test_write_skips_canonical_exact_duplicate(tmp_path: Path):
    """Backticks / quoting / placeholder casing differences canonicalize away
    and hit the exact-fingerprint path (not the fuzzy one). Either skipped
    reason is acceptable — both mean 'we handled the duplicate'."""
    conn = store.init(tmp_path / "u.sqlite")
    a = recommend.write(
        conn,
        recommend.Recommendation(
            title="X", body_markdown="b",
            signature="find . -name <str> | xargs grep <str>",
        ),
        recs_dir=tmp_path / "recs",
    )
    b = recommend.write(
        conn,
        recommend.Recommendation(
            title="X-rephrased", body_markdown="b",
            signature="`find . -name <Str>` | xargs grep `<str>`",
        ),
        recs_dir=tmp_path / "recs",
    )
    assert a.inserted is True
    assert b.inserted is False
    assert b.skipped_reason and (
        "duplicate fingerprint" in b.skipped_reason
        or "near-duplicate" in b.skipped_reason
    )


def test_write_distinguishes_unrelated_patterns(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    a = recommend.write(
        conn,
        recommend.Recommendation(
            title="X", body_markdown="b",
            signature="find . -name <str> | xargs grep <str>",
        ),
        recs_dir=tmp_path / "recs",
    )
    b = recommend.write(
        conn,
        recommend.Recommendation(
            title="Y", body_markdown="b",
            signature="git rebase -i HEAD~<n>",
        ),
        recs_dir=tmp_path / "recs",
    )
    assert a.inserted is True
    assert b.inserted is True


def test_write_fuzzy_dedupe_ignores_dismissed(tmp_path: Path):
    """A fuzzy near-duplicate of a *dismissed* rec is allowed through —
    the user's dismissal applies to the original, not to all rephrasings."""
    conn = store.init(tmp_path / "u.sqlite")
    a = recommend.write(
        conn,
        recommend.Recommendation(
            title="X", body_markdown="b",
            signature="find . -name <str> | xargs grep <str>",
        ),
        recs_dir=tmp_path / "recs",
    )
    assert a.inserted is True

    conn.execute(
        "UPDATE recommendations SET status = 'dismissed' WHERE id = ?",
        (a.fingerprint,),
    )

    b = recommend.write(
        conn,
        recommend.Recommendation(
            title="Y", body_markdown="b",
            signature="find . -name <str> -type f | xargs grep <str>",
        ),
        recs_dir=tmp_path / "recs",
    )
    # Fuzzy lookup filters by status, so a similar-but-distinct rec
    # against a dismissed one lands as a fresh insert.
    assert b.inserted is True


# --------------------------------------------------------------------------
# write() — markdown + DB row + idempotence
# --------------------------------------------------------------------------


def test_write_persists_markdown_and_db(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    rec = recommend.Recommendation(
        title="Wrap repeated find/grep",
        body_markdown="Body here.",
        signature="find . -name <str> | xargs grep <str>",
        confidence="high",
        automation_form="slash_command",
        evidence=["7 invocations"],
    )

    result = recommend.write(
        conn, rec,
        recs_dir=tmp_path / "recs",
        now=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    )
    assert result.inserted is True
    assert result.path is not None
    assert result.path.exists()

    text = result.path.read_text()
    assert "Wrap repeated find/grep" in text
    assert text.startswith("```json\n")
    fm = json.loads(text.split("```\n", 2)[0].removeprefix("```json\n"))
    assert fm["id"] == result.fingerprint
    assert fm["status"] == "logged"
    assert fm["confidence"] == "high"
    assert fm["automation_form"] == "slash_command"
    assert fm["signature"] == rec.signature

    db_row = conn.execute(
        "SELECT id, status, body_path FROM recommendations WHERE id = ?",
        (result.fingerprint,),
    ).fetchone()
    assert db_row is not None
    assert db_row["status"] == "logged"
    assert db_row["body_path"] == str(result.path)


def test_write_is_idempotent_on_fingerprint(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    rec = recommend.Recommendation(
        title="x", body_markdown="b", signature="sig"
    )

    a = recommend.write(conn, rec, recs_dir=tmp_path / "recs")
    b = recommend.write(conn, rec, recs_dir=tmp_path / "recs")
    assert a.inserted is True
    assert b.inserted is False
    assert b.skipped_reason == "duplicate fingerprint"
    count = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
    assert count == 1


def test_write_skips_empty_signature(tmp_path: Path):
    conn = store.init(tmp_path / "u.sqlite")
    rec = recommend.Recommendation(title="x", body_markdown="b", signature="  ")
    result = recommend.write(conn, rec, recs_dir=tmp_path / "recs")
    assert result.inserted is False
    assert result.skipped_reason == "empty title or signature"


def test_write_fires_on_write_hook(tmp_path: Path):
    """on_write is run through /bin/sh -c with the rec path bound to $1."""
    conn = store.init(tmp_path / "u.sqlite")
    sentinel = tmp_path / "hook.touch"

    rec = recommend.Recommendation(title="x", body_markdown="b", signature="sig")
    result = recommend.write(
        conn,
        rec,
        recs_dir=tmp_path / "recs",
        on_write=f'echo "$1" > {sentinel}',
    )
    assert result.inserted is True
    assert sentinel.exists()
    assert sentinel.read_text().strip() == str(result.path)


# --------------------------------------------------------------------------
# End-to-end run() with mocked invoker
# --------------------------------------------------------------------------


def _mk_cfg(tmp_path: Path) -> config.Config:
    return config.Config(
        paths=config.Paths(
            db=tmp_path / "u.sqlite",
            recs_dir=tmp_path / "recs",
            digests_dir=tmp_path / "digests",
            logs_dir=tmp_path / "logs",
        ),
        claude=config.Claude(projects_root=tmp_path / "claude"),
        atuin=config.Atuin(db=tmp_path / "atuin.db"),
    )


def test_run_writes_recommendations_from_mocked_response(tmp_path: Path):
    cfg = _mk_cfg(tmp_path)
    conn = store.init(cfg.paths.db)

    response_payload = json.dumps([
        {
            "title": "Wrap find/xargs grep",
            "body_markdown": "the body",
            "signature": "find . -name <str> | xargs grep <str>",
            "automation_form": "slash_command",
            "confidence": "high",
            "evidence": ["3 invocations across 2 sessions"],
            "rationale": "structure repeats",
        }
    ])

    result = analyze.run(
        conn, cfg, invoker=lambda _prompt: response_payload,
    )
    assert len(result.parsed) == 1
    assert len(result.written) == 1
    rec_path = result.written[0].path
    assert rec_path.exists()
    assert "Wrap find/xargs grep" in rec_path.read_text()


def test_run_dedupes_against_existing_db(tmp_path: Path):
    cfg = _mk_cfg(tmp_path)
    conn = store.init(cfg.paths.db)
    payload = json.dumps([
        {"title": "X", "body_markdown": "b", "signature": "sig", "confidence": "high"}
    ])
    a = analyze.run(conn, cfg, invoker=lambda _: payload)
    b = analyze.run(conn, cfg, invoker=lambda _: payload)
    assert len(a.written) == 1
    assert len(b.written) == 0
    assert b.skipped == 1


def test_run_dry_run_does_not_persist(tmp_path: Path):
    cfg = _mk_cfg(tmp_path)
    conn = store.init(cfg.paths.db)
    payload = json.dumps([
        {"title": "X", "body_markdown": "b", "signature": "sig"}
    ])
    workdir = tmp_path / "workdir"
    result = analyze.run(
        conn, cfg, invoker=lambda _: payload, dry_run=True, workdir=workdir,
    )
    assert len(result.parsed) == 1
    assert workdir.exists()
    assert (workdir / "prompt.md").exists()
    assert (workdir / "response.txt").exists()
    db_count = conn.execute(
        "SELECT COUNT(*) FROM recommendations"
    ).fetchone()[0]
    assert db_count == 0
    # Real recs_dir should NOT have a date subdir.
    assert not (cfg.paths.recs_dir / datetime.now(UTC).strftime("%Y-%m-%d")).exists()


def test_run_persists_response_even_on_parse_failure(tmp_path: Path):
    cfg = _mk_cfg(tmp_path)
    conn = store.init(cfg.paths.db)
    workdir = tmp_path / "wd"
    with pytest.raises(ValueError):
        analyze.run(
            conn, cfg, invoker=lambda _: "garbage that is not json", workdir=workdir,
        )
    # Even though parsing failed, prompt + response are on disk for debugging.
    assert (workdir / "prompt.md").exists()
    assert (workdir / "response.txt").read_text() == "garbage that is not json"


def test_file_based_invoker_uses_response_file(tmp_path: Path):
    """Mock 'claude' that writes the response file and the marker."""
    fake_claude = tmp_path / "fake-claude.sh"
    response_payload = '[{"title": "ok", "signature": "sig", "body_markdown": "b"}]'
    fake_claude.write_text(
        '#!/usr/bin/env bash\n'
        'set -e\n'
        '# args: -p "<instruction>"\n'
        f'cat > "{tmp_path}/wd/response.txt" <<\'EOF\'\n'
        f'{response_payload}\n'
        'EOF\n'
        f'touch "{tmp_path}/wd/done.marker"\n'
    )
    fake_claude.chmod(0o755)

    invoker = analyze.FileBasedInvoker(
        command_argv=[str(fake_claude)],
        workdir=tmp_path / "wd",
        timeout=30,
        poll_interval=0.1,
    )
    out = invoker("hello prompt")
    assert response_payload in out
    assert (tmp_path / "wd" / "prompt.md").read_text() == "hello prompt"


def test_file_based_invoker_surfaces_subprocess_failure(tmp_path: Path):
    """Process exits without marker → RuntimeError with stderr tail."""
    fake_claude = tmp_path / "fail.sh"
    fake_claude.write_text(
        '#!/usr/bin/env bash\necho "boom" >&2\nexit 7\n'
    )
    fake_claude.chmod(0o755)

    invoker = analyze.FileBasedInvoker(
        command_argv=[str(fake_claude)],
        workdir=tmp_path / "wd",
        timeout=10,
        poll_interval=0.1,
    )
    with pytest.raises(RuntimeError) as exc:
        invoker("anything")
    msg = str(exc.value)
    assert "code=7" in msg
    assert "boom" in msg


def test_file_based_invoker_times_out(tmp_path: Path):
    """Process runs forever without writing marker → TimeoutError."""
    fake_claude = tmp_path / "stall.sh"
    fake_claude.write_text(
        '#!/usr/bin/env bash\nsleep 60\n'
    )
    fake_claude.chmod(0o755)

    invoker = analyze.FileBasedInvoker(
        command_argv=[str(fake_claude)],
        workdir=tmp_path / "wd",
        timeout=2,
        poll_interval=0.1,
    )
    with pytest.raises(TimeoutError):
        invoker("anything")


def test_run_handles_malformed_response(tmp_path: Path):
    cfg = _mk_cfg(tmp_path)
    conn = store.init(cfg.paths.db)
    with pytest.raises(ValueError):
        analyze.run(conn, cfg, invoker=lambda _: "not even close to json")
