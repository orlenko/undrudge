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

import json
import re
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from . import config as cfg_mod
from . import digest as digest_mod
from . import events, llm, recommend

PROMPT_TEMPLATE = "analyze.md"
DEFAULT_TIMEOUT = 600  # claude -p with a chunky prompt routinely needs minutes


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


def build_prompt(digest_md: str, recent_recs_md: str) -> str:
    tpl = load_prompt_template()
    return tpl.replace("{digest}", digest_md).replace("{recent_recs}", recent_recs_md)


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

            deadline = time.time() + self.timeout
            try:
                while time.time() < deadline:
                    if marker_file.exists():
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.terminate()
                            proc.wait(timeout=5)
                        if response_file.exists():
                            return response_file.read_text()
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
                    time.sleep(self.poll_interval)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()

        raise TimeoutError(
            f"claude did not finish in {self.timeout}s; workdir={self.workdir}"
        )


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
    window_hours: int = 24,
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

    When ``dry_run=True``: still calls the LLM (so output is real), still
    writes per-rec markdown to the workdir, but does not insert DB rows or
    fire the on_write hook.
    """
    digest_md = digest_mod.render_daily(
        conn, end_ts_ms=end_ts_ms, window_hours=window_hours
    )
    recent = recommend.recent_logged(conn)
    recent_md = recommend.render_recent_for_prompt(recent)
    prompt = build_prompt(digest_md, recent_md)

    workdir = workdir or _new_run_workdir(cfg, suffix="dry" if dry_run else "")
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "prompt.md").write_text(prompt)

    if invoker is None:
        argv = [str(llm.resolve_command(cfg.llm.command))]
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

    try:
        items = extract_json_array(response)
    except ValueError as first_err:
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
        except ValueError:
            raise first_err from None
    recs = to_recommendations(items, scope=scope)

    refs_resolved, refs_total = 0, 0
    for rec in recs:
        r, t = recommend.resolve_evidence_refs(conn, rec.evidence_refs)
        refs_resolved += r
        refs_total += t

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
    return AnalyzeResult(prompt=prompt, response=response, parsed=recs,
                         written=written, skipped=skipped,
                         refs_total=refs_total,
                         refs_resolved=refs_resolved,
                         workdir=workdir)
