# undrudge

<img width="1448" height="1086" alt="rozmarujennya" src="https://github.com/user-attachments/assets/57259488-e837-4ae9-96f9-fe5aff6fb96f" />


A background watchman that watches what you do in Claude Code, Codex, and your
shell, spots the chores you keep redoing by hand instead of factoring
out, and writes recommendations to disk for you to read at your leisure.

The point isn't to surface things you're good at — it's to surface
things you (or your agents) are quietly *suffering*: the same shell
compound retyped seven times this week, the same prompt skeleton
pasted across sessions, the same six-line workaround for a missing
helper. Carrying the same box from table to shelf twenty times a day
when you could just rearrange the room.

I built undrudge for myself, after noticing that I kept retyping the
same shell compounds and pasting the same prompts across sessions. It
runs on cron, reads my Claude and Codex session JSONL plus atuin shell history,
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
Rust binary). No Python dependencies. `undrudge browse` additionally
wants [`fzf`](https://github.com/junegunn/fzf) (required) and
[`glow`](https://github.com/charmbracelet/glow) (optional, for rendered
previews); every other command runs without them.

## Setup

```bash
undrudge init                # create ~/.config/undrudge/config.toml,
                             # ~/.local/share/undrudge/, and the SQLite db
undrudge doctor              # sanity-check histories, atuin, and the LLM CLI
```

Edit `~/.config/undrudge/config.toml` to point at your atuin DB, Claude
projects directory, and Codex home if they live somewhere unusual. The Codex
default follows `$CODEX_HOME` when set and otherwise uses `~/.codex`; both
active and archived rollout sessions are scanned.

## How it works

Three scheduled jobs, one binary, no daemon.

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
undrudge gather              # ingest new Claude + Codex + shell activity (hourly)
undrudge analyze day         # daily digest → recs (24h trailing, regular)
undrudge analyze week        # weekly meta digest → recs (7d trailing, --meta)
undrudge analyze --dry-run day    # daily run, but write to dry-run/ (skip DB+hook)
undrudge digest --window 24h --out -   # render a digest only (no LLM call)
undrudge list                # show recommendations
undrudge here                # open recs belonging to the repo you're in (see below)
undrudge browse              # triage them in a picker (fzf; see below)
undrudge copy [<id>]         # rec → clipboard, ready to paste into an agent chat
undrudge dismiss <id> [--reason ...]    # mark a rec dismissed (full id or unique prefix)
undrudge implement <id> [--reason ...]  # mark a rec implemented
undrudge mark <id> <status> [--reason ...]   # set any status: dispatched, rejected, …
undrudge prune [--dry-run] [--vacuum]   # apply retention to the db (see below)
undrudge dispatch run [--dry-run]    # route logged recs to repo clones as briefs (see below)
undrudge dispatch status             # show logged recs + their dispatch state
undrudge capabilities [--show] [--force]  # installed-capability inventory (see below)
```

A recommendation moves through these statuses: `logged` (freshly
surfaced) → `implemented` / `dispatched` (acted on) or `dismissed` /
`rejected` (declined). `--reason` records *why* a rec was declined; the
reason is written to the rec's frontmatter and the `events.jsonl` audit
trail, and — importantly — fed back into the analyzer. Recently
dismissed/rejected recs (with their reasons) are listed in the analyze
prompt under "do not re-propose variants", and the write-time dedupe
gate suppresses near-duplicates of them. So dismissing a rec with a
reason like *"we don't want a background daemon"* stops the analyzer
from re-proposing rephrasings of that idea.

`day` and `week` are convenience presets — they set `--window`/`--meta`
to the values the schedulers use. Either flag can override the preset
(e.g. `undrudge analyze week --window 14d`). Use `analyze` with no
preset to keep the previous default (24h regular).

Missed runs heal themselves: each successful run records how far it
covered (a cursor row per scope), and the next run without an explicit
`--window` extends its window back to that point — capped at 7d for
daily and 14d for weekly runs. A laptop asleep at 02:30 costs nothing
but latency; the accumulated activity is analyzed on the next run that
does land. An explicit `--window` is used as-is and only advances the
coverage mark when it overlaps the previous one (so a short manual run
can't silently mark a gap as covered).

### Verbose runs

Every subcommand accepts a global `-v` / `-vv` before the command name
for interactive narration on stderr. Quiet by default (cron stays clean).

```bash
undrudge -v gather              # which files, how many lines/rows
undrudge -v analyze day         # prompt sizes, llm spawn, parse, per-rec writes
undrudge -vv analyze day        # plus poll-level DEBUG detail
```

Verbose is the *transient* "what's happening right now" channel. The
durable history channel is the JSONL audit trail at
`~/.local/share/undrudge/events.jsonl` — that records rec writes,
status changes, and analyze runs regardless of `-v`.

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

Cron on macOS will silently fail to read your atuin DB and agent-history
directories unless `/usr/sbin/cron` has Full Disk Access — the
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

## Browsing (`undrudge browse`)

`list` answers "what's there"; `browse` answers "what do I do about it".
It's an fzf picker over the same recs, files, and statuses — recs on the
left, the rendered rec on the right, and the actions that fit *that rec's
status* printed at the top of the preview.

```bash
undrudge browse                      # everything, open recs first
undrudge browse --status logged      # just the queue
undrudge browse --since 7d --scope weekly
```

```
  triage        ^A implement · ^D dismiss · ^X reject · ^L reopen
                (also ⌥a ⌥d ⌥x ⌥l, when the Ctrl chord is taken by tmux)
                Tab multi-selects; the action applies to the whole selection
  read          Enter / ^O open full-screen in less · ^T flip to the audit
                trail for that rec · Shift-↑/↓ scroll · ? toggle the pane
  yank          ^Y copy the rec as a hand-off · ^P copy its path
  navigate      ⌥g / ⌥G newest / oldest · ^b / ^f page · ^R reload
  ^H help       Esc quit
```

`^D` and `^X` prompt for a reason before flipping anything — that prompt
*is* the confirmation, and the reason is the part that pays: it lands in
the rec's frontmatter and `events.jsonl`, appears in the next analyze
prompt under "do not re-propose variants", and feeds the write-time
dedupe gate. Dismissing with *"we don't want a background daemon"* buys
silence on that whole idea. `^A` and `^L` are instant (no prompt) and
reversible.

Acting on a rec drops it out of the open block and slides the next one
under your cursor, so a triage pass is `^D` · reason · `^D` · reason
without touching the arrow keys. Closed recs stay in the list, dimmed and
searchable — type `dismissed` to review what you said no to, `^L` to take
one back.

Nothing new is persisted: every key routes through the same `set_status`
and events-log path as `undrudge dismiss`. Quitting leaves no state
behind. Requires [`fzf`](https://github.com/junegunn/fzf); install
[`glow`](https://github.com/charmbracelet/glow) too if you want the
preview rendered rather than raw.

### Handing a rec to an implementing session

`^Y` in the picker — or `undrudge copy` from any shell — puts the rec on
the clipboard as a **hand-off**: the recommendation verbatim, the
directories its evidence came from, what to check before building it, and
the `undrudge implement` / `undrudge dismiss` lines (id already filled in)
that close the loop. Paste it into whatever session is going to do the
work.

```bash
undrudge copy               # pick from a list, copy the one you choose
undrudge copy 20fc          # any unique id prefix — four characters usually do
undrudge copy 20fc --print  # to stdout instead (pipe it, or read it here)
undrudge copy 20fc --what path   # or: body, id
```

No id opens a one-shot picker with the same preview as `browse`, so the id
never has to make the trip through your eyes and back into another
terminal. With an id, prefix matching means you're typing four characters,
not sixty-four — and an ambiguous prefix tells you so instead of guessing.
Without a clipboard tool on PATH (`pbcopy`, `wl-copy`, `xclip`, `xsel`)
it prints the hand-off rather than silently dropping it.

This is the same idea as a dispatch brief, minus everything that only
exists inside a synced repo clone — use `dispatch` when you want briefs
routed to clones automatically, `copy` when you're driving.

## Retention

The SQLite file is a rebuildable cache over your Claude/Codex JSONL and
atuin history, and every consumer of it reads a 24h or 7d trailing window
— so rows older than a few weeks cost disk and buy nothing. (A few months
of dogfooding put message text past a gigabyte here.)

Each `gather` deletes ingested messages and shell commands older than the
retention window:

```toml
[retention]
days = 30      # 0 = keep everything
```

Recommendations are never pruned — they're the output, they're tiny, and
their markdown bodies on disk are the durable artifact. Cursors aren't
touched either, so pruning can never trigger a re-ingest of the history it
just dropped rows from.

```bash
undrudge prune --dry-run     # what would go, without writing
undrudge prune --days 14     # one-off, ignoring the configured window
undrudge prune --vacuum      # also FTS-optimize + VACUUM, handing the
                             # freed pages back to the filesystem
```

Deleted rows leave free pages behind that SQLite reuses but doesn't
return to the OS, so the file only *shrinks* on `--vacuum` (which needs
room for a second copy while it runs). A scheduled `gather` prunes at
most 50k rows per run, so enabling retention on a long-lived DB drains
the backlog over a few hourly runs instead of stalling one. `undrudge
doctor` reports the file size, the policy, and any remaining backlog.

## Capability gap — features you already have and aren't using

Repetition analysis can't catch one class of drudgery: a hand-rolled
workaround for a feature your agent already ships. The workaround *is*
the repetition — it looks like a well-factored habit. So undrudge also
keeps an inventory of what the installed agents can do, compares it
against what your history shows you actually use, and offers the gap to
the daily analyzer (design: `docs/capability-gap.md`). Four sources:

- **Help scrape** — `claude --help` / `codex --help` plus one level of
  subcommand help, re-parsed when the binary version changes. Free,
  local, can't hallucinate.
- **Live probe** — once a day the analyzer asks a real session to
  enumerate its own tools, skills, and slash commands. This is what
  finds in-session capabilities (and installed-then-forgotten plugins,
  skills, and MCP servers) that no help text mentions. One extra LLM
  call per day; probe rows retire after three consecutive absences, so
  an uninstalled plugin stops generating suggestions.
- **Usage inventory** — the `tool_name` column, slash-command prompts,
  and shell commands already in the DB are the "what you actually use"
  half. Costs nothing.
- **Release notes** — the provider's public changelog. This is the one
  network call undrudge makes: a bare conditional GET of a public file,
  carrying no local state, degrading silently to local-only when
  offline. On first run it backfills the whole changelog in byte-capped
  daily chunks (a cursor tracks progress), then reads only entries above
  the last-seen version. Notes are what catch *dormant* features —
  shipped behind an env var or enable command, invisible to every local
  source. Set `fetch_release_notes = false` under `[capabilities]` to
  turn it off, or point `release_notes_url` at a `file://` mirror.

Capability recs flow through the same pipeline as everything else —
same dedupe, same triage, same dismissal feedback. Their signature is
capability-derived (`adopt:claude:tool:SendMessage`), so dismissing one
silences that capability for good. A capability with no matching pain in
the digest produces no rec at all; the gap list is offered once and not
re-offered every morning.

```bash
undrudge capabilities --show     # print the current gap, no refresh
undrudge capabilities            # refresh (scrape/fetch/probe as due)
undrudge capabilities --force    # re-scrape + re-probe + re-fetch now
```

`undrudge doctor` reports per-provider versions, probe age, and backfill
progress under `capabilities:`.

## `undrudge here` — the pull side

`dispatch` (below) pushes work into a clone and opens a session there,
which can collide with a session already working in that directory.
`undrudge here` is the inverse: run it *from inside* the session that's
already there, and it tells you which open recs belong to this repo.

```bash
undrudge here                # ranked candidates for the repo in $PWD
undrudge here --json         # same, for an agent session to consume
undrudge here --dir ~/code/ops --limit 10
undrudge here --all-scopes   # include cross_cutting / agent_global too
```

Matching runs off evidence, not prose. Every rec's `evidence_refs` point
at the shell commands and agent sessions it was drawn from, and those
rows carry the directory they were observed in — captured at ingest, so
unlike the repo/branch labels in a digest it never goes stale. Three
tiers, strongest first:

- **`this_clone`** — evidence was observed inside this working tree.
  Path-prefix, so it still matches when the directory is long gone (a
  deleted worktree, a `/tmp` run).
- **`same_repo`** — evidence came from a different checkout with the same
  remote origin. `ops`, `ops2`, and `ops4/.qc/dev-worktrees/*` are one
  repo in several clones; matching on the directory name would call them
  several repos.
- **`named`** — no evidence points here, but the rec names this repo in
  its title. This is the tool-fix case: *"undrudge show should print the
  rec body"* was observed in two unrelated repos, because that's where
  the tool was being used — the fix belongs in undrudge's own tree, which
  appears in no evidence at all. A hint, not proof; verify before acting.

Only `single_repo` recs are considered by default. `cross_cutting` and
`agent_global` ones span directories by definition, so they stay with
`browse` + `copy` and a human deciding where they land.

The command only *reports* — it changes nothing, calls no LLM, and is
safe to run inside a session that's mid-task. Acting on the result is an
agent's job, not this repo's: point one at `undrudge here --json`, have
it verify the top candidate still applies against the live tree, and let
it either implement the rec or close it with `undrudge dismiss` /
`implement` / `mark`. Two rules worth building into whatever drives it —
**at most one rec per run**, and refuse outright when `repo.dirty` is
true rather than branching on top of someone's work in progress.

## Dispatch (optional)

`undrudge dispatch` is a deterministic courier — it moves paper, it
doesn't think. It selects `logged` recs, gates and routes them to repo
clones, writes a self-contained brief into each clone's
`.undrudge-inbox/`, and reconciles the verdicts your sessions leave back
into rec statuses. **Zero LLM calls** — an interactive Claude session
(the global `/undrudge-check` command) does the triage; the dispatcher
only shuffles files and flips statuses.

The loop is pull-based and file-only, in the same spirit as the rest of
undrudge:

1. `dispatch run` writes `<clone>/.undrudge-inbox/<id12>.md` for each
   routed rec and flips it to `dispatched` (so the analyzer treats it as
   in-flight and won't re-propose it).
2. A session triages the brief and drops a verdict —
   `.undrudge-inbox/done/<id12>.verdict.json` with a disposition (`ship`
   with a PR URL, `dismiss-stale`, `already-done`, `needs-human`, …).
3. The next `dispatch run` reconciles: it flips statuses (always via the
   same `set_status` path), polls open `ship` PRs to merge → `implemented`
   or closed → `dismissed`, and surfaces anything needing a human in a
   vault approval queue.

Routing, gating, the daily cap, deny-list (shadow mode), and per-route
clone behaviour are configured in a `[dispatch]` section of
`config.toml`; see `prototype/config.example.json` for the shape.
`--dry-run` computes and prints the whole report without writing a
single brief, flipping a status, or touching the vault.

## Privacy

Sanitization runs at ingest, before any row hits the SQLite. There is
no path in the system that stores unredacted text. Four mechanisms:

- Pattern matching for common API keys, tokens, JWTs, password
  assignments, connection strings, private keys.
- Path exclusion for file-read/edit tool calls touching `.env`, `.pem`,
  `.key`, `id_rsa*`, kubeconfigs, etc. — the path is stored, the content never
  is. Nested Codex tool payloads are sanitized recursively.
- Shannon-entropy detection for random-looking strings the patterns
  missed.
- Contextual suppression near phrases like "here is the token" /
  "my api key is".

A parametrized test plants every supported secret type in a fixture and
asserts none of them survive ingest. CI fails loud if anything leaks.

If your environment uses a secret type that isn't covered, add a pattern to
`src/undrudge/sanitize.py` and a synthetic case under `tests/` *before* running
`undrudge gather` against real data.

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
./scripts/dev-fresh.sh gather      # empty synthetic source dirs; never real history
./scripts/dev-fresh.sh analyze --dry-run day
```

Both scripts started life as recommendations undrudge made about its
own development workflow on early dogfood runs, then implemented. The
loop closes.

## Design

Full design notes and rationale: [docs/design.md](docs/design.md).

## License

MIT — see [LICENSE](LICENSE).
