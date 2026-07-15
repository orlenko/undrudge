# undrudge — original v1 plan

> Historical rationale, not a complete description of current behavior. Use
> `README.md`, current source, and tests for the operator contract. Later work
> added Codex ingestion, dispatch/status flows, and other commands without
> rewriting this plan wholesale.

A retrospective tool that watches my Claude Code, Codex, and shell history,
finds patterns worth automating, and writes recommendations as markdown files.
Daily detection, weekly meta-analysis, no daemon, no UI, no real-time, no
external services.

## Outcome

When I do something tedious enough times that a script could do it, undrudge
writes a markdown recommendation under
`~/.local/share/undrudge/recommendations/`. I read it during my normal review,
implement what's worth implementing, dismiss what isn't. After two weeks of
running, the directory contains a few real wins per week.

## Stack

**Python 3.12+ with `uv`.** Installed via `uv tool install ./undrudge`, which
puts the CLI on PATH with its own managed venv. No pip rituals, no global
Python pollution, portable to any machine that has uv (a single Rust binary).

Standard library does most of the work: `sqlite3` (with FTS5), `tomllib`,
`argparse`, `subprocess`, `re`, `hashlib`, `pathlib`. Third-party deps only
when there's a clear win. Tests with pytest. Lint with ruff.

Why not Go: better deployment story but worse iteration cost; this is three
cron jobs that each run for seconds, not a long-lived daemon. Python is in
its element here.

Why not TypeScript: dependency drift on long-lived background tooling is
worse than Python.

Why not Rust: overkill for I/O-bound text munging.

## Architecture

```
                ~/.local/share/undrudge/undrudge.sqlite
              (sessions, messages, commands, recs, FTS5)
                            ▲
            ┌───────────────┼─────────────┐
            │               │             │
     hourly │        daily  │      weekly │ (or ad-hoc)
            ▼               ▼             ▼
       gather          analyze        analyze --meta
            │               │             │
            ▼               ▼             ▼
    ingest+sanitize    digest + claude -p + dedupe
                            │
                            ▼
       ~/.local/share/undrudge/recommendations/<date>/<id>.md
                            │
                            ▼
            optional [output.on_write] hook fires
```

One binary, six subcommands:

| Subcommand            | What it does                                                              |
|-----------------------|---------------------------------------------------------------------------|
| `undrudge init`       | Create XDG dirs, write default config, apply schema.                      |
| `undrudge gather`     | Ingest new Claude/Codex messages and shell commands. Idempotent. **Hourly.** |
| `undrudge analyze`    | Render digest, call `claude -p`, write recommendations. **Daily.**         |
| `undrudge analyze --meta --window 7d` | Same code path; reads daily digests instead of raw activity. **Weekly.** |
| `undrudge list`       | Print recommendations index (filter by `--since`, `--status`).             |
| `undrudge dismiss <id>` | Mark a recommendation dismissed.                                         |
| `undrudge doctor`     | Sanity-check paths, atuin, claude CLI, last-run age, redaction failures.   |

Three cron lines, one binary.

## File layout (XDG)

```
~/.config/undrudge/
  config.toml

~/.local/share/undrudge/
  undrudge.sqlite
  digests/
    2026-05-04.md
  recommendations/
    2026-05-04/
      001-wrap-find-grep-into-slash.md
      002-shell-aliases-for-frequent-flow.md
    2026-W18-weekly.md
    index.jsonl
  logs/
    gather.log
    analyze.log
```

Project source (this repo):

```
misc/undrudge/
  pyproject.toml
  src/undrudge/
    __init__.py
    cli.py
    config.py
    store.py
    schema.sql
    sanitize.py
    ingest_claude.py
    ingest_codex.py
    ingest_shell.py
    digest.py
    analyze.py
    recommend.py
    prompts/
      analyze.md
      analyze_meta.md
  tests/
    fixtures/
      jsonl/
      atuin.db
      secrets.txt
    test_sanitize.py
    test_ingest.py
    ...
```

## Schema

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE sessions (
  id          TEXT PRIMARY KEY,
  project     TEXT,
  started_at  INTEGER,
  last_seen   INTEGER
);

CREATE TABLE messages (
  id          TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(id),
  seq         INTEGER NOT NULL,
  ts          INTEGER NOT NULL,
  role        TEXT NOT NULL,         -- 'user' | 'assistant' | 'tool'
  text        TEXT,                  -- sanitized
  tool_name   TEXT,
  tool_input  TEXT,                  -- JSON, sanitized
  tool_result TEXT,                  -- JSON or text, sanitized
  is_error    INTEGER DEFAULT 0
);
CREATE INDEX idx_messages_ts ON messages(ts);
CREATE INDEX idx_messages_session ON messages(session_id, seq);

CREATE VIRTUAL TABLE messages_fts USING fts5(
  text, content='messages', content_rowid='rowid', tokenize='porter unicode61'
);
-- triggers keep FTS in sync

CREATE TABLE commands (
  id            INTEGER PRIMARY KEY,
  source        TEXT NOT NULL,       -- 'atuin' | 'zsh_history'
  external_id   TEXT,
  ts            INTEGER NOT NULL,
  shell         TEXT,
  cwd           TEXT,
  hostname      TEXT,
  command       TEXT NOT NULL,       -- sanitized
  exit_status   INTEGER,
  duration_ms   INTEGER,
  UNIQUE(source, external_id)
);
CREATE INDEX idx_commands_ts ON commands(ts);

CREATE VIRTUAL TABLE commands_fts USING fts5(
  command, content='commands', content_rowid='id', tokenize='porter unicode61'
);

CREATE TABLE recommendations (
  id           TEXT PRIMARY KEY,     -- sha256(scope + signature)
  scope        TEXT NOT NULL,        -- 'daily' | 'weekly'
  title        TEXT NOT NULL,
  signature    TEXT NOT NULL,
  body_path    TEXT NOT NULL,
  evidence     TEXT NOT NULL,        -- JSON array of refs
  status       TEXT NOT NULL DEFAULT 'logged',
                                     -- 'logged' | 'dismissed' | 'implemented'
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);
CREATE INDEX idx_recs_status ON recommendations(status, created_at DESC);

CREATE TABLE cursors (
  source       TEXT PRIMARY KEY,     -- 'claude:<jsonl_path>' | 'atuin' | etc.
  position     TEXT NOT NULL,        -- JSON
  updated_at   INTEGER NOT NULL
);

CREATE TABLE redaction_failures (
  id           INTEGER PRIMARY KEY,
  ts           INTEGER NOT NULL,
  source       TEXT NOT NULL,
  reason       TEXT NOT NULL
);
```

Removed from the v1 plan:

- **`correlations` table.** The 4-classification deterministic logic was
  over-engineering; the LLM does this better given a well-rendered digest.
- **`last_run` table.** Per-source `cursors` cover idempotent resume; cron
  timing is observable from logs and `doctor`.
- **`tool_registry.json`.** Deferred until evidence shows daily recs are
  missing context.

## Privacy filter

Self-contained, no external runtime dependencies:

- **Patterns**: AWS access/secret keys, GitHub PATs, Anthropic and OpenAI
  keys, Slack tokens, JWTs, bearer tokens, password assignments, connection
  strings, private keys.
- **Path exclusion**: Read/Write/Edit tool calls touching `.env`, `.pem`,
  `.key`, `id_rsa*`, kubeconfigs, `secrets.{json,yml,toml}`, `token.json`,
  etc. — keep the path, drop the content.
- **Entropy detection**: Shannon entropy threshold flags random-looking
  strings that don't match a known pattern.
- **Contextual suppression**: lines near markers like "here is the token" /
  "my api key is" get redacted along with the next line.

Sanitization happens **at ingest, before any row hits the DB**. There is no
code path that stores unredacted text. If `sanitize()` raises, the row is
dropped (logged to `redaction_failures`, not stored).

**Golden privacy test** (Phase 0, non-negotiable):

- `tests/fixtures/secrets.txt` contains every pattern.
- A fixture JSONL session embeds those secrets.
- `pytest` ingests the fixture and asserts:
  1. Zero substring matches of any planted secret in any DB column.
  2. One `redaction_failures` or in-line redaction marker per planted secret.
- CI fails loud if anything leaks. This is the only failure mode the plan
  treats as catastrophic.

## Recommendation format

One markdown file per recommendation:

```markdown
---
id: 7f3a91b8c4d2...
scope: daily
status: logged
created: 2026-05-04T02:30:00Z
confidence: high
automation_form: slash_command
signature: "find . -name '*' | xargs grep"
evidence:
  - {kind: message, session_id: "...", message_id: "abc...", ts: 1714000000}
  - {kind: command, command_id: 4123, ts: 1714000300}
---

# Wrap repeated find/xargs grep into a slash command

You ran the same `find . -name '*.py' | xargs grep ...` skeleton 7 times across
3 sessions this week. A `/grep-py` command (or shell alias) cuts this to one
keystroke.

**Suggested form:**
- Slash command at `~/.claude/commands/grep-py.md`, takes a pattern arg.
- Or shell alias: `alias gpy='find . -name "*.py" -print0 | xargs -0 grep -n'`

**Why this is automatable:** the structure is fixed; only the search term
varies.

**Evidence:** 7 invocations in sessions {…} between {…} and {…}.
```

`id` is `sha256(scope + signature)` — the LLM emits the normalized signature
explicitly. Reruns dedupe correctly even when wording drifts. `index.jsonl`
mirrors the recommendations table for fast grep-friendly browsing.

To dismiss: edit the frontmatter `status: dismissed`, or run
`undrudge dismiss <id>`. The tool reconciles on next run.

## Hook seam

```toml
[output]
on_write = "/Users/vlad/.local/bin/undrudge-handoff"
```

Fires per newly written `.md` file with the absolute path as `$1`. Errors are
logged but never block. Wire to anything: a personal devlog command, scp to
another host, `git commit`, or nothing at all. The tool knows about no
specific downstream consumer.

## Config (`config.toml`)

```toml
[paths]
db          = "~/.local/share/undrudge/undrudge.sqlite"
recs_dir    = "~/.local/share/undrudge/recommendations"
digests_dir = "~/.local/share/undrudge/digests"

[claude]
projects_root = "~/.claude/projects"

[codex]
home = "~/.codex"  # scans sessions/ and archived_sessions/

[atuin]
db = "~/.local/share/atuin/history.db"

[llm]
# `@bundled` (default) → use the claude-sandboxed.sh wrapper shipped inside
# the package. Wraps claude under `nono` if installed; falls through to bare
# `claude` otherwise. Override with an absolute path or "claude" to disable.
command         = "@bundled"
model           = "claude-sonnet-4-6"
max_tokens      = 8000
timeout_seconds = 120

[output]
on_write = ""                            # optional shell command, $1 = path

[privacy]
fail_loud = true                         # raise → drop row + log
```

## Phases

Each phase is a vertical slice with tests; we land it before moving on.

- **Phase 0 — scaffold + sanitizer.** `pyproject.toml`, CLI skeleton with
  `init` and `doctor`, full sanitizer port, **golden privacy test passing**.
- **Phase 1 — ingest.** `ingest_claude.py`, `ingest_shell.py`, `gather`
  subcommand. End-to-end test on fixture JSONL + fixture atuin DB.
- **Phase 2 — digest.** `digest.py` rendering DB → markdown. Inspect the
  output by hand on real data.
- **Phase 3 — analyze (daily).** Prompt + `analyze` subcommand +
  recommendation writer. Dry-run mode logs to
  `~/.local/share/undrudge/dry-run/` until the prompts feel right; flip to
  real on user say-so.
- **Phase 4 — meta (weekly).** `analyze --meta --window 7d`. Reads digests,
  not raw activity.
- **Phase 5 — schedule.** Crontab. Watch for a week. Tune.

Cron entries (Phase 5):

```
17 *  * * *   ~/.local/bin/undrudge gather                     >> ~/.local/share/undrudge/logs/gather.log 2>&1
30 2  * * *   ~/.local/bin/undrudge analyze                    >> ~/.local/share/undrudge/logs/analyze.log 2>&1
0  3  * * 0   ~/.local/bin/undrudge analyze --meta --window 7d >> ~/.local/share/undrudge/logs/analyze.log 2>&1
```

`launchd` plists in `scripts/launchd/` are an optional Phase 5b for
laptop-friendly catch-up scheduling.

## Non-goals (v1)

- Real-time intervention.
- Web UI / browser.
- MCP server exposing search.
- Additional agent-history providers (Cursor, Gemini) — defer until their
  share rises.
- Active tool registry / capability tagging.
- Automatic implementation of recommendations — human in the loop indefinitely.
- Cross-machine sync — recommendations and DB are per-machine. Sync, if ever
  wanted, is a user choice (rsync/syncthing/git), not a built-in.

## Notes for the implementing agent

- **Atuin's DB is read-only from undrudge's side.** Never write to it. Open
  with `sqlite3.connect(..., uri=True)` and the URI flag `mode=ro`.
- **Claude JSONL is append-only.** Cursors store byte offsets per file path.
  A file truncating or shrinking is a signal to reset that file's cursor.
- **The privacy filter is self-contained.** All rules live in
  `src/undrudge/sanitize.py`; if your environment has a secret type that
  isn't covered, add the regex to ``SECRET_PATTERNS`` and a test case to
  `tests/test_sanitize.py` before running against real data.
- **uv invocation example**: `uv run undrudge gather` from repo root, or
  `uv tool install .` then `undrudge gather`.
- **Bundled `claude-sandboxed.sh`** lives at
  `src/undrudge/scripts/claude-sandboxed.sh`. The `[llm].command` config
  defaults to the sentinel `@bundled` which `undrudge.llm.resolve_command`
  turns into the on-disk path. The script auto-detects `nono`; if it's
  absent, claude runs unwrapped. No external repos required.
