"""Dispatch decision logic — pure, I/O-free unit tests.

Covers the deterministic core ported from the undrudge-dispatch
prototype: managed-clone resolution, routing precedence (invariant #4),
gating, dismissal similarity, and the approval-queue grammar (#3).
"""

from __future__ import annotations

from undrudge import dispatch
from undrudge.dispatch import DispatchConfig, Route


def _cfg(**kw) -> DispatchConfig:
    routes = kw.pop("routes", [])
    return DispatchConfig(routes=routes, **kw)


# --------------------------------------------------------------------------
# Managed clones (addendum 1)
# --------------------------------------------------------------------------


def test_resolve_managed_clones_materializes_dirless_route():
    cfg = _cfg(
        clones_dir="/Users/x/code/undrudge-checks",
        routes=[Route(name="volpe-lite", clone_url="git@h:o/v.git", branch="develop")],
    )
    cfg.resolve_managed_clones()
    r = cfg.routes[0]
    assert r.dir == "/Users/x/code/undrudge-checks/volpe-lite"
    assert r.managed_clone is True
    assert r.sync == ["fetch", "switch:develop", "pull"]


def test_resolve_managed_clones_leaves_explicit_dir_untouched():
    cfg = _cfg(
        clones_dir="/Users/x/code/undrudge-checks",
        routes=[Route(name="ops", dir="/Users/x/code2/ops")],
    )
    cfg.resolve_managed_clones()
    r = cfg.routes[0]
    assert r.dir == "/Users/x/code2/ops"
    assert r.managed_clone is False
    assert r.sync is None  # explicit/pinned route keeps its own (or no) sync


# --------------------------------------------------------------------------
# normalize_origin / path_under
# --------------------------------------------------------------------------


def test_normalize_origin_forms_match():
    a = dispatch.normalize_origin("git@github.com:orlenko/undrudge.git")
    b = dispatch.normalize_origin("https://github.com/orlenko/undrudge")
    c = dispatch.normalize_origin("ssh://git@github.com/orlenko/undrudge.git")
    assert a == b == c == "github.com/orlenko/undrudge"


def test_path_under():
    assert dispatch.path_under("/a/b", "/a")
    assert dispatch.path_under("/a", "/a")
    assert not dispatch.path_under("/ab", "/a")
    assert not dispatch.path_under("/c", "/a")


# --------------------------------------------------------------------------
# Routing precedence (invariant #4)
# --------------------------------------------------------------------------


def test_route_agent_global_goes_to_vault():
    cfg = _cfg(routes=[Route(name="vault"), Route(name="ops")])
    d = dispatch.route_for({"target_scope": "agent_global"}, "anything", [], cfg)
    assert d.route is not None and d.route.name == "vault"


def test_route_title_hint_overrides_cwd():
    cfg = _cfg(routes=[
        Route(name="ops", cwd_prefixes=["/Users/x/code/ops"]),
        Route(name="volpe-lite"),
    ])
    # Evidence cwd is under ops, but the title names volpe-lite → title wins.
    d = dispatch.route_for(
        {}, "Speed up volpe-lite deploy", ["/Users/x/code/ops/sub"], cfg
    )
    assert d.route is not None and d.route.name == "volpe-lite"
    assert d.note and "routed by title" in d.note


def test_route_cwd_prefix_match():
    cfg = _cfg(routes=[Route(name="ops", cwd_prefixes=["/Users/x/code/ops"])])
    d = dispatch.route_for({}, "no route name here", ["/Users/x/code/ops/a"], cfg)
    assert d.route is not None and d.route.name == "ops"
    assert d.note is None


def test_route_origin_fallback():
    cfg = _cfg(routes=[Route(name="fab", origins=["github.com/orlenko/fab"])])
    origins = {"/Users/x/somewhere": "git@github.com:orlenko/fab.git"}
    d = dispatch.route_for(
        {}, "title", ["/Users/x/somewhere"], cfg,
        origin_of=lambda c: origins.get(c), isdir=lambda c: True,
    )
    assert d.route is not None and d.route.name == "fab"


def test_route_unresolved_returns_none():
    cfg = _cfg(routes=[Route(name="ops", cwd_prefixes=["/Users/x/code/ops"])])
    d = dispatch.route_for({}, "title", ["/elsewhere"], cfg,
                           origin_of=lambda c: None, isdir=lambda c: True)
    assert d.route is None


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------


def test_deny_hit():
    pats = [r"\brm -rf\b", r"force.?push"]
    assert dispatch.deny_hit("clean up", "rm -rf <path>", "", pats) == r"\brm -rf\b"
    assert dispatch.deny_hit("safe", "ls -la", "body", pats) is None


def test_similar_dismissed_reuses_undrudge_similarity():
    cfg = _cfg(similarity_threshold=0.85, similarity_min_sig_len=8)
    dismissed = [("aaaa11112222", "Wrap find/grep", "find . -name <str> | xargs grep <str>")]
    hit = dispatch.similar_dismissed(
        "find . -name <str> -type f | xargs grep <str>", dismissed, cfg
    )
    assert hit is not None and hit[0] == "aaaa11112222"


def test_similar_dismissed_ignores_unrelated():
    cfg = _cfg(similarity_threshold=0.85, similarity_min_sig_len=8)
    dismissed = [("aaaa11112222", "x", "find . -name <str> | xargs grep <str>")]
    assert dispatch.similar_dismissed("git rebase -i HEAD~<n>", dismissed, cfg) is None


def test_similar_dismissed_skips_short_signatures():
    cfg = _cfg(similarity_threshold=0.85, similarity_min_sig_len=20)
    dismissed = [("a", "x", "ls")]
    assert dispatch.similar_dismissed("ls", dismissed, cfg) is None


# --------------------------------------------------------------------------
# Approval grammar (invariant #3)
# --------------------------------------------------------------------------


def test_parse_approvals_grammar():
    text = "\n".join([
        "# queue",
        "- [x] approve a1b2c3d4e5f6",
        "- [x] approve 99887766 -> volpe-lite",
        "- [x] dismiss deadbeef00 — reason: too niche",
        "- [ ] approve unchecked0000",      # unchecked → ignored
        "- [x] frobnicate abc123",          # checked but malformed
    ])
    actions, malformed = dispatch.parse_approvals(text)
    assert set(actions) == {"a1b2c3d4e5f6", "99887766", "deadbeef00"}
    assert actions["99887766"].action == "approve"
    assert actions["99887766"].route == "volpe-lite"
    assert actions["deadbeef00"].action == "dismiss"
    assert actions["deadbeef00"].reason == "too niche"
    assert any("frobnicate" in m for m in malformed)


def test_resolve_approvals_prefix_match():
    raw = {
        "a1b2": dispatch.Approval("approve", None, ""),
        "ffff": dispatch.Approval("dismiss", None, "no"),
    }
    rec_ids = ["a1b2c3d4e5f6aaaa", "0000111122223333"]
    resolved, failures = dispatch.resolve_approvals(raw, rec_ids)
    assert "a1b2c3d4e5f6" in resolved
    assert any("ffff" in f and "no logged rec" in f for f in failures)


def test_resolve_approvals_ambiguous_is_a_failure():
    raw = {"ab": dispatch.Approval("approve", None, "")}
    rec_ids = ["abcd00001111", "abef00002222"]
    resolved, failures = dispatch.resolve_approvals(raw, rec_ids)
    assert resolved == {}
    assert any("ambiguous" in f for f in failures)


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------


def test_ledger_append_load_roundtrip(tmp_path):
    p = tmp_path / "ledger.jsonl"
    led = dispatch.Ledger.load(p)
    led.append("abc123", "dispatched", route="ops")
    led.append("abc123", "verdict", disposition="ship", artifact="http://pr/1")
    reloaded = dispatch.Ledger.load(p)
    assert reloaded.has_action("abc123", "dispatched")
    assert reloaded.has_action("abc123", "verdict")
    assert reloaded.last_verdict("abc123")["disposition"] == "ship"


def test_ledger_in_memory_when_path_none():
    led = dispatch.Ledger()
    led.append("x", "dispatched")
    assert led.has_action("x", "dispatched")  # no file, still tracked


def test_ledger_last_verdict_is_the_latest():
    led = dispatch.Ledger()
    led.append("x", "verdict", disposition="needs-human")
    led.append("x", "verdict", disposition="ship")
    assert led.last_verdict("x")["disposition"] == "ship"


# --------------------------------------------------------------------------
# resolve_rid
# --------------------------------------------------------------------------


def test_resolve_rid_unique_prefix():
    assert dispatch.resolve_rid("abc", ["abcdef0000001111"]) == "abcdef000000"


def test_resolve_rid_ambiguous_falls_back_to_prefix():
    assert dispatch.resolve_rid("ab", ["abcd1111", "abef2222"]) == "ab"


# --------------------------------------------------------------------------
# reconcile (invariant #1 flip-first; addendum 2)
# --------------------------------------------------------------------------


def _flip_recorder(fail: set[str] | None = None):
    """A fake status-flip that records calls and can be told to fail."""
    fail = fail or set()
    calls = []

    def flip(verb, rid, why):
        calls.append((verb, rid))
        return rid not in fail

    return flip, calls


def _vf(id12, disposition, route="ops", artifact=None, reason=None):
    return dispatch.VerdictFile(
        data={"id": id12, "disposition": disposition,
              "artifact": artifact, "reason": reason},
        fallback_id=id12, route_name=route,
    )


def test_reconcile_dismiss_verdict_flips_then_ledgers():
    led = dispatch.Ledger()
    flip, calls = _flip_recorder()
    rep = dispatch.reconcile(
        ledger=led, logged_ids={"aaaa11112222"}, all_ids=["aaaa11112222ffff"],
        verdicts=[_vf("aaaa11112222", "dismiss-stale")],
        flip=flip, gh_state=lambda a, r: None,
    )
    assert ("dismiss", "aaaa11112222") in calls
    assert led.has_action("aaaa11112222", "verdict")
    assert rep.flips == 1


def test_reconcile_flip_failure_does_not_ledger_so_it_retries():
    led = dispatch.Ledger()
    flip, _ = _flip_recorder(fail={"aaaa11112222"})
    rep = dispatch.reconcile(
        ledger=led, logged_ids={"aaaa11112222"}, all_ids=["aaaa11112222"],
        verdicts=[_vf("aaaa11112222", "reject-as-framed")],
        flip=flip, gh_state=lambda a, r: None,
    )
    # Flip failed -> no verdict ledgered -> next run retries.
    assert not led.has_action("aaaa11112222", "verdict")
    assert any("failed" in f for f in rep.failures)


def test_reconcile_already_processed_verdict_skipped():
    led = dispatch.Ledger()
    led.append("aaaa11112222", "verdict", disposition="ship")
    flip, calls = _flip_recorder()
    dispatch.reconcile(
        ledger=led, logged_ids={"aaaa11112222"}, all_ids=["aaaa11112222"],
        verdicts=[_vf("aaaa11112222", "dismiss-stale")],
        flip=flip, gh_state=lambda a, r: None,
    )
    assert calls == []  # no re-flip


def test_reconcile_needs_human_becomes_a_hold():
    led = dispatch.Ledger()
    led.append("aaaa11112222", "verdict", disposition="needs-human", reason="need creds")
    rep = dispatch.reconcile(
        ledger=led, logged_ids={"aaaa11112222"}, all_ids=["aaaa11112222"],
        verdicts=[], flip=lambda *a: True, gh_state=lambda a, r: None,
    )
    assert rep.holds.get("aaaa11112222") == ("needs-human", "need creds")


def test_reconcile_ship_pr_merged_implements():
    led = dispatch.Ledger()
    led.append("aaaa11112222", "verdict", disposition="ship",
               artifact="https://github.com/o/r/pull/1", route="ops")
    flip, calls = _flip_recorder()
    rep = dispatch.reconcile(
        ledger=led, logged_ids=set(), all_ids=["aaaa11112222"], verdicts=[],
        flip=flip, gh_state=lambda a, r: "MERGED",
    )
    assert ("implement", "aaaa11112222") in calls
    assert led.has_action("aaaa11112222", "pr-merged")
    assert any("implemented" in c for c in rep.completed)


def test_reconcile_ship_pr_closed_dismisses():
    led = dispatch.Ledger()
    led.append("aaaa11112222", "verdict", disposition="ship",
               artifact="https://github.com/o/r/pull/1", route="ops")
    flip, calls = _flip_recorder()
    dispatch.reconcile(
        ledger=led, logged_ids=set(), all_ids=["aaaa11112222"], verdicts=[],
        flip=flip, gh_state=lambda a, r: "CLOSED",
    )
    assert ("dismiss", "aaaa11112222") in calls
    assert led.has_action("aaaa11112222", "pr-closed")


def test_reconcile_ship_pr_open_is_pending():
    led = dispatch.Ledger()
    led.append("aaaa11112222", "verdict", disposition="ship",
               artifact="https://github.com/o/r/pull/1", route="ops")
    rep = dispatch.reconcile(
        ledger=led, logged_ids=set(), all_ids=["aaaa11112222"], verdicts=[],
        flip=lambda *a: True, gh_state=lambda a, r: "OPEN",
    )
    assert any("aaaa11112222" in p for p in rep.pending_prs)


def test_reconcile_non_url_artifact_flagged_only_while_logged():
    # Still logged -> needs manual close-out (failure).
    led = dispatch.Ledger()
    led.append("aaaa11112222", "verdict", disposition="ship", artifact="/some/file.py")
    rep = dispatch.reconcile(
        ledger=led, logged_ids={"aaaa11112222"}, all_ids=["aaaa11112222"],
        verdicts=[], flip=lambda *a: True, gh_state=lambda a, r: None,
    )
    assert any("needs manual close-out" in f for f in rep.failures)
    assert led.has_action("aaaa11112222", "artifact-unusable")


def test_reconcile_non_url_artifact_clean_terminal_when_not_logged():
    # addendum 2: a non-git route implemented directly and left logged set.
    led = dispatch.Ledger()
    led.append("aaaa11112222", "verdict", disposition="ship", artifact="/some/file.py")
    rep = dispatch.reconcile(
        ledger=led, logged_ids=set(), all_ids=["aaaa11112222"],
        verdicts=[], flip=lambda *a: True, gh_state=lambda a, r: None,
    )
    assert rep.failures == []  # not an error
    assert not led.has_action("aaaa11112222", "artifact-unusable")


# --------------------------------------------------------------------------
# collect_verdict_files
# --------------------------------------------------------------------------


def test_collect_verdict_files_from_done_and_spool():
    routes = [Route(name="ops", dir="/clones/ops")]
    tree = {
        "/clones/ops/.undrudge-inbox/done": ["a1b2.verdict.json", "notes.txt"],
        "/spool": ["c3d4.verdict.json"],
    }
    files = {
        "/clones/ops/.undrudge-inbox/done/a1b2.verdict.json": {"id": "a1b2", "disposition": "ship"},
        "/spool/c3d4.verdict.json": {"id": "c3d4", "disposition": "dismiss-stale"},
    }
    out = dispatch.collect_verdict_files(
        routes, "/spool",
        isdir=lambda d: d in tree,
        listdir=lambda d: tree.get(d, []),
        load_json=lambda p: files[p],
    )
    got = {(v.route_name, v.data["disposition"]) for v in out}
    assert got == {("ops", "ship"), ("spool", "dismiss-stale")}
