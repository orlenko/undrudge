"""Tests for the pull-side repo matcher (`undrudge here`).

The matching rule under test: a rec belongs to the repo you're standing
in when its evidence was observed inside this working tree, or inside a
sibling clone with the same remote origin, or — failing both — when it
names this repo in its title.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from undrudge import locate, recommend

# --------------------------------------------------------------------------
# normalize_origin
# --------------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:orlenko/undrudge.git", "github.com/orlenko/undrudge"),
    ("https://github.com/orlenko/undrudge.git", "github.com/orlenko/undrudge"),
    ("https://github.com/orlenko/undrudge", "github.com/orlenko/undrudge"),
    ("ssh://git@github.com/orlenko/undrudge.git", "github.com/orlenko/undrudge"),
    ("https://github.com/orlenko/undrudge/", "github.com/orlenko/undrudge"),
    ("git@GitHub.com:orlenko/undrudge.git", "github.com/orlenko/undrudge"),
    # A non-default SSH port must not become part of the path.
    ("ssh://git@github.com:2222/orlenko/undrudge.git",
     "github.com/orlenko/undrudge"),
])
def test_normalize_origin_unifies_protocols(url: str, expected: str):
    assert locate.normalize_origin(url) == expected


def test_normalize_origin_handles_empty_and_local():
    assert locate.normalize_origin(None) is None
    assert locate.normalize_origin("   ") is None
    a = locate.normalize_origin("/srv/mirrors/thing.git")
    assert a == locate.normalize_origin("/srv/mirrors/thing")
    assert a is not None


def test_clones_of_one_repo_share_an_origin():
    """The case this tier exists for: six ops checkouts are one repo."""
    assert (
        locate.normalize_origin("git@github.com:urban-sky/ops.git")
        == locate.normalize_origin("https://github.com/urban-sky/ops")
    )


def test_normalize_origin_keeps_distinct_hosts_apart():
    assert (
        locate.normalize_origin("git@github.com:o/r.git")
        != locate.normalize_origin("git@gitlab.com:o/r.git")
    )


# --------------------------------------------------------------------------
# match_tier
# --------------------------------------------------------------------------


def _repo(root="/w/ops", origin="github.com/o/ops"):
    return locate.RepoIdent(root=root, origin=origin, dirty=False)


def _origin_of(mapping: dict[str, str]):
    return lambda cwd: mapping.get(cwd)


def test_match_tier_this_clone_exact_and_subdir():
    tier, hits = locate.match_tier(
        ["/w/ops", "/w/ops/src/deep"], _repo(), origin_of=_origin_of({})
    )
    assert tier == "this_clone"
    assert hits == ["/w/ops", "/w/ops/src/deep"]


def test_match_tier_prefix_does_not_match_sibling_name():
    """/w/ops must not swallow /w/ops-archive."""
    tier, _ = locate.match_tier(
        ["/w/ops-archive/x"], _repo(), origin_of=_origin_of({})
    )
    assert tier is None


def test_match_tier_dead_directory_still_matches_by_prefix():
    """A deleted worktree keeps its cwd string; prefix needs no I/O."""
    tier, hits = locate.match_tier(
        ["/w/ops/.qc/dev-worktrees/gone"], _repo(), origin_of=lambda _c: None,
    )
    assert (tier, hits) == ("this_clone", ["/w/ops/.qc/dev-worktrees/gone"])


def test_match_tier_same_repo_via_origin():
    tier, hits = locate.match_tier(
        ["/w/ops4"], _repo(),
        origin_of=_origin_of({"/w/ops4": "github.com/o/ops"}),
    )
    assert (tier, hits) == ("same_repo", ["/w/ops4"])


def test_match_tier_prefers_this_clone_and_keeps_both():
    tier, hits = locate.match_tier(
        ["/w/ops4", "/w/ops/x"], _repo(),
        origin_of=_origin_of({"/w/ops4": "github.com/o/ops"}),
    )
    assert (tier, hits) == ("this_clone", ["/w/ops/x", "/w/ops4"])


def test_match_tier_ignores_other_repos_and_blanks():
    tier, hits = locate.match_tier(
        ["/w/other", "", "/tmp/scratch"], _repo(),
        origin_of=_origin_of({"/w/other": "github.com/o/other"}),
    )
    assert (tier, hits) == (None, [])


def test_match_tier_without_origin_falls_back_to_prefix_only():
    repo = locate.RepoIdent(root="/w/solo", origin=None)
    assert locate.match_tier(
        ["/w/solo/a"], repo, origin_of=_origin_of({})
    )[0] == "this_clone"
    assert locate.match_tier(
        ["/w/elsewhere"], repo, origin_of=_origin_of({})
    )[0] is None


def test_match_tier_root_repo_claims_nothing():
    """A repo rooted at / would otherwise match every absolute path."""
    repo = locate.RepoIdent(root="/", origin="github.com/o/r")
    assert locate.match_tier(
        ["/anything/at/all"], repo, origin_of=_origin_of({})
    ) == (None, [])


# --------------------------------------------------------------------------
# names_repo
# --------------------------------------------------------------------------


def test_names_repo_matches_whole_word_only():
    repo = locate.RepoIdent(root="/w/undrudge", origin="github.com/o/undrudge")
    assert locate.names_repo("undrudge show should print the body", repo)
    assert locate.names_repo("Fix UNDRUDGE health reporting", repo)
    assert not locate.names_repo("undrudgery is not a word", repo)
    assert not locate.names_repo("nothing relevant here", repo)


def test_names_repo_ignores_short_names():
    repo = locate.RepoIdent(root="/w/ops", origin="github.com/o/ops")
    assert not locate.names_repo("stack-health script for ops checks", repo)


@pytest.mark.parametrize("name,title", [
    ("tests", "Add tests for the deploy script in volpe-lite"),
    ("docs", "Automate the docs build in ops"),
    ("tools", "Wrap the deploy tools invocation"),
    ("vault", "Stop hand-copying vault secrets during ops deploys"),
    ("data", "Cache the data fixtures between runs"),
])
def test_names_repo_rejects_generic_directory_names(name: str, title: str):
    """A repo called `tests` must not claim every rec mentioning tests."""
    repo = locate.RepoIdent(root=f"/w/{name}", origin=f"github.com/o/{name}")
    assert not locate.names_repo(title, repo)


def test_names_repo_uses_origin_slug_when_checkout_was_renamed():
    repo = locate.RepoIdent(root="/w/ops-local", origin="github.com/o/thelocalring")
    assert locate.names_repo("thelocalring deploy wrapper", repo)


# --------------------------------------------------------------------------
# rank
# --------------------------------------------------------------------------


def _cand(cid, tier, cwds=(), created=0, title="t"):
    return locate.Candidate(
        id=cid, title=title, body_path="/x.md",
        created_at=created, tier=tier, cwds=list(cwds),
    )


def test_rank_orders_by_tier_then_evidence_then_recency():
    out = locate.rank([
        _cand("d", "named", created=900),
        _cand("c", "same_repo", cwds=["/a"], created=100),
        _cand("b", "this_clone", cwds=["/a"], created=500),
        _cand("a", "this_clone", cwds=["/a", "/b"], created=1),
    ])
    assert [c.id for c in out] == ["a", "b", "c", "d"]


# --------------------------------------------------------------------------
# Evidence resolution — the contract `here` leans on
# --------------------------------------------------------------------------

SESSION_ID = "11111111-2222-3333-4444-555555555555"
MSG_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CMD_ID = "0197ab5c1111aaaa"


def _seed_activity(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sessions(id, source, project, started_at, last_seen) "
        "VALUES(?,'claude','/w/ops',1,2)", (SESSION_ID,)
    )
    conn.execute(
        "INSERT INTO messages(id, session_id, seq, ts, role, text) "
        "VALUES(?,?,0,1,'user','hi')", (MSG_ID, SESSION_ID)
    )
    conn.execute(
        "INSERT INTO commands(source, external_id, ts, command, cwd) "
        "VALUES('atuin',?,1,'ls','/w/ops4')", (CMD_ID,)
    )
    conn.commit()


def test_evidence_cwds_resolves_the_sources_recs_actually_use(
    db: sqlite3.Connection,
):
    _seed_activity(db)
    got = recommend.evidence_cwds(db, {"evidence_refs": [
        {"source": "atuin", "external_id": CMD_ID[:8]},
        {"source": "msg", "external_id": MSG_ID[:8]},
        {"source": "session", "external_id": SESSION_ID[:8]},
    ]})
    assert sorted(got) == ["/w/ops", "/w/ops4"]


def test_evidence_cwds_ignores_wildcards_in_llm_authored_handles(
    db: sqlite3.Connection,
):
    """`external_id` is model-written text. A LIKE metacharacter must not
    turn into a wildcard that resolves to some unrelated repo's cwd."""
    _seed_activity(db)
    conn = db
    conn.execute(
        "INSERT INTO commands(source, external_id, ts, command, cwd) "
        "VALUES('atuin','0197ffff2222bbbb',1,'ls','/w/somewhere-private')"
    )
    conn.commit()
    # As a LIKE pattern '0197%' would match both commands and resolve to
    # an arbitrary one. Stripped to hex it is a 4-char literal, under the
    # length floor, so it resolves to nothing.
    assert recommend.evidence_cwds(conn, {"evidence_refs": [
        {"source": "atuin", "external_id": "0197%"},
    ]}) == []
    # Likewise '_' would stand in for the typo'd character and match
    # /w/ops4; as a literal it matches nothing.
    assert recommend.evidence_cwds(conn, {"evidence_refs": [
        {"source": "atuin", "external_id": "0197_b5c1111aaaa"},
    ]}) == []
    # The correct handle still resolves.
    assert recommend.evidence_cwds(conn, {"evidence_refs": [
        {"source": "atuin", "external_id": CMD_ID[:8]},
    ]}) == ["/w/ops4"]


def test_evidence_cwds_tolerates_malformed_refs(db: sqlite3.Connection):
    _seed_activity(db)
    assert recommend.evidence_cwds(db, {"evidence_refs": [
        {"source": "wat", "external_id": CMD_ID},
        {"source": "atuin"},
        {"source": "atuin", "external_id": "abc"},  # under the length floor
        "not-a-dict",
    ]}) == []
    assert recommend.evidence_cwds(db, {}) == []


# --------------------------------------------------------------------------
# here() end to end
# --------------------------------------------------------------------------


def _write_rec(
    conn: sqlite3.Connection, tmp_path: Path, *,
    rec_id: str, title: str, target_scope: str | None = "single_repo",
    refs: list[dict] | None = None, created_at: int = 1000,
    status: str = "logged", raw: str | None = None,
) -> Path:
    """A rec row plus the markdown file `here` reads frontmatter from."""
    body = tmp_path / f"{rec_id}.md"
    if raw is not None:
        body.write_text(raw)
    else:
        fm = {
            "id": rec_id, "scope": "daily", "status": status,
            "title": title, "evidence_refs": refs or [],
        }
        if target_scope is not None:
            fm["target_scope"] = target_scope
        body.write_text(
            "```json\n" + json.dumps(fm, indent=2) + "\n```\n\n# " + title + "\n"
        )
    conn.execute(
        "INSERT INTO recommendations"
        "(id, scope, title, signature, body_path, evidence, status,"
        " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (rec_id, "daily", title, title, str(body), "[]", status,
         created_at, created_at),
    )
    conn.commit()
    return body


def test_here_matches_ranks_and_filters_scope(db: sqlite3.Connection, tmp_path: Path):
    _seed_activity(db)
    _write_rec(db, tmp_path, rec_id="a" * 64, title="in this clone",
               refs=[{"source": "session", "external_id": SESSION_ID[:8]}])
    _write_rec(db, tmp_path, rec_id="b" * 64, title="in a sibling clone",
               refs=[{"source": "atuin", "external_id": CMD_ID[:8]}])
    _write_rec(db, tmp_path, rec_id="c" * 64, title="elsewhere entirely",
               refs=[])
    _write_rec(db, tmp_path, rec_id="d" * 64, title="global thing",
               target_scope="cross_cutting",
               refs=[{"source": "session", "external_id": SESSION_ID[:8]}])

    res = locate.here(
        db, _repo(), origin_of=_origin_of({"/w/ops4": "github.com/o/ops"}),
    )
    assert [c.id[:1] for c in res.candidates] == ["a", "b"]
    assert [c.tier for c in res.candidates] == ["this_clone", "same_repo"]
    assert res.matched == 2
    assert res.considered == 3  # cross_cutting excluded by default scope


def test_here_all_scopes_includes_cross_cutting(db: sqlite3.Connection, tmp_path: Path):
    _seed_activity(db)
    _write_rec(db, tmp_path, rec_id="d" * 64, title="global thing",
               target_scope="cross_cutting",
               refs=[{"source": "session", "external_id": SESSION_ID[:8]}])
    res = locate.here(db, _repo(), origin_of=_origin_of({}), scopes=())
    assert len(res.candidates) == 1


def test_here_keeps_recs_written_before_target_scope_existed(
    db: sqlite3.Connection, tmp_path: Path
):
    """Legacy frontmatter has no target_scope. Judge it on its evidence
    rather than dropping it out of every counter."""
    _seed_activity(db)
    _write_rec(db, tmp_path, rec_id="a" * 64, title="legacy rec",
               target_scope=None,
               refs=[{"source": "session", "external_id": SESSION_ID[:8]}])
    res = locate.here(db, _repo(), origin_of=_origin_of({}))
    assert res.considered == 1
    assert [c.tier for c in res.candidates] == ["this_clone"]


def test_here_named_tier_when_no_cwd_evidence_points_here(
    db: sqlite3.Connection, tmp_path: Path
):
    """The tool-fix case: pain felt elsewhere, fix belongs in this repo."""
    _seed_activity(db)
    _write_rec(db, tmp_path, rec_id="e" * 64,
               title="undrudge show should print the rec body",
               refs=[{"source": "atuin", "external_id": CMD_ID[:8]}])
    repo = locate.RepoIdent(root="/w/undrudge", origin="github.com/o/undrudge")
    res = locate.here(db, repo, origin_of=_origin_of({"/w/ops4": "github.com/o/ops"}))
    assert [c.tier for c in res.candidates] == ["named"]


def test_here_named_tier_ranks_below_cwd_evidence(
    db: sqlite3.Connection, tmp_path: Path
):
    _seed_activity(db)
    _write_rec(db, tmp_path, rec_id="f" * 64,
               title="undrudge thing seen in this clone",
               refs=[{"source": "session", "external_id": SESSION_ID[:8]}])
    _write_rec(db, tmp_path, rec_id="0" * 64,
               title="undrudge thing with no evidence here", refs=[])
    repo = locate.RepoIdent(root="/w/ops", origin="github.com/o/undrudge")
    res = locate.here(db, repo, origin_of=_origin_of({}))
    assert [c.tier for c in res.candidates] == ["this_clone", "named"]


def test_here_respects_status_and_limit(db: sqlite3.Connection, tmp_path: Path):
    _seed_activity(db)
    for i, rid in enumerate("abc"):
        _write_rec(db, tmp_path, rec_id=rid * 64, title=f"rec {i}",
                   refs=[{"source": "session", "external_id": SESSION_ID[:8]}],
                   created_at=1000 + i)
    _write_rec(db, tmp_path, rec_id="d" * 64, title="already done",
               status="implemented",
               refs=[{"source": "session", "external_id": SESSION_ID[:8]}])
    repo = _repo()

    assert len(locate.here(db, repo, origin_of=_origin_of({})).candidates) == 3
    capped = locate.here(db, repo, origin_of=_origin_of({}), limit=2)
    assert len(capped.candidates) == 2
    assert capped.matched == 3  # the caller can tell it was truncated
    assert len(locate.here(db, repo, origin_of=_origin_of({}), limit=0).candidates) == 3
    # A negative limit must not silently drop the last candidate.
    assert len(locate.here(db, repo, origin_of=_origin_of({}), limit=-1).candidates) == 3
    done = locate.here(db, repo, origin_of=_origin_of({}), status="implemented")
    assert [c.title for c in done.candidates] == ["already done"]


@pytest.mark.parametrize("raw,label", [
    (None, "missing file"),
    ("no fence at all, just prose\n", "no frontmatter"),
    ("```json\n[1, 2, 3]\n```\n\nbody\n", "frontmatter is not an object"),
])
def test_here_counts_unplaceable_rec_files(
    db: sqlite3.Connection, tmp_path: Path, raw: str | None, label: str
):
    """Anything we can't read frontmatter from is reported, never
    silently treated as 'no match for this repo'."""
    _seed_activity(db)
    body = _write_rec(db, tmp_path, rec_id="a" * 64, title="x", raw=raw)
    if raw is None:
        body.unlink()
    res = locate.here(db, _repo(), origin_of=_origin_of({}))
    assert res.candidates == [], label
    assert res.unreadable == 1, label
    assert res.considered == 0, label


def test_here_on_a_repo_with_nothing_queued(db: sqlite3.Connection, tmp_path: Path):
    _seed_activity(db)
    _write_rec(db, tmp_path, rec_id="a" * 64, title="somewhere else",
               refs=[{"source": "session", "external_id": SESSION_ID[:8]}])
    repo = locate.RepoIdent(root="/w/unrelated", origin="github.com/o/unrelated")
    res = locate.here(db, repo, origin_of=_origin_of({}))
    assert res.candidates == []
    assert (res.matched, res.considered) == (0, 1)


# --------------------------------------------------------------------------
# repo_at — real git I/O
# --------------------------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
         *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    root = tmp_path / "clone"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "remote", "add", "origin", "git@github.com:o/thing.git")
    (root / "f.txt").write_text("one\n")
    _git(root, "add", "f.txt")
    _git(root, "commit", "-qm", "init")
    return root


def test_repo_at_reads_root_origin_and_cleanliness(clone: Path):
    ident = locate.repo_at(clone)
    assert ident is not None
    assert Path(ident.root).resolve() == clone.resolve()
    assert ident.origin == "github.com/o/thing"
    assert ident.dirty is False
    assert ident.dirty_reason == ""


def test_repo_at_sees_modified_tracked_files(clone: Path):
    (clone / "f.txt").write_text("changed\n")
    ident = locate.repo_at(clone)
    assert ident.dirty is True
    assert "modified" in ident.dirty_reason


def test_repo_at_ignores_untracked_files(clone: Path):
    """Untracked scratch files don't stop you branching."""
    (clone / "scratch.txt").write_text("notes\n")
    assert locate.repo_at(clone).dirty is False


def test_repo_at_treats_a_stopped_rebase_as_dirty(clone: Path):
    """A worktree can be spotless and still be mid-rebase; branching out
    from under one strands the resolved conflicts."""
    (clone / "f.txt").write_text("two\n")
    _git(clone, "commit", "-aqm", "second")
    subprocess.run(
        ["git", "-C", str(clone), "-c", "user.email=t@t", "-c", "user.name=t",
         "rebase", "HEAD~1", "-x", "false"],
        capture_output=True, text=True, check=False,
    )
    ident = locate.repo_at(clone)
    assert ident.dirty is True
    assert "in progress" in ident.dirty_reason


def test_repo_at_fails_closed_when_git_cannot_answer(clone: Path, monkeypatch):
    """A status timeout must read as dirty. This is the only gate
    stopping an agent committing on top of someone's work."""
    real = locate._git

    def flaky(args: list[str]):
        if "status" in args:
            return (False, "")
        return real(args)

    monkeypatch.setattr(locate, "_git", flaky)
    ident = locate.repo_at(clone)
    assert ident.dirty is True
    assert "unavailable" in ident.dirty_reason


def test_repo_at_handles_a_repo_with_no_origin(tmp_path: Path):
    root = tmp_path / "noremote"
    root.mkdir()
    _git(root, "init", "-q")
    ident = locate.repo_at(root)
    assert ident is not None
    assert ident.origin is None


def test_repo_at_returns_none_outside_a_repo(tmp_path: Path):
    plain = tmp_path / "notarepo"
    plain.mkdir()
    assert locate.repo_at(plain) is None


def test_repo_ident_defaults_to_dirty():
    """Anyone constructing one without asking git gets the safe answer."""
    assert locate.RepoIdent(root="/w/x").dirty is True


# --------------------------------------------------------------------------
# origin_resolver
# --------------------------------------------------------------------------


def test_origin_resolver_walks_up_to_a_surviving_ancestor(clone: Path):
    """Dev worktrees are deleted once merged. Their evidence should still
    identify the repo through the clone above them."""
    gone = clone / ".qc" / "dev-worktrees" / "long-merged"
    assert not gone.exists()
    resolve = locate.origin_resolver()
    assert resolve(str(gone)) == "github.com/o/thing"


def test_origin_resolver_caches_and_gives_up_outside_any_repo(tmp_path: Path):
    resolve = locate.origin_resolver()
    missing = tmp_path / "nope" / "deeper"
    assert resolve(str(missing)) is None
    assert resolve(str(missing)) is None  # cached, no second walk
