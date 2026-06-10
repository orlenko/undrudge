"""Dispatch orchestration — the I/O layer that drives a run.

Wires the pure logic (``dispatch`` module: config, routing, gating,
reconcile, renderers) to the real world: the undrudge store, the
filesystem (briefs, managed clones), git/gh/cmux subprocesses, and the
vault report/queue. The decision logic lives in ``dispatch``; this module
only moves bytes and shells out.

Subprocess access goes through ``self._run`` (overridable) so a run can
be exercised end-to-end in tests with a stub instead of real git/gh.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import dispatch, recommend, store

_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}


@dataclass
class RunResult:
    dispatched: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    flips: int = 0
    failures: list[str] = field(default_factory=list)
    report_text: str = ""


def _parse_frontmatter(body_path: str) -> tuple[dict, str]:
    """Pull the leading ```json fence + body from a rec markdown file."""
    try:
        text = Path(body_path).read_text(encoding="utf-8")
    except OSError:
        return {}, ""
    if not text.startswith("```json\n"):
        return {}, text
    end = text.find("\n```", len("```json\n"))
    if end < 0:
        return {}, text
    try:
        fm = json.loads(text[len("```json\n"):end])
    except json.JSONDecodeError:
        return {}, text
    return fm, text[end + len("\n```"):].lstrip("\n")


class Dispatcher:
    def __init__(
        self,
        ucfg: cfg_mod.Config,
        dcfg: dispatch.DispatchConfig,
        conn: Any,
        *,
        dry: bool = False,
        now: datetime | None = None,
    ) -> None:
        self.ucfg = ucfg
        self.cfg = dcfg
        self.conn = conn
        self.dry = dry
        self.now = now or datetime.now(UTC)
        self.today = self.now.date().isoformat()
        self.now_hm = self.now.strftime("%H:%M")
        self.state_dir = Path(dcfg.state_dir)
        self.spool_dir = str(self.state_dir / "verdicts")
        self.vault = Path(dcfg.vault_dir)
        self.queue_path = self.vault / "undrudge" / "dispatch-queue.md"
        self.log_dir = self.vault / "undrudge" / "dispatch-log"
        ledger_path = None if dry else self.state_dir / "ledger.jsonl"
        if not dry:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / "verdicts").mkdir(parents=True, exist_ok=True)
        self.ledger = dispatch.Ledger.load(ledger_path)
        self.flipped_ids: set[str] = set()
        self.report: dict[str, list[str]] = {
            k: [] for k in (
                "dispatched", "held", "would_dismiss", "auto_dismissed",
                "completed", "pending_prs", "approvals", "verdicts",
                "failures", "health", "notes",
            )
        }

    # ---------------------------------------------------------------- subprocess

    def _run(self, argv: list[str], *, timeout: int = 30, cwd: str | None = None,
             extra_env: dict | None = None) -> tuple[int, str, str]:
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        try:
            p = subprocess.run(argv, capture_output=True, text=True,
                               timeout=timeout, cwd=cwd, env=env)
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except subprocess.TimeoutExpired:
            return 124, "", f"timeout: {' '.join(argv)}"
        except OSError as e:
            return 127, "", f"{type(e).__name__}: {e}"

    # ---------------------------------------------------------------- status flip

    def flip(self, verb: str, id12: str, why: str) -> bool:
        """Single status-mutation path (invariant #1). verb is dismiss /
        implement; maps to recommend.set_status. Records the flip."""
        if self.dry:
            return True
        status = {"dismiss": "dismissed", "implement": "implemented"}[verb]
        try:
            res = recommend.set_status(self.conn, id12, status, reason=why)
        except (LookupError, ValueError) as e:
            self.report["failures"].append(f"flip {verb} {id12}: {e}")
            return False
        if res.matched_id is None:
            self.report["failures"].append(f"flip {verb} {id12}: no match")
            return False
        self.flipped_ids.add(id12)
        return True

    def mark_dispatched(self, id12: str, route_name: str) -> bool:
        """Flip a rec to 'dispatched' when its brief goes out — the
        transition the port owns (the prototype only ledgered it). This is
        what tells the analyzer the rec is in-flight so it won't re-suggest
        a variant while a session is working on it."""
        if self.dry:
            return True
        try:
            res = recommend.set_status(
                self.conn, id12, "dispatched", reason=f"dispatched to {route_name}")
        except (LookupError, ValueError) as e:
            self.report["failures"].append(f"mark dispatched {id12}: {e}")
            return False
        if res.matched_id is None:
            return False
        self.flipped_ids.add(id12)
        return True

    def gh_state(self, artifact: str, route_name: str) -> str | None:
        route = self.cfg.route_by_name(route_name)
        cwd = route.dir if route and route.dir and os.path.isdir(route.dir) else None
        rc, out, _ = self._run([self.cfg.gh_bin, "pr", "view", artifact,
                                "--json", "state"], timeout=30, cwd=cwd)
        if rc != 0:
            return None
        try:
            return json.loads(out).get("state", "")
        except json.JSONDecodeError:
            return None

    # ---------------------------------------------------------------- db reads

    def fetch_logged(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, scope, title, signature, body_path, created_at "
            "FROM recommendations WHERE status='logged' ORDER BY created_at"
        ).fetchall()
        recs = []
        for r in rows:
            fm, body = _parse_frontmatter(r["body_path"])
            recs.append({
                "id": r["id"], "id12": r["id"][:12], "scope": r["scope"],
                "title": r["title"], "signature": r["signature"],
                "created": r["created_at"], "fm": fm, "body": body,
                "cwds": [], "forced": False, "forced_route": None, "route": None,
            })
        return recs

    def fetch_dismissed(self) -> list[tuple[str, str, str]]:
        rows = self.conn.execute(
            "SELECT id, title, signature FROM recommendations "
            "WHERE status IN ('dismissed', 'rejected')"
        ).fetchall()
        return [(r["id"][:12], r["title"], r["signature"]) for r in rows]

    def resolve_cwds(self, fm: dict) -> list[str]:
        counts: dict[str, int] = {}
        for ref in fm.get("evidence_refs") or []:
            eid = "".join(c for c in str(ref.get("external_id", "")).lower()
                          if c in "0123456789abcdef")
            if len(eid) < 6:
                continue
            src = ref.get("source", "")
            if src in ("atuin", "shell"):
                q = ("SELECT cwd FROM commands WHERE "
                     "replace(external_id,'-','') LIKE ? LIMIT 5")
            elif src in ("claude", "msg"):
                q = ("SELECT s.project FROM messages m JOIN sessions s "
                     "ON m.session_id=s.id WHERE replace(m.id,'-','') LIKE ? LIMIT 5")
            elif src == "session":
                q = ("SELECT project FROM sessions WHERE "
                     "replace(id,'-','') LIKE ? LIMIT 5")
            else:
                continue
            for row in self.conn.execute(q, (eid + "%",)).fetchall():
                cwd = row[0]
                if cwd:
                    counts[cwd] = counts.get(cwd, 0) + 1
        return [c for c, _ in sorted(counts.items(), key=lambda kv: -kv[1])]

    # ---------------------------------------------------------------- git / fs

    def route_dir_ok(self, route: dispatch.Route) -> str | None:
        d = route.dir or ""
        if os.path.isdir(d) and os.listdir(d):
            return None
        if route.managed_clone and route.clone_url:
            if self.dry:
                return None
            os.makedirs(os.path.dirname(d), exist_ok=True)
            rc, out, err = self._run([self.cfg.git_bin, "clone", "--quiet",
                                      route.clone_url, d], timeout=600,
                                     extra_env=_GIT_ENV)
            if rc != 0:
                return f"clone failed for {route.name}: {out[:120]} {err[:120]}"
            return None
        return f"route dir {d} missing or empty — clone deleted/moved?"

    def sync_route(self, route: dispatch.Route) -> str | None:
        spec = route.sync
        if not spec:
            return None
        d, git = route.dir or "", self.cfg.git_bin
        rc, out, _ = self._run([git, "-C", d, "status", "--porcelain", "-uno",
                                "--ignore-submodules=all"], timeout=20, extra_env=_GIT_ENV)
        if rc != 0:
            return f"git status failed in {d}"
        if out.strip():
            return f"tracked files dirty in {d} — refusing to sync"
        if self.dry:
            return None
        for step in spec:
            if step == "fetch":
                argv = [git, "-C", d, "-c", "fetch.recurseSubmodules=no", "fetch", "--quiet"]
            elif step.startswith("switch:"):
                argv = [git, "-C", d, "switch", "--quiet", step.split(":", 1)[1]]
            elif step == "pull":
                argv = [git, "-C", d, "-c", "fetch.recurseSubmodules=no",
                        "pull", "--ff-only", "--quiet"]
            else:
                return f"unknown sync step {step!r}"
            rc, out, err = self._run(argv, timeout=120, extra_env=_GIT_ENV)
            if rc != 0:
                return f"sync step {step!r} failed in {d}: {out[:120]} {err[:120]}"
        return None

    def freshness(self, route: dispatch.Route) -> tuple[str, str, str]:
        d = route.dir or ""
        rc, branch, _ = self._run([self.cfg.git_bin, "-C", d, "branch",
                                   "--show-current"], timeout=10, extra_env=_GIT_ENV)
        rc2, gitlog, _ = self._run([self.cfg.git_bin, "-C", d, "log", "--oneline",
                                    "-15", "--since=4.weeks"], timeout=20, extra_env=_GIT_ENV)
        rc3, prs, _ = self._run([self.cfg.gh_bin, "pr", "list", "--state", "merged",
                                 "--limit", "10"], timeout=30,
                                cwd=d if os.path.isdir(d) else None)
        return (branch if rc == 0 else "?",
                gitlog if rc2 == 0 else "", prs if rc3 == 0 else "")

    def ensure_inbox(self, route: dispatch.Route) -> str:
        inbox = os.path.join(route.dir or "", ".undrudge-inbox")
        if not self.dry:
            os.makedirs(os.path.join(inbox, "done"), exist_ok=True)
            exclude = os.path.join(route.dir or "", ".git", "info", "exclude")
            if os.path.isfile(exclude):
                content = Path(exclude).read_text(encoding="utf-8")
                if ".undrudge-inbox" not in content:
                    with open(exclude, "a", encoding="utf-8") as f:
                        f.write("\n.undrudge-inbox/\n")
        return inbox

    def write_brief(self, rec: dict, route: dispatch.Route,
                    branch: str, gitlog: str, prs: str) -> None:
        inbox = self.ensure_inbox(route)
        path = os.path.join(inbox, f"{rec['id12']}.md")
        brief = dispatch.render_brief(
            id12=rec["id12"], title=rec["title"], body=rec["body"],
            cwds=rec.get("cwds", []), fm=rec["fm"], route_name=route.name,
            route_dir=route.dir or "", branch=branch, gitlog=gitlog, prs=prs,
            today=self.today,
        )
        if not self.dry and not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(brief)

    def inbox_count(self, route: dispatch.Route) -> int:
        inbox = os.path.join(route.dir or "", ".undrudge-inbox")
        if not os.path.isdir(inbox):
            return 0
        return len([f for f in os.listdir(inbox) if f.endswith(".md")])

    # ---------------------------------------------------------------- health

    def health(self) -> None:
        h = self.report["health"]
        # gather health: latest gather_complete (schema shared with doctor).
        latest, transient = None, 0
        cutoff = (self.now.timestamp() - 86400) * 1000
        log = self.ucfg.paths.events_log
        if log.exists():
            for line in log.read_text(encoding="utf-8").splitlines()[-500:]:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (e.get("ts") or 0) < cutoff:
                    continue
                if e.get("event") == "gather_failed":
                    transient += 1
                elif e.get("event") == "gather_complete":
                    latest = e
        if latest is not None:
            failed = latest.get("failed_sources") or []
            if failed:
                h.append(f"gather: latest run FAILING: {', '.join(failed)}  ⚠️")
            elif transient:
                h.append(f"gather: healthy ({transient} transient self-healed in 24h)")
            else:
                h.append("gather: healthy")
        try:
            free_gb = shutil.disk_usage(os.path.expanduser("~")).free / 2**30
            h.append(f"disk free: {free_gb:.0f} GB{'  ⚠️' if free_gb < 15 else ''}")
        except OSError:
            pass

    # ---------------------------------------------------------------- writes

    def write_queue(self, holds: list[tuple[dict, str]]) -> None:
        names = ", ".join(r.name for r in self.cfg.routes)
        text = dispatch.render_queue(holds, route_names=names)
        if self.dry:
            return
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        self.queue_path.write_text(text, encoding="utf-8")

    def write_report(self, staging_notes: list[str]) -> str:
        text = dispatch.render_report(
            self.report, staging_notes, now_hm=self.now_hm, dry=self.dry,
            apply_denylist=self.cfg.apply_denylist,
        )
        if not self.dry:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            day = self.log_dir / f"{self.today}.md"
            header = "" if day.exists() else f"# undrudge dispatch — {self.today}\n"
            with day.open("a", encoding="utf-8") as f:
                f.write(header + text)
        return text

    # ---------------------------------------------------------------- run

    def run(self) -> RunResult:
        raw_approvals, malformed = dispatch.parse_approvals(
            self.queue_path.read_text(encoding="utf-8")
            if self.queue_path.exists() else ""
        )
        for line in malformed:
            self.report["failures"].append(f"malformed approval line ignored: {line}")

        # Reconcile verdicts + draft PRs (stage 2 logic, wired to real I/O).
        # The "open" set a verdict can act on is logged OR dispatched — a
        # dispatched rec awaiting its session's verdict is still open.
        all_ids = [r[0] for r in self.conn.execute("SELECT id FROM recommendations")]
        logged_ids = {
            r[0][:12] for r in self.conn.execute(
                "SELECT id FROM recommendations WHERE status IN ('logged','dispatched')")
        }
        verdicts = dispatch.collect_verdict_files(
            self.cfg.routes, self.spool_dir,
            isdir=os.path.isdir, listdir=os.listdir,
            load_json=lambda p: _load_json(p),
        )
        rrep = dispatch.reconcile(
            ledger=self.ledger, logged_ids=logged_ids, all_ids=all_ids,
            verdicts=verdicts, flip=self.flip, gh_state=self.gh_state,
            now=self.now.isoformat(timespec="seconds"),
        )
        self.report["verdicts"].extend(rrep.verdicts)
        self.report["completed"].extend(rrep.completed)
        self.report["pending_prs"].extend(rrep.pending_prs)
        self.report["failures"].extend(rrep.failures)
        self.flipped_ids |= rrep.flipped_ids
        verdict_holds = rrep.holds

        recs = self.fetch_logged()
        dismissed = self.fetch_dismissed()
        approvals, ap_failures = dispatch.resolve_approvals(
            raw_approvals, [r["id"] for r in recs])
        self.report["failures"].extend(ap_failures)

        holds: list[tuple[dict, str]] = []
        normal: list[dict] = []
        forced: list[dict] = []

        for rec in recs:
            rid = rec["id12"]
            act = approvals.get(rid)
            if act and act.action == "dismiss":
                if self.flip("dismiss", rid, "human via dispatch-queue"):
                    self.report["approvals"].append(
                        f"{rid} dismissed — {act.reason or 'no reason given'}")
                    self.ledger.append(rid, "human-dismissed",
                                       ts=self.now.isoformat(timespec="seconds"),
                                       reason=act.reason)
                continue
            if rid in verdict_holds:
                d, reason = verdict_holds[rid]
                holds.append((rec, f"verdict {d} — {reason[:120]}"))
                continue
            if self.ledger.has_action(rid, "verdict", "dispatched"):
                continue

            is_forced = bool(act and act.action == "approve")
            rec["forced"] = is_forced
            rec["forced_route"] = act.route if is_forced else None

            pat = dispatch.deny_hit(
                rec["title"], rec["signature"], rec["body"], self.cfg.deny_patterns)
            if pat and not is_forced:
                if self.cfg.apply_denylist:
                    if self.flip("dismiss", rid, f"deny-list {pat}"):
                        self.report["auto_dismissed"].append(f"{rid} {rec['title']} (`{pat}`)")
                    continue
                self.report["would_dismiss"].append(f"{rid} {rec['title']} (`{pat}`)")

            if not is_forced:
                sim = dispatch.similar_dismissed(rec["signature"], dismissed, self.cfg)
                if sim:
                    holds.append((rec, f"≥{self.cfg.similarity_threshold:.0%} "
                                       f"similar to dismissed {sim[0]} ({sim[1]})"))
                    continue
                if rec["fm"].get("target_scope") == "cross_cutting":
                    holds.append((rec, "cross_cutting scope — needs a human routing decision"))
                    continue

            if rec["forced_route"]:
                route = self.cfg.route_by_name(rec["forced_route"])
                if route is None:
                    self.report["failures"].append(
                        f"{rid} approved with unknown route `{rec['forced_route']}`")
                    holds.append((rec, "no route resolved (unknown route name)"))
                    continue
            else:
                rec["cwds"] = self.resolve_cwds(rec["fm"])
                decision = dispatch.route_for(
                    rec["fm"], rec["title"], rec["cwds"], self.cfg,
                    origin_of=self._origin_of, isdir=os.path.isdir)
                if decision.note:
                    self.report["notes"].append(f"{rid}: {decision.note}")
                route = decision.route
            if route is None:
                holds.append((rec, f"no route resolved (scope {rec['fm'].get('target_scope')})"))
                continue
            rec["route"] = route

            conf_ok = rec["fm"].get("confidence") in self.cfg.gate_confidence
            form_ok = rec["fm"].get("automation_form") in self.cfg.gate_forms
            if is_forced:
                forced.append(rec)
            elif route.auto and conf_ok and form_ok:
                normal.append(rec)
            elif not route.auto:
                holds.append((rec, f"route {route.name} is approval-only"))
            else:
                holds.append((rec, f"gate: confidence={rec['fm'].get('confidence')} "
                                   f"form={rec['fm'].get('automation_form')}"))

        normal.sort(key=lambda r: (
            0 if r["route"].name == "vault" else 1,
            0 if r["scope"] == "weekly" else 1, r["created"] or 0))
        cap = self.cfg.max_dispatch_per_run
        for rec in normal[cap:]:
            holds.append((rec, "daily cap reached — will dispatch on a later run"))
        to_dispatch = forced + normal[:cap]

        by_route: dict[str, list[dict]] = {}
        for rec in to_dispatch:
            by_route.setdefault(rec["route"].name, []).append(rec)
        for rname, rrecs in by_route.items():
            route = rrecs[0]["route"]
            err = self.route_dir_ok(route) or self.sync_route(route)
            if err:
                self.report["failures"].append(f"route {rname}: {err}")
                for rec in rrecs:
                    holds.append((rec, f"route {rname} unavailable — see failures"))
                continue
            branch, gitlog, prs = self.freshness(route)
            for rec in rrecs:
                self.write_brief(rec, route, branch, gitlog, prs)
                self.mark_dispatched(rec["id12"], rname)
                self.ledger.append(rec["id12"], "dispatched",
                                   ts=self.now.isoformat(timespec="seconds"),
                                   route=rname, title=rec["title"])
                self.report["dispatched"].append(f"{rec['id12']} → {rname} — {rec['title']}")

        self.health()
        self.write_queue(holds)
        for rec, why in holds:
            self.report["held"].append(f"{rec['id12']} {rec['title']} — {why}")
        report_text = self.write_report([])

        return RunResult(
            dispatched=self.report["dispatched"], held=self.report["held"],
            flips=len(self.flipped_ids), failures=self.report["failures"],
            report_text=report_text,
        )

    def _origin_of(self, cwd: str) -> str | None:
        rc, out, _ = self._run([self.cfg.git_bin, "-C", cwd, "remote",
                                "get-url", "origin"], timeout=10, extra_env=_GIT_ENV)
        return out if rc == 0 else None

    def status(self) -> list[str]:
        recs = self.fetch_logged()
        lines = [f"logged recs: {len(recs)}"]
        for rec in recs:
            rid = rec["id12"]
            if self.ledger.has_action(rid, "verdict"):
                disp = "pr-pending"
            elif self.ledger.has_action(rid, "dispatched"):
                disp = "dispatched"
            else:
                disp = "pending"
            lines.append(f"  {rid}  {disp:11} {rec['title']}")
        return lines


def _load_json(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# The standalone prototype's config lives here. During the coexistence
# period (prototype still on launchd, port available as `undrudge
# dispatch`), the port falls back to reading it so both run off ONE
# source of truth — no hand-migration into a [dispatch] TOML block.
PROTOTYPE_CONFIG_PATH = os.path.expanduser("~/.config/undrudge-dispatch/config.json")


def load_dispatch_config(
    ucfg: cfg_mod.Config,
    *,
    config_toml_path: Path | None = None,
    prototype_json_path: str | None = None,
) -> dispatch.DispatchConfig | None:
    """Resolve the dispatch config.

    Prefers a ``[dispatch]`` table in undrudge's own config.toml. Failing
    that, falls back to the standalone prototype's ``config.json`` so the
    two tools share config while they coexist. Returns ``None`` if neither
    is configured.
    """
    import tomllib

    toml_path = config_toml_path or cfg_mod.default_config_path()
    if toml_path.exists():
        try:
            table = tomllib.loads(toml_path.read_text()).get("dispatch")
        except (OSError, tomllib.TOMLDecodeError):
            table = None
        if table:
            return dispatch.DispatchConfig.from_dict(table)

    proto = Path(prototype_json_path or PROTOTYPE_CONFIG_PATH)
    if proto.exists():
        try:
            data = json.loads(proto.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return dispatch.DispatchConfig.from_dict(data)
    return None


def cmd_dispatch(ucfg: cfg_mod.Config, *, subcmd: str, dry: bool) -> int:
    dcfg = load_dispatch_config(ucfg)
    if dcfg is None:
        print("dispatch not configured: add a [dispatch] section to "
              f"{cfg_mod.default_config_path()}", file=sys.stderr)
        return 2
    conn = store.open_db(ucfg.paths.db)
    try:
        store.apply_schema(conn)
        d = Dispatcher(ucfg, dcfg, conn, dry=dry)
        if subcmd == "status":
            for line in d.status():
                print(line)
            return 0
        result = d.run()
    finally:
        conn.close()

    print(f"dispatched: {len(result.dispatched)}")
    print(f"held:       {len(result.held)}")
    print(f"flips:      {result.flips}")
    print(f"failures:   {len(result.failures)}")
    for f in result.failures:
        print(f"  ! {f}", file=sys.stderr)
    return 0
