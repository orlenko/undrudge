"""undrudge CLI."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import (
    __version__,
    analyze,
    browse,
    config,
    digest,
    events,
    ingest_claude,
    ingest_codex,
    ingest_shell,
    llm,
    prune,
    recommend,
    scrub,
    store,
)


class _ShortNameFormatter(logging.Formatter):
    """Strip the ``undrudge.`` prefix from logger names for readability."""

    def format(self, record: logging.LogRecord) -> str:
        record.short_name = record.name.removeprefix("undrudge.")
        return super().format(record)


def _setup_logging(verbosity: int) -> None:
    """Configure the ``undrudge`` logger from a ``-v`` count.

    0 = WARNING (default, silent), 1 = INFO, 2+ = DEBUG. The handler is
    attached to the ``undrudge`` logger directly (not root) so we don't
    leak narration from third-party libraries if any are added later.
    All output goes to stderr; stdout is reserved for the tool's actual
    payload (digest markdown, rec paths, etc.).
    """
    level = (
        logging.WARNING if verbosity <= 0
        else logging.INFO if verbosity == 1
        else logging.DEBUG
    )
    logger = logging.getLogger("undrudge")
    logger.setLevel(level)
    # Don't stack handlers if main() is invoked more than once (tests).
    for h in list(logger.handlers):
        logger.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _ShortNameFormatter("%(levelname)-5s %(short_name)s: %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False


def _cmd_init(args: argparse.Namespace) -> int:
    cfg_path = config.default_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg_path.exists() and not args.force:
        print(f"config already exists: {cfg_path}")
    else:
        cfg_path.write_text(config.render_default_config_toml())
        print(f"wrote config: {cfg_path}")

    cfg = config.load(cfg_path)

    for d in (cfg.paths.recs_dir, cfg.paths.digests_dir, cfg.paths.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    conn = store.init(cfg.paths.db)
    conn.close()
    print(f"initialized db:   {cfg.paths.db}")
    print(f"recs dir:         {cfg.paths.recs_dir}")
    print(f"digests dir:      {cfg.paths.digests_dir}")
    print(f"logs dir:         {cfg.paths.logs_dir}")
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    cfg = config.load()
    ok = True

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "ok " if cond else "FAIL"
        print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
        if not cond:
            ok = False

    print("paths:")
    check("config readable", config.default_config_path().exists(),
          str(config.default_config_path()))
    check("db file present", cfg.paths.db.exists(), str(cfg.paths.db))
    histories = _history_sources(cfg)
    check(
        "agent history available",
        any(available for _, available, _ in histories),
        f"claude={cfg.claude.projects_root}; "
        f"codex={cfg.codex.home if cfg.codex else '(disabled)'}",
    )
    for label, available, detail in histories:
        # Each provider is optional, so expose partial configuration without
        # making doctor fail when the other provider remains healthy.
        mark = "ok " if available else "-- "
        print(f"  [{mark}] {label} (optional) — {detail}")
    check("atuin db", cfg.atuin.db.exists(), str(cfg.atuin.db))

    print("\nbinaries:")
    try:
        resolved = llm.resolve_command(cfg.llm.command)
        check(f"llm.command ({cfg.llm.command})", True, str(resolved))
    except FileNotFoundError as e:
        check(f"llm.command ({cfg.llm.command})", False, str(e))

    nono_bin = shutil.which("nono")
    check("nono on PATH (optional)", nono_bin is not None,
          nono_bin or "not installed; bundled wrapper will fall through to bare claude")
    claude_bin = shutil.which("claude")
    check("claude on PATH", claude_bin is not None,
          claude_bin or "not found")

    print("\ndb sanity:")
    if cfg.paths.db.exists():
        try:
            conn = store.open_db(cfg.paths.db)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for t in ("sessions", "messages", "commands", "recommendations",
                      "cursors", "redaction_failures"):
                check(f"table {t}", t in tables)
            failures = conn.execute(
                "SELECT COUNT(*) FROM redaction_failures"
            ).fetchone()[0]
            check("redaction failures clean", failures == 0,
                  f"{failures} entries — review table")
            _report_retention(cfg, conn)
            conn.close()
        except sqlite3.DatabaseError as e:
            check("db opens", False, str(e))

    print("\ngather:")
    _check_gather_health(cfg, check)

    return 0 if ok else 1


def _report_retention(cfg: config.Config, conn: sqlite3.Connection) -> None:
    """Informational size/retention line. Not a pass/fail check — a big DB is
    a disk-cost question, not a broken install."""
    size = prune.human_bytes(prune.db_size_bytes(cfg.paths.db))
    days = cfg.retention.days
    policy = f"retention {days}d" if days > 0 else "retention off (keep everything)"
    oldest_ms = prune.oldest_message_ms(conn)
    oldest = (
        datetime.fromtimestamp(oldest_ms / 1000, tz=UTC).strftime("%Y-%m-%d")
        if oldest_ms else "none"
    )
    detail = f"{size}; {policy}; oldest message {oldest}"
    if days > 0:
        aged_msgs, aged_cmds = prune.count_aged(conn, prune.cutoff_for_days(days))
        if aged_msgs or aged_cmds:
            detail += (
                f"; {aged_msgs + aged_cmds:,} row(s) past the cutoff "
                f"(gather prunes up to {prune.GATHER_MAX_ROWS:,}/run)"
            )
    print(f"  [--] db size — {detail}")


def _history_sources(cfg: config.Config) -> list[tuple[str, bool, str]]:
    """Return provider-specific history availability for doctor output."""
    codex_available = bool(
        cfg.codex and any(root.exists() for root in cfg.codex.session_roots)
    )
    return [
        ("claude history", cfg.claude.projects_root.exists(), str(cfg.claude.projects_root)),
        (
            "codex history",
            codex_available,
            str(cfg.codex.home) if cfg.codex else "disabled in this config",
        ),
    ]


def _check_gather_health(cfg: config.Config, check) -> None:
    """Report gather health from events.jsonl, keyed off the *latest* run.

    Silent hourly failure is how the atuin WAL race went unnoticed for
    weeks, so doctor must surface a *currently broken* gather. But it must
    not cry wolf: the atuin read can transiently fail (WAL-open race) and
    self-heal on the next hourly run, so flagging any failure in a 24h
    window paints every morning red after one overnight blip — training
    the operator to ignore the check.

    So: FAIL only when the most recent ``gather_complete`` shows a source
    still failing. Transient failures that a later run recovered are
    reported as an informational note, not a FAIL.
    """
    log = cfg.paths.events_log
    if not log.exists():
        check("events log present", False,
              f"{log} not created yet — gather has not run")
        return

    cutoff_ms = store.now_ms() - 24 * 3600 * 1000
    last_complete: dict | None = None
    transient_failures = 0
    try:
        with log.open(encoding="utf-8") as fp:
            for line in fp:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("ts", 0) < cutoff_ms:
                    continue
                event = e.get("event")
                if event == "gather_complete":
                    last_complete = e
                elif event == "gather_failed":
                    transient_failures += 1
    except OSError as e:
        check("events log readable", False, str(e))
        return

    # Old DBs predate gather_complete: fall back to "any failure in 24h"
    # rather than claim health we can't prove.
    if last_complete is None:
        if transient_failures:
            check("last gather run healthy", False,
                  f"{transient_failures} failure(s) in 24h; "
                  f"no gather_complete event yet (pre-upgrade gather)")
        else:
            check("last gather run healthy", True,
                  "no failures in 24h (no gather_complete event yet)")
        return

    failed_now = last_complete.get("failed_sources") or []
    if failed_now:
        check("last gather run healthy", False,
              f"latest run still failing: {', '.join(failed_now)}")
        return

    # Latest run was clean. Note any self-healed transients as info, not FAIL.
    if transient_failures:
        check("last gather run healthy", True,
              f"clean — but {transient_failures} transient failure(s) in 24h "
              f"self-healed (likely atuin WAL-open race)")
    else:
        check("last gather run healthy", True, "clean")


def _placeholder(name: str):
    def _run(_args: argparse.Namespace) -> int:
        print(f"undrudge {name}: not implemented yet (Phase {_PHASE_OF[name]})",
              file=sys.stderr)
        return 2
    return _run


_PHASE_OF: dict[str, int] = {}


def _parse_since(spec: str | None) -> int | None:
    """Accept a relative spec ('7d', '24h') or absolute YYYY-MM-DD."""
    if not spec:
        return None
    s = spec.strip().lower()
    if s.endswith("h"):
        return store.now_ms() - int(s[:-1]) * 3600 * 1000
    if s.endswith("d"):
        return store.now_ms() - int(s[:-1]) * 86400 * 1000
    if s.endswith("w"):
        return store.now_ms() - int(s[:-1]) * 7 * 86400 * 1000
    try:
        day = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)
        return int(day.timestamp() * 1000)
    except ValueError as e:
        raise SystemExit(f"--since: {e}") from None


def _cmd_list(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = store.open_db(cfg.paths.db)
    try:
        store.apply_schema(conn)
        rows = recommend.list_recs(
            conn,
            status=args.status,
            since_ms=_parse_since(args.since),
            scope=args.scope,
            limit=args.limit,
        )
    finally:
        conn.close()

    if not rows:
        print("(no recommendations)")
        return 0

    for r in rows:
        ts = datetime.fromtimestamp(r["created_at"] / 1000, tz=UTC)
        date = ts.strftime("%Y-%m-%d")
        short = r["id"][:12]
        status = f"{r['status']:11}"
        scope = f"{r['scope']:6}"
        title = r["title"][:80]
        print(f"{date}  {short}  {status} {scope} {title}")
    return 0


def _cmd_dismiss(args: argparse.Namespace) -> int:
    return _cmd_set_status(args, new_status="dismissed")


def _cmd_implement(args: argparse.Namespace) -> int:
    return _cmd_set_status(args, new_status="implemented")


def _cmd_mark(args: argparse.Namespace) -> int:
    """General status setter — covers dispatched/rejected (and any valid
    status) that don't have a dedicated verb. The dispatch pipeline uses
    this to mark recs dispatched/rejected with a reason.
    """
    return _cmd_set_status(args, new_status=args.status)


def _cmd_show(args: argparse.Namespace) -> int:
    """Print the absolute path of the rec's markdown file. Cmd+click in
    iTerm2 (or pipe to your viewer of choice) takes it from there."""
    cfg = config.load()
    conn = store.open_db(cfg.paths.db)
    try:
        store.apply_schema(conn)
        matches = recommend.find_by_id_prefix(conn, args.id)
    finally:
        conn.close()

    if not matches:
        print(f"no recommendation matched id prefix {args.id!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        prefixes = [m["id"][:12] for m in matches]
        print(
            f"id prefix {args.id!r} is ambiguous "
            f"({len(matches)} matches: {prefixes})",
            file=sys.stderr,
        )
        return 1
    body_path = matches[0].get("body_path")
    if not body_path:
        print(
            f"recommendation {matches[0]['id'][:12]} has no body_path on disk",
            file=sys.stderr,
        )
        return 1
    print(body_path)
    return 0


def _cmd_set_status(args: argparse.Namespace, *, new_status: str) -> int:
    reason = getattr(args, "reason", None)
    cfg = config.load()
    conn = store.open_db(cfg.paths.db)
    try:
        store.apply_schema(conn)
        try:
            result = recommend.set_status(conn, args.id, new_status, reason=reason)
        except (LookupError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    finally:
        conn.close()

    if result.matched_id is None:
        print(f"no recommendation matches id prefix {args.id!r}", file=sys.stderr)
        return 1

    events.record(
        cfg.paths.events_log,
        f"rec_{new_status}",
        {
            "id": result.matched_id,
            "from_status": result.old_status,
            "reason": reason,
            "body_path": str(result.body_path) if result.body_path else None,
        },
    )
    print(f"{result.matched_id[:12]}: {result.old_status} → {result.new_status}")
    if reason:
        print(f"  reason: {reason}")
    if result.body_path:
        print(f"  {result.body_path}")
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    # Resolve preset -> defaults. Explicit --window / --meta override.
    meta = args.meta or (args.preset == "week")
    window = args.window or ("7d" if args.preset == "week" else "24h")

    cfg = config.load()
    conn = store.open_db(cfg.paths.db)
    try:
        store.apply_schema(conn)

        if args.prompt_only:
            digest_md = digest.render_daily(
                conn, window_hours=_parse_window(window)
            )
            recent = recommend.recent_logged(conn)
            recent_md = recommend.render_recent_for_prompt(recent)
            dismissed_md = recommend.render_dismissed_for_prompt(
                recommend.recent_dismissed(conn)
            )
            sys.stdout.write(
                analyze.build_prompt(
                    digest_md, recent_md,
                    scope="weekly" if meta else "daily",
                    dismissed_md=dismissed_md,
                )
            )
            return 0

        result = analyze.run(
            conn,
            cfg,
            window_hours=_parse_window(window),
            scope="weekly" if meta else "daily",
            dry_run=args.dry_run,
        )
    finally:
        conn.close()

    if result.workdir:
        print(f"workdir:        {result.workdir}")
    print(f"prompt size:    {len(result.prompt)} chars")
    print(f"response size:  {len(result.response)} chars")
    print(f"parsed:         {len(result.parsed)} recommendation(s)")
    print(f"written:        {len(result.written)}")
    if not args.dry_run:
        print(f"skipped (dup):  {result.skipped}")
    if result.refs_total:
        pct = 100 * result.refs_resolved // result.refs_total
        print(
            f"evidence refs:  {result.refs_resolved}/{result.refs_total} "
            f"resolved ({pct}%)"
        )
    for w in result.written:
        print(f"  - {w.path}")
    return 0


def _parse_window(spec: str) -> int:
    """Accept '24h', '7d', or a bare integer (= hours)."""
    s = spec.strip().lower()
    if s.endswith("h"):
        return int(s[:-1])
    if s.endswith("d"):
        return int(s[:-1]) * 24
    return int(s)


def _cmd_digest(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = store.open_db(cfg.paths.db)
    try:
        store.apply_schema(conn)
        if args.date:
            try:
                day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                print(f"bad --date {args.date!r} (expected YYYY-MM-DD)", file=sys.stderr)
                return 2
            end_ts_ms = int((day.replace(hour=23, minute=59, second=59)).timestamp() * 1000)
        else:
            end_ts_ms = store.now_ms()

        window_hours = _parse_window(args.window)
        md = digest.render_daily(conn, end_ts_ms=end_ts_ms, window_hours=window_hours)
    finally:
        conn.close()

    if args.out == "-":
        sys.stdout.write(md)
        return 0

    if args.out:
        out_path = Path(args.out).expanduser()
    else:
        end_dt = datetime.fromtimestamp(end_ts_ms / 1000, tz=UTC)
        out_path = cfg.paths.digests_dir / f"{end_dt.strftime('%Y-%m-%d')}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)
    print(out_path)
    return 0


def _cmd_gather(_args: argparse.Namespace) -> int:
    """Ingest Claude, Codex, and shell activity. Each source runs independently; one
    failing doesn't block the other. Failures are recorded as
    ``gather_failed`` events in events.jsonl so ``undrudge doctor`` can
    surface them — silent hourly failure is what let the WAL bug fester
    for weeks.
    """
    cfg = config.load()
    conn = store.open_db(cfg.paths.db)
    c: ingest_claude.ClaudeIngestStats | None = None
    x: ingest_codex.CodexIngestStats | None = None
    s: ingest_shell.ShellIngestStats | None = None
    p: prune.PruneStats | None = None
    prune_error: BaseException | None = None
    failures: list[tuple[str, BaseException]] = []
    gather_logger = logging.getLogger("undrudge.gather")
    try:
        store.apply_schema(conn)
        try:
            c = ingest_claude.ingest(
                conn, cfg.claude.projects_root, fail_loud=cfg.privacy.fail_loud
            )
        except Exception as e:
            failures.append(("claude", e))
            gather_logger.exception("claude ingest failed")
        if cfg.codex is not None:
            try:
                x = ingest_codex.ingest(
                    conn, cfg.codex.home, fail_loud=cfg.privacy.fail_loud
                )
            except Exception as e:
                failures.append(("codex", e))
                gather_logger.exception("codex ingest failed")
        try:
            s = ingest_shell.ingest(
                conn, cfg.atuin.db, fail_loud=cfg.privacy.fail_loud
            )
        except Exception as e:
            failures.append(("shell", e))
            gather_logger.exception("shell ingest failed")

        # Retention runs after ingest, capped so an unattended hourly job
        # never turns into a long delete. It is not a "source": a broken
        # prune must not make doctor report ingestion as failing, but it
        # still gets an event, a stderr line, and a non-zero exit.
        if cfg.retention.days > 0:
            try:
                p = prune.run(
                    conn,
                    cutoff_ms=prune.cutoff_for_days(cfg.retention.days),
                    max_rows=prune.GATHER_MAX_ROWS,
                )
            except Exception as e:
                prune_error = e
                gather_logger.exception("prune failed")
    finally:
        conn.close()

    if c is not None:
        print("claude:")
        print(f"  files seen          : {c.files_seen}")
        print(f"  files skipped       : {c.files_skipped}")
        print(f"  lines read          : {c.lines_read}")
        print(f"  lines skipped       : {c.lines_skipped}")
        print(f"  rows inserted       : {c.rows_inserted}")
        print(f"  sessions tracked    : {c.sessions_touched}")
        print(f"  bytes consumed      : {c.bytes_consumed}")
        print(f"  redaction drops     : {c.redaction_drops}")
        print()
    if x is not None:
        print("codex:")
        print(f"  files seen          : {x.files_seen}")
        print(f"  files skipped       : {x.files_skipped}")
        print(f"  lines read          : {x.lines_read}")
        print(f"  lines skipped       : {x.lines_skipped}")
        print(f"  rows inserted       : {x.rows_inserted}")
        print(f"  rows updated        : {x.rows_updated}")
        print(f"  sessions tracked    : {x.sessions_touched}")
        print(f"  bytes consumed      : {x.bytes_consumed}")
        print(f"  redaction drops     : {x.redaction_drops}")
        print()
    if s is not None:
        print("shell (atuin):")
        print(f"  rows seen           : {s.rows_seen}")
        print(f"  rows inserted       : {s.rows_inserted}")
        print(f"  rows dropped        : {s.rows_dropped}")
        print(f"  last ts (ns)        : {s.last_ts_ns}")

    if p is not None:
        print()
        _print_prune_stats(p, cfg.retention.days)
        events.record(
            cfg.paths.events_log,
            "prune_complete",
            {
                "cutoff_ms": p.cutoff_ms,
                "retention_days": cfg.retention.days,
                "messages": p.messages,
                "commands": p.commands,
                "sessions": p.sessions,
                "truncated": p.truncated,
            },
        )

    for source, exc in failures:
        print(
            f"\nFAILED: {source} ingest — {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        events.record(
            cfg.paths.events_log,
            "gather_failed",
            {
                "source": source,
                "error_type": type(exc).__name__,
                "error_msg": str(exc),
            },
        )

    # One terminal event per run with the per-source outcome. doctor reads
    # the *latest* gather_complete to decide health, so a transient failure
    # that self-heals on the next run no longer shows as a standing FAIL —
    # only a currently-broken source does.
    events.record(
        cfg.paths.events_log,
        "gather_complete",
        {
            "failed_sources": [src for src, _ in failures],
            "shell_rows": s.rows_inserted if s is not None else None,
            "claude_rows": c.rows_inserted if c is not None else None,
            "codex_rows": x.rows_inserted if x is not None else None,
        },
    )

    if prune_error is not None:
        print(
            f"\nFAILED: prune — {type(prune_error).__name__}: {prune_error}",
            file=sys.stderr,
        )
        events.record(
            cfg.paths.events_log,
            "prune_failed",
            {
                "error_type": type(prune_error).__name__,
                "error_msg": str(prune_error),
                "retention_days": cfg.retention.days,
            },
        )
    return 1 if (failures or prune_error is not None) else 0


def _print_prune_stats(p: prune.PruneStats, days: int, *, dry_run: bool = False) -> None:
    cutoff = datetime.fromtimestamp(p.cutoff_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")
    verb = "would delete" if dry_run else "deleted"
    print(f"retention ({days}d, cutoff {cutoff} UTC):")
    print(f"  messages {verb:14}: {p.messages:,}")
    print(f"  commands {verb:14}: {p.commands:,}")
    print(f"  sessions {verb:14}: {p.sessions:,}")
    if p.truncated:
        print("  more to prune       : yes — run again (or `undrudge prune`) to continue")


def _cmd_prune(args: argparse.Namespace) -> int:
    """Apply retention by hand: bigger batches than gather takes, an optional
    VACUUM, and a dry run that touches nothing."""
    cfg = config.load()
    days = args.days if args.days is not None else cfg.retention.days
    if days <= 0:
        print(
            "retention is disabled (days = 0). Pass --days N to prune anyway.",
            file=sys.stderr,
        )
        return 2

    conn = store.open_db(cfg.paths.db)
    before = prune.db_size_bytes(cfg.paths.db)
    try:
        store.apply_schema(conn)
        stats = prune.run(
            conn,
            cutoff_ms=prune.cutoff_for_days(days),
            dry_run=args.dry_run,
            max_rows=args.max_rows,
        )
        _print_prune_stats(stats, days, dry_run=args.dry_run)
        if args.vacuum and not args.dry_run:
            print("\ncompacting (FTS optimize + VACUUM) — this can take a while…")
            prune.compact(conn)
    finally:
        conn.close()

    after = prune.db_size_bytes(cfg.paths.db)
    print(f"\ndb size: {prune.human_bytes(before)} → {prune.human_bytes(after)}")
    if not args.dry_run and not args.vacuum and stats.rows:
        print("(freed pages stay in the file until `undrudge prune --vacuum`)")

    if not args.dry_run:
        events.record(
            cfg.paths.events_log,
            "prune_complete",
            {
                "cutoff_ms": stats.cutoff_ms,
                "retention_days": days,
                "messages": stats.messages,
                "commands": stats.commands,
                "sessions": stats.sessions,
                "truncated": stats.truncated,
                "vacuum": bool(args.vacuum),
                "bytes_before": before,
                "bytes_after": after,
            },
        )
    return 0


def _cmd_browse(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = store.open_db(cfg.paths.db)
    try:
        store.apply_schema(conn)
        filters = browse.Filters(
            status=args.status,
            since_ms=_parse_since(args.since),
            scope=args.scope,
            limit=args.limit,
        )
        return browse.run_picker(conn, cfg, filters)
    finally:
        conn.close()


def _cmd_dispatch(args: argparse.Namespace) -> int:
    from . import dispatch_run
    cfg = config.load()
    return dispatch_run.cmd_dispatch(cfg, subcmd=args.subcmd, dry=args.dry_run)


def _cmd_scrub(args: argparse.Namespace) -> int:
    """Re-apply current secret redaction over stored command text.

    Run after adding a secret pattern to the sanitizer so rows ingested
    before the pattern existed get cleaned without a re-ingest.
    """
    cfg = config.load()
    conn = store.open_db(cfg.paths.db)
    try:
        store.apply_schema(conn)
        stats = scrub.rescan_commands(conn, dry_run=args.dry_run)
    finally:
        conn.close()

    suffix = " (dry-run, not written)" if args.dry_run else ""
    print(f"commands scanned   : {stats.scanned}")
    print(f"commands rewritten : {stats.changed}{suffix}")
    if stats.changed and not args.dry_run:
        events.record(
            cfg.paths.events_log,
            "scrub_complete",
            {"commands_changed": stats.changed, "commands_scanned": stats.scanned},
        )
    return 0


# --------------------------------------------------------------------------
# Internal hooks the browse picker's fzf bindings call back into. Kept out of
# the argparse subcommand table so `undrudge --help` stays the user's surface;
# you never type these.
# --------------------------------------------------------------------------

_BROWSE_INTERNALS = (
    "__browse-list",
    "__browse-preview",
    "__browse-act",
    "__browse-copy",
    "__browse-flip",
    "__browse-help",
)


def _run_browse_internal(argv: list[str]) -> int:
    sub = argv[0]
    if sub == "__browse-help":
        sys.stdout.write(browse.KEYS_HELP)
        sys.stdout.write(
            "\n" + "─" * 52 + "\n"
            "  Press  q  to close this help and return to the picker.\n"
        )
        return 0

    ap = argparse.ArgumentParser(prog=f"undrudge {sub}")
    if sub == "__browse-list":
        ap.add_argument("--status")
        ap.add_argument("--since-ms", type=int, default=None)
        ap.add_argument("--scope")
        ap.add_argument("--limit", type=int, default=500)
    if sub in ("__browse-preview", "__browse-copy"):
        ap.add_argument("--id", required=True)
    if sub in ("__browse-preview", "__browse-flip"):
        ap.add_argument("--flag", required=True)
    if sub == "__browse-act":
        ap.add_argument("--action", required=True, choices=sorted(browse.ACTIONS))
        ap.add_argument("--rows", required=True)
    if sub == "__browse-copy":
        ap.add_argument("--what", required=True, choices=["body", "path"])
    args = ap.parse_args(argv[1:])

    if sub == "__browse-flip":
        flag = Path(args.flag)
        current = ""
        with contextlib.suppress(OSError):
            current = flag.read_text(encoding="utf-8").strip()
        flag.write_text("trail" if current != "trail" else "rec", encoding="utf-8")
        return 0

    cfg = config.load()
    # No apply_schema here: these run on every keystroke of an already-running
    # picker, against a db the parent process has already migrated.
    conn = store.open_db(cfg.paths.db)
    try:
        if sub == "__browse-list":
            filters = browse.Filters(
                status=args.status,
                since_ms=args.since_ms,
                scope=args.scope,
                limit=args.limit,
            )
            sys.stdout.write(browse.render_rows(browse.fetch_rows(conn, filters)))
            return 0

        if sub == "__browse-preview":
            mode = "rec"
            with contextlib.suppress(OSError):
                mode = Path(args.flag).read_text(encoding="utf-8").strip() or "rec"
            sys.stdout.write(
                browse.preview_markdown(
                    conn, args.id, mode=mode, events_log=cfg.paths.events_log
                )
            )
            return 0

        if sub == "__browse-copy":
            row = conn.execute(
                "SELECT body_path FROM recommendations WHERE id = ?", (args.id,)
            ).fetchone()
            if row is None or not row["body_path"]:
                return 1
            if args.what == "path":
                payload = str(row["body_path"])
            else:
                _, payload = recommend.parse_rec_file(row["body_path"])
            return 0 if browse.copy_to_clipboard(payload) else 1

        return _browse_act(conn, cfg, args.action, args.rows)
    finally:
        conn.close()


def _browse_act(conn, cfg: config.Config, action: str, rows_file: str) -> int:
    ids = browse.ids_from_rows_file(rows_file)
    if not ids:
        return 0
    _, needs_reason = browse.ACTIONS[action]
    reason = None
    if needs_reason:
        titles = []
        for rec_id in ids:
            row = conn.execute(
                "SELECT title FROM recommendations WHERE id = ?", (rec_id,)
            ).fetchone()
            titles.append(row["title"] if row else rec_id[:12])
        proceed, reason = browse.prompt_reason(action, titles)
        if not proceed:
            return 0
    for line in browse.apply_action(conn, cfg, ids, action, reason=reason):
        print(line)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="undrudge",
        description="Background watchman that finds what to automate.",
    )
    p.add_argument("--version", action="version", version=f"undrudge {__version__}")
    p.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Narrate to stderr. -v = INFO, -vv = DEBUG. Default is silent.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Create config, dirs, and db.")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing config.")
    p_init.set_defaults(func=_cmd_init)

    p_doctor = sub.add_parser("doctor", help="Check paths, histories, atuin, and LLM CLI.")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_gather = sub.add_parser("gather", help="Ingest Claude, Codex, and shell activity.")
    p_gather.set_defaults(func=_cmd_gather)

    p_dispatch = sub.add_parser(
        "dispatch",
        help="Route logged recs to repo clones as briefs (needs a "
             "[dispatch] config section).",
    )
    p_dispatch.add_argument(
        "subcmd", nargs="?", default="run", choices=["run", "status"],
        help="run = dispatch + reconcile (default); status = show logged recs.",
    )
    p_dispatch.add_argument(
        "--dry-run", action="store_true",
        help="Compute and print the report without writing briefs, "
             "flipping statuses, or touching the vault.",
    )
    p_dispatch.set_defaults(func=_cmd_dispatch)

    p_scrub = sub.add_parser(
        "scrub",
        help="Re-apply secret redaction to stored command text "
             "(run after adding a sanitizer pattern).",
    )
    p_scrub.add_argument(
        "--dry-run", action="store_true",
        help="Report how many rows would change; don't write.",
    )
    p_scrub.set_defaults(func=_cmd_scrub)

    p_digest = sub.add_parser(
        "digest", help="Render activity digest for inspection or LLM input.",
    )
    p_digest.add_argument(
        "--window", default="24h",
        help="Trailing window: e.g. 24h, 7d. Default 24h.",
    )
    p_digest.add_argument(
        "--date", default=None,
        help="End date in YYYY-MM-DD (UTC). Default: now.",
    )
    p_digest.add_argument(
        "--out", default=None,
        help="Output path. '-' for stdout. Default: <digests_dir>/YYYY-MM-DD.md",
    )
    p_digest.set_defaults(func=_cmd_digest)

    p_analyze = sub.add_parser(
        "analyze",
        help="Render digest, call claude -p, write recommendations.",
    )
    p_analyze.add_argument(
        "preset", nargs="?", choices=["day", "week"], default=None,
        help="Convenience preset. 'day' = 24h trailing, regular daily run. "
             "'week' = 7d trailing, --meta. Explicit flags override.",
    )
    p_analyze.add_argument("--meta", action="store_true",
                           help="Weekly meta-analysis (scope=weekly).")
    p_analyze.add_argument("--window", default=None,
                           help="Trailing window: e.g. 24h, 7d. Default: 24h "
                                "(or 7d when preset=week).")
    p_analyze.add_argument("--dry-run", action="store_true",
                           help="Call the LLM but write to dry-run/ instead of recs_dir; "
                                "skip DB rows and the on_write hook.")
    p_analyze.add_argument("--prompt-only", action="store_true",
                           help="Print the assembled prompt to stdout and exit; no LLM call.")
    p_analyze.set_defaults(func=_cmd_analyze)

    p_list = sub.add_parser("list", help="Show recommendations.")
    p_list.add_argument("--since", default=None,
                        help="Show recs newer than this (e.g. 7d, 24h, 2026-05-01).")
    p_list.add_argument(
        "--status", default=None,
        choices=["logged", "dismissed", "implemented", "dispatched", "rejected"],
    )
    p_list.add_argument("--scope", default=None,
                        choices=["daily", "weekly"])
    p_list.add_argument("--limit", default=200, type=int)
    p_list.set_defaults(func=_cmd_list)

    p_browse = sub.add_parser(
        "browse",
        help="Triage recommendations in an fzf picker (needs fzf; glow optional).",
    )
    p_browse.add_argument("--since", default=None,
                          help="Only recs newer than this (e.g. 7d, 24h, 2026-05-01).")
    p_browse.add_argument(
        "--status", default=None,
        choices=["logged", "dismissed", "implemented", "dispatched", "rejected"],
        help="Only this status. Default: everything, open recs first.",
    )
    p_browse.add_argument("--scope", default=None, choices=["daily", "weekly"])
    p_browse.add_argument("--limit", default=500, type=int)
    p_browse.set_defaults(func=_cmd_browse)

    p_prune = sub.add_parser(
        "prune",
        help="Delete ingested rows older than the retention window.",
    )
    p_prune.add_argument(
        "--days", type=int, default=None,
        help="Retention window in days. Default: [retention].days from config.",
    )
    p_prune.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be deleted; write nothing.",
    )
    p_prune.add_argument(
        "--vacuum", action="store_true",
        help="Also optimize the FTS indexes and VACUUM, handing freed pages "
             "back to the filesystem. Slow, and needs room for a second copy.",
    )
    p_prune.add_argument(
        "--max-rows", type=int, default=None,
        help="Stop after deleting this many rows (default: no cap).",
    )
    p_prune.set_defaults(func=_cmd_prune)

    p_dismiss = sub.add_parser("dismiss", help="Mark a recommendation dismissed.")
    p_dismiss.add_argument("id", help="Full id or unique prefix.")
    p_dismiss.add_argument(
        "--reason", default=None,
        help="Why it's being dismissed. Fed back into the analyze prompt so "
             "variants stop getting re-proposed.",
    )
    p_dismiss.set_defaults(func=_cmd_dismiss)

    p_implement = sub.add_parser("implement", help="Mark a recommendation implemented.")
    p_implement.add_argument("id", help="Full id or unique prefix.")
    p_implement.add_argument(
        "--reason", default=None, help="Optional note recorded with the status flip.",
    )
    p_implement.set_defaults(func=_cmd_implement)

    p_mark = sub.add_parser(
        "mark",
        help="Set a recommendation's status to any valid value "
             "(dispatched, rejected, etc.).",
    )
    p_mark.add_argument("id", help="Full id or unique prefix.")
    p_mark.add_argument(
        "status",
        choices=["logged", "dismissed", "implemented", "dispatched", "rejected"],
        help="New status.",
    )
    p_mark.add_argument(
        "--reason", default=None,
        help="Why. Persisted to frontmatter + events.jsonl; negative "
             "statuses feed the analyze prompt's do-not-re-propose section.",
    )
    p_mark.set_defaults(func=_cmd_mark)

    p_show = sub.add_parser(
        "show",
        help="Print the absolute path of a rec's markdown file.",
    )
    p_show.add_argument("id", help="Full id or unique prefix.")
    p_show.set_defaults(func=_cmd_show)

    return p


def main(argv: list[str] | None = None) -> int:
    args_in = sys.argv[1:] if argv is None else argv
    if args_in and args_in[0] in _BROWSE_INTERNALS:
        return _run_browse_internal(args_in)
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
