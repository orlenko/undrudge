"""Daily analyzer: digest → claude → recommendations.

Three steps, each isolated for testability:

1. Build the prompt from the digest + recent recs (deterministic).
2. Invoke the LLM via a file-based protocol (prompt.md → claude → response.txt
   → done.marker). Polling on the marker is more reliable than stdout
   capture for headless claude — that's the lesson encoded in the QC
   tooling on this machine.
3. Parse the JSON response into Recommendation objects, persist via
   ``recommend.write``.

The LLM call is dependency-injected (``invoker`` parameter) so tests
substitute a pure-Python mock without touching subprocess.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from . import capabilities as capabilities_mod
from . import config as cfg_mod
from . import digest as digest_mod
from . import events, llm, recommend, store

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = "analyze.md"
TOOL_META_TEMPLATE = "tool_meta.md"
DEFAULT_TIMEOUT = 600  # claude -p with a chunky prompt routinely needs minutes

# Coverage watermark: eventual completeness for scheduled runs. The
# launchd job fires at 02:30 whether or not the machine is awake, so a
# failed night would otherwise leave a permanent hole — every run
# digests a fixed trailing window and nothing ever re-covers the gap.
# We record the end of the last successfully analyzed window in
# ``cursors`` (one row per scope) and, when the caller passes no
# explicit window, extend the next run back to that point. The scope
# default stays the *minimum* so rows gather ingests late are still
# re-covered; the cap bounds the digest when the machine has been off
# for a long stretch (retention keeps 30d by default, well past it).
_HOUR_MS = 3_600_000
_WINDOW_DEFAULT_HOURS = {"daily": 24, "weekly": 168}
_CATCHUP_CAP_HOURS = {"daily": 7 * 24, "weekly": 14 * 24}


def _cursor_source(scope: str) -> str:
    return f"analyze:{scope}"


def covered_through(conn: sqlite3.Connection, scope: str) -> int | None:
    """End ts_ms of the last successfully analyzed window, or None."""
    row = conn.execute(
        "SELECT position FROM cursors WHERE source = ?",
        (_cursor_source(scope),),
    ).fetchone()
    return int(row[0]) if row else None


def _record_covered(
    conn: sqlite3.Connection, scope: str, end_ts_ms: int
) -> None:
    conn.execute(
        """INSERT INTO cursors(source, position, updated_at)
           VALUES(?, ?, ?)
           ON CONFLICT(source) DO UPDATE
             SET position = excluded.position,
                 updated_at = excluded.updated_at""",
        (_cursor_source(scope), str(end_ts_ms), store.now_ms()),
    )
    conn.commit()


def resolve_window_hours(
    conn: sqlite3.Connection, *, scope: str, end_ts_ms: int
) -> int:
    """Window for a run with no explicit --window: the scope default,
    extended to cover the gap since the last successful run (capped)."""
    default = _WINDOW_DEFAULT_HOURS.get(scope, 24)
    mark = covered_through(conn, scope)
    if mark is None or mark >= end_ts_ms:
        return default
    gap_hours = (end_ts_ms - mark + _HOUR_MS - 1) // _HOUR_MS
    hours = max(default, gap_hours)
    cap = _CATCHUP_CAP_HOURS.get(scope, default)
    if hours > cap:
        logger.warning(
            "catch-up window %dh exceeds %dh cap; truncating — activity "
            "older than the cap will not be analyzed",
            hours, cap,
        )
        hours = cap
    return hours


@dataclass
class AnalyzeResult:
    prompt: str
    response: str
    parsed: list[recommend.Recommendation]
    written: list[recommend.WriteResult]
    skipped: int
    # Probe-phase: how many evidence_refs the LLM produced and how many
    # resolved to actual DB rows. Used to tune the digest format.
    refs_total: int = 0
    refs_resolved: int = 0
    workdir: Path | None = None


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------


def load_prompt_template() -> str:
    return (files("undrudge") / "prompts" / PROMPT_TEMPLATE).read_text()


def load_tool_meta_section() -> str:
    return (files("undrudge") / "prompts" / TOOL_META_TEMPLATE).read_text()


def build_prompt(
    digest_md: str,
    recent_recs_md: str,
    *,
    scope: str = "daily",
    dismissed_md: str = "_(no recently dismissed recommendations)_",
    capability_section: str = "",
) -> str:
    tpl = load_prompt_template()
    # Tool meta-analysis is a weekly-only pass: per-pattern friction
    # dominates daily runs, and re-asking "should you use rg?" every
    # day produces noise the dedupe layer would have to absorb anyway.
    meta = load_tool_meta_section() if scope == "weekly" else ""
    return (
        tpl
        .replace("{digest}", digest_md)
        .replace("{recent_recs}", recent_recs_md)
        .replace("{dismissed_recs}", dismissed_md)
        .replace("{tool_meta_section}", meta)
        .replace("{capability_gap_section}", capability_section)
    )


# --------------------------------------------------------------------------
# LLM invocation
# --------------------------------------------------------------------------


Invoker = Callable[[str], str]


@dataclass
class FileBasedInvoker:
    """Invoke the LLM with a file-based handshake.

    Layout in ``workdir`` after one call:
        prompt.md     — the full prompt we built (input)
        response.txt  — what claude wrote (output)
        done.marker   — empty file claude touches when finished
        stderr.log    — captured stderr in case the run failed

    Why files: headless claude is unreliable about stdout (buffering,
    truncation, intermixed log lines, permission UI bleed-through). The
    Read/Write tools and a sentinel marker survive all of that. Pattern
    cribbed from QC's claude-runner.ts.
    """

    command_argv: list[str]
    workdir: Path
    timeout: int = DEFAULT_TIMEOUT
    poll_interval: float = 2.0
    # Teardown grace: SIGTERM then, if the child ignores it, SIGKILL.
    # A child can ignore SIGTERM when wedged in an interactive nono prompt
    # after writing its marker — see _reap.
    term_wait: float = 5.0
    kill_wait: float = 5.0

    def __call__(self, prompt: str) -> str:
        self.workdir.mkdir(parents=True, exist_ok=True)
        prompt_file = self.workdir / "prompt.md"
        response_file = self.workdir / "response.txt"
        marker_file = self.workdir / "done.marker"
        stderr_file = self.workdir / "stderr.log"

        # Clean any leftovers from a prior aborted call in the same dir.
        for f in (response_file, marker_file, stderr_file):
            f.unlink(missing_ok=True)

        prompt_file.write_text(prompt)

        instruction = (
            f"Read the file at {prompt_file} carefully and follow its "
            f"instructions exactly. Your final answer must be a JSON "
            f"array. Use the Write tool to write the JSON array (and "
            f"nothing else — no prose, no code fences) to {response_file}. "
            f"After writing, use the Write tool to create an empty file "
            f"at {marker_file} as a completion signal. Do not write your "
            f"answer to stdout — the response file is the authoritative "
            f"channel."
        )

        with stderr_file.open("wb") as err_fp:
            # cwd matters: the bundled nono wrapper grants `--allow-cwd`,
            # and launchd starts agents in `/` by default. Without an
            # explicit cwd nono would be asked to allow `/`, which it
            # refuses because it overlaps `~/.nono`.
            proc = subprocess.Popen(
                [*self.command_argv, "-p", instruction],
                stdout=subprocess.DEVNULL,
                stderr=err_fp,
                cwd=self.workdir,
            )
            started = time.time()
            logger.info(
                "spawned claude pid=%d argv=%s prompt=%d chars timeout=%ds",
                proc.pid, self.command_argv, len(prompt), self.timeout,
            )

            deadline = started + self.timeout
            last_log = started
            try:
                while time.time() < deadline:
                    if marker_file.exists():
                        elapsed = time.time() - started
                        logger.info(
                            "claude pid=%d wrote marker after %.1fs",
                            proc.pid, elapsed,
                        )
                        # Capture the result BEFORE reaping the child.
                        # claude writes response.txt and *then* the marker,
                        # so a present marker means the response is on disk
                        # — even if claude is still alive, e.g. blocked on
                        # an interactive nono "review denied paths" prompt
                        # that only appears post-run and that nobody answers
                        # in a headless invocation. A hung child must never
                        # discard a run that already succeeded.
                        response = (
                            response_file.read_text()
                            if response_file.exists() else None
                        )
                        _reap(proc, term_wait=self.term_wait,
                              kill_wait=self.kill_wait)
                        if response is not None:
                            return response
                        raise RuntimeError(
                            f"marker present but no response file at {response_file}"
                        )
                    if proc.poll() is not None:
                        # Process exited without writing the marker.
                        tail = _tail(stderr_file, 1500)
                        raise RuntimeError(
                            f"claude exited (code={proc.returncode}) without "
                            f"writing {marker_file}.\n"
                            f"--- stderr tail ---\n{tail}\n"
                            f"workdir: {self.workdir}"
                        )
                    now = time.time()
                    if now - last_log >= 30:
                        logger.info(
                            "claude pid=%d still running (%.0fs elapsed, %.0fs remaining)",
                            proc.pid, now - started, deadline - now,
                        )
                        last_log = now
                    logger.debug(
                        "poll pid=%d elapsed=%.1fs marker=%s",
                        proc.pid, time.time() - started, marker_file.exists(),
                    )
                    time.sleep(self.poll_interval)
            finally:
                _reap(proc, term_wait=self.term_wait, kill_wait=self.kill_wait)

        raise TimeoutError(
            f"claude did not finish in {self.timeout}s; workdir={self.workdir}"
        )


def _reap(
    proc: subprocess.Popen, *, term_wait: float = 5.0, kill_wait: float = 5.0
) -> None:
    """Best-effort teardown of the claude child. Never raises.

    The child can outlive its marker: claude writes ``response.txt`` and
    ``done.marker`` and *then*, instead of exiting, can block on an
    interactive nono "review denied paths" prompt that nobody will answer
    in a headless ``-p`` run. SIGTERM may not reap it promptly, so we
    escalate to SIGKILL — and swallow every error. By the time we reap,
    the caller has already captured the authoritative result from
    ``response.txt``, so teardown must never turn a successful run into a
    crash. The bug this guards against: an unwrapped ``proc.wait`` raising
    ``TimeoutExpired`` and discarding a finished analysis.
    """
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=term_wait)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        return
    try:
        proc.kill()
        proc.wait(timeout=kill_wait)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _tail(path: Path, n_chars: int) -> str:
    if not path.exists():
        return "(no stderr captured)"
    try:
        data = path.read_text()
        return data[-n_chars:] if len(data) > n_chars else data
    except OSError as e:
        return f"(failed to read stderr: {e})"


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------


_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE
)


def _build_repair_prompt(
    original_prompt: str, bad_response: str, err: Exception
) -> str:
    """Compose a follow-up that asks the LLM to fix its own JSON.

    Keeps the original instructions verbatim so the LLM still has the
    full context, but prefaces them with the failing attempt and the
    parser's complaint. One-shot retry; the harness gives up after the
    second parse failure.
    """
    snippet = bad_response.strip()
    if len(snippet) > 4000:
        snippet = snippet[:4000] + "\n…(truncated)"
    return (
        "Your previous response could not be parsed as a JSON array.\n"
        f"Parser error: {type(err).__name__}: {err}\n"
        "\n"
        "Previous response (verbatim):\n"
        "```\n"
        f"{snippet}\n"
        "```\n"
        "\n"
        "Re-emit a corrected response. Same constraints as before — a "
        "bare JSON array, no prose, no fences, each element matching "
        "the schema below. If your earlier judgment was right and the "
        "format was the only problem, fix the format and keep the "
        "content. If you can't recover meaningful output, return `[]`.\n"
        "\n"
        "Original instructions (for reference):\n"
        "---\n"
        f"{original_prompt}\n"
    )


def extract_json_array(text: str) -> list[dict]:
    """Pull a JSON array out of an LLM response.

    Tries: (1) the whole response is JSON, (2) a fenced JSON block,
    (3) the first '[' to the matching ']'. Raises ValueError if none work.
    """
    s = text.strip()

    # Direct
    if s.startswith("[") and s.endswith("]"):
        try:
            data = json.loads(s)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    # Fenced block
    for m in _FENCE_RE.finditer(text):
        candidate = m.group(1).strip()
        if candidate.startswith("["):
            try:
                data = json.loads(candidate)
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                continue

    # Scan for first balanced bracket pair
    start = text.find("[")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        data = json.loads(candidate)
                        if isinstance(data, list):
                            return data
                    except json.JSONDecodeError:
                        break

    raise ValueError("no JSON array found in LLM response")


def to_recommendations(items: list[dict], *, scope: str) -> list[recommend.Recommendation]:
    out: list[recommend.Recommendation] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        body = str(item.get("body_markdown") or item.get("body") or "").strip()
        signature = str(item.get("signature") or "").strip()
        if not title or not signature:
            continue
        refs = item.get("evidence_refs") or []
        # Keep only well-shaped dicts; the resolver tolerates more, but
        # the on-disk frontmatter shouldn't include junk like strings or
        # dicts missing the two required fields.
        clean_refs = [
            r for r in refs
            if isinstance(r, dict) and r.get("source") and r.get("external_id")
        ]
        out.append(
            recommend.Recommendation(
                title=title,
                body_markdown=body,
                signature=signature,
                automation_form=str(item.get("automation_form") or "other"),
                confidence=str(item.get("confidence") or "medium"),
                rationale=str(item.get("rationale") or ""),
                evidence=item.get("evidence") or [],
                evidence_refs=clean_refs or None,
                target_scope=str(item.get("target_scope") or "single_repo"),
                scope=scope,
            )
        )
    return out


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def _runs_dir(cfg: cfg_mod.Config) -> Path:
    return cfg.paths.db.parent / "runs"


def _new_run_workdir(cfg: cfg_mod.Config, *, suffix: str = "") -> Path:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    workdir = _runs_dir(cfg) / (f"{stamp}-{suffix}" if suffix else stamp)
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def run(
    conn: sqlite3.Connection,
    cfg: cfg_mod.Config,
    *,
    window_hours: int | None = None,
    end_ts_ms: int | None = None,
    scope: str = "daily",
    invoker: Invoker | None = None,
    dry_run: bool = False,
    workdir: Path | None = None,
) -> AnalyzeResult:
    """End-to-end: digest → LLM → recommendations.

    Every run gets its own workdir (under ``<data>/runs/<stamp>/``) holding
    ``prompt.md``, ``response.txt``, ``stderr.log``, and per-rec ``.md``
    files. Both dry-run and real runs leave the workdir for inspection.

    When ``window_hours`` is None the window is the scope default,
    extended to cover the gap since the last successful run — see
    ``resolve_window_hours``. An explicit value is used as-is.

    When ``dry_run=True``: still calls the LLM (so output is real), still
    writes per-rec markdown to the workdir, but does not insert DB rows or
    fire the on_write hook.
    """
    end_ts_ms = end_ts_ms or store.now_ms()
    auto_window = window_hours is None
    if auto_window:
        window_hours = resolve_window_hours(
            conn, scope=scope, end_ts_ms=end_ts_ms
        )
    logger.info(
        "analyze start: scope=%s window=%dh dry_run=%s",
        scope, window_hours, dry_run,
    )
    # Capability inventory rides the daily run: the probe is the one LLM
    # call gather refuses to make, and analyze already spawns claude daily.
    # Failure-isolated — a broken probe or a borked capabilities table must
    # never cost the day's analysis.
    capability_section = ""
    cap_offered_ts = 0
    if cfg.capabilities.enabled and scope == "daily":
        try:
            if not dry_run:
                capabilities_mod.refresh(conn, cfg, with_probe=True)
            # Snapshot the offer time before rendering: rows a concurrent
            # gather inserts while the LLM runs must stay "new" for the
            # next run, not get swallowed by a later consumption mark.
            cap_offered_ts = store.now_ms()
            capability_section = capabilities_mod.render_gap_section(conn, cfg)
        except Exception:
            logger.exception("capability refresh/render failed; continuing")
    digest_md = digest_mod.render_daily(
        conn, end_ts_ms=end_ts_ms, window_hours=window_hours
    )
    recent = recommend.recent_logged(conn)
    recent_md = recommend.render_recent_for_prompt(recent)
    dismissed = recommend.recent_dismissed(conn)
    dismissed_md = recommend.render_dismissed_for_prompt(dismissed)
    prompt = build_prompt(
        digest_md, recent_md, scope=scope, dismissed_md=dismissed_md,
        capability_section=capability_section,
    )
    logger.info(
        "prompt assembled: digest=%d chars, recent_recs=%d chars, "
        "dismissed=%d, prompt=%d chars",
        len(digest_md), len(recent_md), len(dismissed), len(prompt),
    )

    workdir = workdir or _new_run_workdir(cfg, suffix="dry" if dry_run else "")
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "prompt.md").write_text(prompt)
    logger.info("workdir: %s", workdir)

    if invoker is None:
        resolved = llm.resolve_command(cfg.llm.command)
        logger.info("llm.command resolved: %s -> %s", cfg.llm.command, resolved)
        argv = [str(resolved)]
        invoker = FileBasedInvoker(
            command_argv=argv,
            workdir=workdir,
            timeout=cfg.llm.timeout_seconds,
        )

    response = ""
    try:
        response = invoker(prompt)
    finally:
        # Persist whatever response we got — even on failure — so the user
        # can `cat <workdir>/response.txt` and see what happened.
        (workdir / "response.txt").write_text(response or "")
    logger.info("llm response: %d chars", len(response))

    try:
        items = extract_json_array(response)
    except ValueError as first_err:
        logger.warning(
            "first parse failed (%s) — sending repair prompt to claude",
            first_err,
        )
        # Repair attempt: hand the bad output back to claude with a
        # tight "this isn't valid JSON, fix it" prompt. One retry only;
        # if the second attempt fails, surface the original error.
        repair_prompt = _build_repair_prompt(prompt, response, first_err)
        (workdir / "repair-prompt.md").write_text(repair_prompt)
        repair_response = invoker(repair_prompt)
        # Overwrite response.txt with the (hopefully valid) repair so
        # the persisted-on-failure file matches what we'd parse.
        (workdir / "response.txt").write_text(repair_response or "")
        try:
            items = extract_json_array(repair_response)
            logger.info("repair succeeded; recovered %d items", len(items))
        except ValueError:
            logger.warning("repair also failed — surfacing original error")
            raise first_err from None
    recs = to_recommendations(items, scope=scope)
    logger.info("parsed %d items -> %d valid recommendations", len(items), len(recs))

    refs_resolved, refs_total = 0, 0
    for rec in recs:
        r, t = recommend.resolve_evidence_refs(conn, rec.evidence_refs)
        refs_resolved += r
        refs_total += t
    if refs_total:
        logger.info(
            "evidence refs resolved: %d/%d (%d%%)",
            refs_resolved, refs_total, 100 * refs_resolved // refs_total,
        )

    if dry_run:
        written: list[recommend.WriteResult] = []
        for rec in recs:
            md = recommend.render_markdown(
                recommend.normalize(rec),
                fingerprint=rec.fingerprint(),
                created=datetime.now(UTC),
            )
            slug = (
                "-".join(rec.title.lower().split())[:60].strip("-") or "rec"
            )
            path = workdir / f"{slug}.md"
            path.write_text(md)
            written.append(recommend.WriteResult(
                fingerprint=rec.fingerprint(), path=path,
                inserted=False, skipped_reason="dry-run",
            ))
        return AnalyzeResult(prompt=prompt, response=response, parsed=recs,
                             written=written, skipped=0,
                             refs_total=refs_total,
                             refs_resolved=refs_resolved,
                             workdir=workdir)

    written = []
    skipped = 0
    on_write = cfg.output.on_write or None
    for rec in recs:
        result = recommend.write(
            conn, rec, recs_dir=cfg.paths.recs_dir, on_write=on_write
        )
        if result.inserted:
            logger.info(
                "wrote rec %s: %s",
                result.fingerprint[:12], rec.title[:60],
            )
            written.append(result)
            events.record(
                cfg.paths.events_log,
                "rec_written",
                {
                    "id": result.fingerprint,
                    "scope": rec.scope,
                    "title": rec.title,
                    "signature": rec.signature,
                    "automation_form": rec.automation_form,
                    "confidence": rec.confidence,
                    "path": str(result.path) if result.path else None,
                    "evidence_refs_count": len(rec.evidence_refs or []),
                },
                conn=conn,
            )
        else:
            skipped += 1
            logger.info(
                "skipped rec %s (dup): %s",
                result.fingerprint[:12], rec.title[:60],
            )
    logger.info(
        "analyze done: parsed=%d written=%d skipped=%d",
        len(recs), len(written), skipped,
    )
    if capability_section:
        # The gap rows offered this run are no longer "new". Advance even
        # when the LLM proposed nothing — no pain, no rec is the designed
        # outcome, and re-offering the same rows every morning is the
        # chatty-feature-list failure mode.
        with contextlib.suppress(sqlite3.Error):
            capabilities_mod.mark_consumed(conn, now_ms=cap_offered_ts)
    events.record(
        cfg.paths.events_log,
        "analyze_complete",
        {
            "scope": scope,
            "window_hours": window_hours,
            "parsed": len(recs),
            "written": len(written),
            "skipped": skipped,
            "refs_total": refs_total,
            "refs_resolved": refs_resolved,
            "workdir": str(workdir),
        },
        conn=conn,
    )
    # (Dry runs returned above and never reach this.) Auto-resolved
    # windows always advance the watermark: they either reach back to it
    # or were truncated by the catch-up cap, and a capped hole is
    # accepted by design — freezing the mark would re-analyze the full
    # cap every night forever. Explicit --window runs advance only when
    # contiguous, so a short manual run can't mark a gap as covered.
    prev = covered_through(conn, scope)
    start_ts_ms = end_ts_ms - window_hours * _HOUR_MS
    contiguous = prev is None or start_ts_ms <= prev
    if (prev is None or prev < end_ts_ms) and (auto_window or contiguous):
        _record_covered(conn, scope, end_ts_ms)
    return AnalyzeResult(prompt=prompt, response=response, parsed=recs,
                         written=written, skipped=skipped,
                         refs_total=refs_total,
                         refs_resolved=refs_resolved,
                         workdir=workdir)
