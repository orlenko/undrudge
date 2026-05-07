# undrudge

<img width="1448" height="1086" alt="image" src="https://github.com/user-attachments/assets/c4ec31ab-4479-4eb9-ac09-36f255f9e0f7" />

A background watchman that watches what you do in Claude Code and your
shell, finds the parts that look like muscle memory, and writes
recommendations to disk for you to read at your leisure.

I built undrudge for myself, after noticing that I kept retyping the
same shell compounds and pasting the same prompts across sessions. It
runs on cron, reads my Claude session JSONL and atuin shell history,
sanitizes everything before storage, and once a day asks `claude -p`
"what did I do repeatedly that a script could have done?" The answers
land as markdown files I read during my normal review.

It's not a daemon, not a UI, not real-time. Three cron jobs, one
SQLite file, markdown out. Privacy is sanitize-on-ingest with a golden
test that fails loud if any planted secret survives.

## Install

```bash
git clone https://github.com/orlenko/undrudge.git
cd undrudge

# install on PATH
uv tool install .

# or run from source
uv run undrudge --help
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/) (a single
Rust binary).

## Setup

```bash
undrudge init                # create ~/.config/undrudge/config.toml,
                             # ~/.local/share/undrudge/, and the SQLite db
undrudge doctor              # sanity-check paths, atuin, claude CLI
```

Edit `~/.config/undrudge/config.toml` to point at your atuin DB and
Claude projects directory if they live somewhere unusual. The defaults
work on a stock macOS/Linux install.

## How it works

Three subcommands, one binary, no daemon.

```
                ~/.local/share/undrudge/undrudge.sqlite
              (sessions, messages, commands, recs, FTS5)
                            ▲
            ┌───────────────┼─────────────┐
            │               │             │
     hourly │        daily  │      weekly │ (or ad-hoc)
            ▼               ▼             ▼
       gather          analyze day    analyze week
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

```bash
undrudge gather              # ingest new Claude + shell activity (hourly)
undrudge analyze day         # daily digest → recs (24h trailing, regular)
undrudge analyze week        # weekly meta digest → recs (7d trailing, --meta)
undrudge analyze --dry-run day    # daily run, but write to dry-run/ (skip DB+hook)
undrudge digest --window 24h --out -   # render a digest only (no LLM call)
undrudge list                # show recommendations
undrudge dismiss <id>        # mark a rec dismissed (full id or any unique prefix)
undrudge implement <id>      # mark a rec implemented
```

`day` and `week` are convenience presets — they set `--window`/`--meta`
to the values the schedulers use. Either flag can override the preset
(e.g. `undrudge analyze week --window 14d`). Use `analyze` with no
preset to keep the previous default (24h regular).

If a scheduled run misses (laptop asleep, Claude API blip, you closed
the lid), running these by hand replays them. Recs are deduplicated by
fingerprint, so re-running is safe — duplicates just count as
`skipped (dup)` in the output.

A typical scheduler setup:

```
17 *  * * *   ~/.local/bin/undrudge gather
30 2  * * *   ~/.local/bin/undrudge analyze day
0  3  * * 0   ~/.local/bin/undrudge analyze week
```

### macOS: prefer launchd, or grant cron Full Disk Access

Cron on macOS will silently fail to read your atuin DB and Claude
projects directory unless `/usr/sbin/cron` has Full Disk Access — the
symptom is `sqlite3.OperationalError: unable to open database file`
even though the file is right there and owned by you. To grant it:
System Settings → Privacy & Security → Full Disk Access → `+` →
press `⌘⇧G` → `/usr/sbin/cron` → enable.

The cleaner option is launchd, which runs your job in your normal
user session and doesn't need a Full Disk Access grant. Templates
ship in `scripts/launchd/`:

```bash
./scripts/launchd/install.sh
```

This renders the three plists with your `$HOME`, copies them to
`~/Library/LaunchAgents/`, and bootstraps them via `launchctl`. Edit
the plists in the repo and re-run the script to change schedules.
Logs go to `~/.local/share/undrudge/logs/{gather,analyze,analyze-meta}.log`.
The script prints removal and on-demand-run commands at the end of a
successful install.

## Recommendation format

Each recommendation is a single markdown file with a JSON header:

````markdown
```json
{
  "id": "7f3a91...",
  "scope": "daily",
  "status": "logged",
  "created": "2026-05-04T02:30:00Z",
  "confidence": "high",
  "automation_form": "slash_command",
  "signature": "find . -name <str> | xargs grep <str>",
  "evidence": [...]
}
```

# Wrap repeated find/xargs grep into a slash command

You ran the same `find . -name '*.py' | xargs grep ...` skeleton 7 times
across 3 sessions this week. ...
````

To dismiss: edit the frontmatter `status: dismissed`, or run `undrudge
dismiss <id>`.

## Privacy

Sanitization runs at ingest, before any row hits the SQLite. There is
no path in the system that stores unredacted text. Four mechanisms:

- Pattern matching for common API keys, tokens, JWTs, password
  assignments, connection strings, private keys.
- Path exclusion for `.env`, `.pem`, `.key`, `id_rsa*`, kubeconfigs,
  etc. — the path is stored, the content never is.
- Shannon-entropy detection for random-looking strings the patterns
  missed.
- Contextual suppression near phrases like "here is the token" /
  "my api key is".

A parametrized test plants every supported secret type in a fixture and
asserts none of them survive ingest. CI fails loud if anything leaks.

If your environment uses a secret type that isn't covered, add a
pattern to `src/undrudge/sanitize.py:SECRET_PATTERNS` and a test case
to `tests/fixtures/secrets.txt` *before* running `undrudge gather`
against real data.

## Sandboxed Claude (optional)

`[llm].command` in the config points at whatever wraps `claude -p` for
you. The default `@bundled` resolves to `scripts/claude-sandboxed.sh`,
which runs claude under [`nono`](https://github.com/always-further/nono)
if installed and falls through to bare `claude` if not. Override with
an absolute path to your own wrapper, or with `claude` to disable
wrapping entirely.

If you do use the bundled wrapper with nono ≥ 0.48, install the
`claude` pack once:

```bash
nono pull always-further/claude
```

The wrapper invokes `--profile claude`. Older nono (< 0.48) shipped
this profile as the built-in `claude-code` and didn't need a pack
pull. The pack is also where the inter-process lock file
`~/.claude.lock` is granted; if you ever see a nono "Refusing to grant"
error on that path, the pack is missing or out of date.

## Dev shortcuts

```bash
./scripts/dev-test.sh              # uv-cache-aware pytest, last 50 lines
./scripts/dev-test.sh -v           # verbose
TAIL_N=200 ./scripts/dev-test.sh   # more output

./scripts/dev-fresh.sh             # wipe scratch dirs, init a clean sandbox
./scripts/dev-fresh.sh doctor      # ...then run doctor in that sandbox
./scripts/dev-fresh.sh gather
./scripts/dev-fresh.sh analyze --dry-run day
```

Both scripts started life as recommendations undrudge made about its
own development workflow on early dogfood runs, then implemented. The
loop closes.

## Design

Full design notes and rationale: [docs/design.md](docs/design.md).

## License

MIT — see [LICENSE](LICENSE).
