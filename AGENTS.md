# undrudge agent guide

## Project shape

undrudge is a retrospective, batch-oriented Python 3.12+ CLI. It ingests
sanitized agent-session and shell history into SQLite, renders compact digests,
and asks an LLM for automation recommendations. The database is a rebuildable
query cache; source histories remain the source of truth.

- Application code: `src/undrudge/`
- Tests and synthetic fixtures: `tests/`
- Current operator documentation: `README.md`
- Original design rationale: `docs/design.md` (useful history, not a complete
  description of current behavior)
- Developer commands: `scripts/dev-test.sh` and `scripts/dev-fresh.sh`

Use current source and tests to resolve disagreements with older design notes.
Treat `prototype/` as reference-only unless a task explicitly targets it.

## Non-negotiable invariants

- Sanitize every transcript, tool input, tool result, and shell-command field
  before it reaches SQLite. On sanitizer failure, drop the row; never persist a
  raw fallback.
- Never use or commit real chat histories, shell histories, tokens, or private
  paths as test fixtures. Build synthetic JSONL variants under `tests/`.
- Treat Claude, Codex, and Atuin data as read-only sources.
- Keep gather sources failure-isolated. One unavailable or malformed source
  must not block the others, and failures must remain visible in `events.jsonl`
  and `doctor`.
- Keep byte cursors and row identifiers idempotent across reruns, partial
  writes, file truncation, and Codex session archival/moves.
- Treat Codex rollout JSONL as an unstable external format. Ignore unknown
  record shapes safely, isolate compatibility logic in `ingest_codex.py`, and
  add a synthetic fixture for every newly supported shape.
- Schema changes require all three: the fresh schema in `schema.sql`, an
  additive upgrade in `store._migrate`, and tests for fresh and existing DBs.
- Preserve the product boundary: scheduled batch jobs, markdown output, and a
  human deciding what to implement. Do not add a daemon, UI, or external
  service without an explicit product decision.
  - `undrudge browse` is the one sanctioned interactive surface (product
    decision, 2026-07-31): an ephemeral fzf sidecar that reads the same rows
    and files, routes every mutation through `recommend.set_status` +
    `events.record`, and leaves no state of its own behind. Keep it that way —
    a picker that persists its own view state, caches recs, or outlives the
    keypress is a UI, and needs its own decision.
- Retention (`prune`, and the capped pass at the end of `gather`) may delete
  `messages`, `commands`, and sessions those leave empty — nothing else.
  Recommendations are the product's output and cursors are what make re-ingest
  idempotent; pruning either would either lose work or replay whole histories.
- Prefer the standard library. Ask before adding a production dependency.

## Working agreement

1. Inspect `git status` and the relevant diff before editing. Preserve unrelated
   and pre-existing work in a dirty tree.
2. Read the parser, schema, digest, config/CLI, and their tests together when a
   change crosses an ingestion boundary.
3. Use focused tests while iterating. Before handoff run:
   - `uv run ruff check .`
   - `uv run pytest -q`
   The repo-level uv configuration keeps its cache in `.uv-cache`; no
   `UV_CACHE_DIR` prefix is needed for commands run from this project.
4. Do not run `undrudge gather` or `undrudge analyze` against the user's default
   histories as routine verification. Use synthetic fixtures and isolated
   config/data directories.
5. When user-visible sources or providers change, update config generation,
   `doctor`, gather reporting/events, README, and package metadata together.

## Skeptical review workflow

For nontrivial architecture, privacy, ingestion, migration, cursor, or parser
work, delegate two bounded read-only reviews when subagents are available:

1. Use `design_skeptic` before or early in implementation to challenge product
   fit, assumptions, compatibility, and simpler designs.
2. Use `implementation_critic` after the diff and focused tests exist to trace
   failure paths, privacy boundaries, idempotency, migrations, and missing
   coverage.

Reviews are advisory; the main agent owns the decision and verification. Do not
spawn a review fleet for trivial documentation or mechanical edits.

## Rationale history

Prior decisions, risks, and deferred work may exist in the local rationale
repository. When investigating unfamiliar behavior, search by path, commit,
date, or keyword with:

```bash
~/.rationale/repo/bin/rationale search <query>
```
