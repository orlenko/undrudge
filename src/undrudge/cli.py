"""undrudge CLI."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import (
    __version__,
    analyze,
    config,
    digest,
    ingest_claude,
    ingest_shell,
    llm,
    recommend,
    store,
)


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
    check("claude projects root", cfg.claude.projects_root.exists(),
          str(cfg.claude.projects_root))
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
            conn.close()
        except sqlite3.DatabaseError as e:
            check("db opens", False, str(e))

    return 0 if ok else 1


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
    cfg = config.load()
    conn = store.open_db(cfg.paths.db)
    try:
        store.apply_schema(conn)
        try:
            result = recommend.set_status(conn, args.id, new_status)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    finally:
        conn.close()

    if result.matched_id is None:
        print(f"no recommendation matches id prefix {args.id!r}", file=sys.stderr)
        return 1

    print(f"{result.matched_id[:12]}: {result.old_status} → {result.new_status}")
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
            sys.stdout.write(analyze.build_prompt(digest_md, recent_md))
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
    cfg = config.load()
    conn = store.open_db(cfg.paths.db)
    try:
        store.apply_schema(conn)
        c = ingest_claude.ingest(
            conn, cfg.claude.projects_root, fail_loud=cfg.privacy.fail_loud
        )
        s = ingest_shell.ingest(
            conn, cfg.atuin.db, fail_loud=cfg.privacy.fail_loud
        )
    finally:
        conn.close()

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
    print("shell (atuin):")
    print(f"  rows seen           : {s.rows_seen}")
    print(f"  rows inserted       : {s.rows_inserted}")
    print(f"  rows dropped        : {s.rows_dropped}")
    print(f"  last ts (ns)        : {s.last_ts_ns}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="undrudge",
        description="Background watchman that finds what to automate.",
    )
    p.add_argument("--version", action="version", version=f"undrudge {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Create config, dirs, and db.")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing config.")
    p_init.set_defaults(func=_cmd_init)

    p_doctor = sub.add_parser("doctor", help="Check paths, atuin, claude CLI.")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_gather = sub.add_parser("gather", help="Ingest Claude + shell activity.")
    p_gather.set_defaults(func=_cmd_gather)

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
    p_list.add_argument("--status", default=None,
                        choices=["logged", "dismissed", "implemented"])
    p_list.add_argument("--scope", default=None,
                        choices=["daily", "weekly"])
    p_list.add_argument("--limit", default=200, type=int)
    p_list.set_defaults(func=_cmd_list)

    p_dismiss = sub.add_parser("dismiss", help="Mark a recommendation dismissed.")
    p_dismiss.add_argument("id", help="Full id or unique prefix.")
    p_dismiss.set_defaults(func=_cmd_dismiss)

    p_implement = sub.add_parser("implement", help="Mark a recommendation implemented.")
    p_implement.add_argument("id", help="Full id or unique prefix.")
    p_implement.set_defaults(func=_cmd_implement)

    p_show = sub.add_parser(
        "show",
        help="Print the absolute path of a rec's markdown file.",
    )
    p_show.add_argument("id", help="Full id or unique prefix.")
    p_show.set_defaults(func=_cmd_show)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
