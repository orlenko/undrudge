---
description: Triage and implement queued undrudge briefs in this repo (or one rec by id)
argument-hint: "[rec-id]"
---

# /undrudge-check — process undrudge recommendations against this repo

You are in a repo that may have undrudge dispatch briefs queued. undrudge is a
background watchman that mines shell history + Claude transcripts for automation
opportunities; a deterministic courier (`undrudge-dispatch`) routes its
recommendations here as self-contained briefs. Your job: triage each one against
the *live* tree and either implement it or close it with a reasoned verdict.

## Mode

- **No argument**: process every `*.md` brief in `./.undrudge-inbox/` (ignore `done/`).
  If the directory is empty or missing, say so and stop.
- **Argument `$ARGUMENTS` (a rec id)**: run `undrudge show $ARGUMENTS`, read the rec
  file it prints, and triage that single rec against this repo (no brief required).

## Triage procedure (per rec)

Read the brief fully — it contains the rec, resolved evidence locations, and recent
git/PR context. Then verify the four predicates **before building anything**
(undrudge evidence is sometimes hallucinated or 2–4 weeks stale):

1. The cited files/commands/paths exist in THIS tree.
2. The pain is steady-state — not a one-off burst that ended (check evidence dates).
3. Not already solved here (check the brief's git log / merged PRs, and the tree).
4. It doesn't target the retired sbx/QC stack and doesn't automate the manual
   /rat:inbox loop (standing user constraints).

Pick exactly one disposition:

| Disposition | Action |
|---|---|
| **ship** | Implement it (below), open a **draft** PR. Do NOT flip undrudge status — the dispatcher implements it when the PR merges. |
| **dismiss-stale** | Evidence dead / already obsolete → `undrudge dismiss <id12>` |
| **reject-as-framed** | Real pain, wrong proposal → `undrudge dismiss <id12>`, explain the better framing in the reason |
| **already-done** | Solved since the rec → `undrudge implement <id12>` |
| **convert-to-task** | Human backlog item, not automatable → verdict only; the dispatcher surfaces it |
| **needs-human** | Blocked on a decision/credential → verdict only, state exactly what you need |

## Implementation rules (ship)

- Start from the route's base branch (this clone is synced by the dispatcher; if the
  tree is dirty with tracked changes, stop and use disposition needs-human).
- Branch `undrudge/<id12>-<short-slug>`, commit subject `undrudge:<id12> <title>`.
- Keep it minimal and idiomatic to this repo; run the repo's own checks
  (`scripts/check`, lint, affected tests) before committing.
- Push and `gh pr create --draft --title "undrudge: <title>"` — try `--label undrudge`,
  retry without the label if it doesn't exist. PR body: what the rec observed,
  what you built, how you verified. End the body with:
  🤖 Generated with [Claude Code](https://claude.com/claude-code)
- Return to the base branch afterward so the next brief starts clean.

## Bookkeeping (every rec, every disposition)

1. Write `./.undrudge-inbox/done/<id12>.verdict.json`:
   `{"id": "<id12>", "disposition": "...", "reason": "<one or two sentences>", "artifact": "<full PR URL for ship, else null>"}`
   For **ship**, `artifact` MUST be the complete PR URL (https://github.com/...) —
   the dispatcher polls it to flip the rec when the PR merges.
2. Move the brief from `.undrudge-inbox/<id12>.md` into `.undrudge-inbox/done/`.
3. **Single-id mode** (no brief, possibly a clone the dispatcher doesn't scan):
   write the verdict to the central spool instead:
   `~/.local/share/undrudge-dispatch/verdicts/<id12>.verdict.json` — the dispatcher
   always reads that directory.

## Guardrails

- Never touch `.a2a/` directories; never schedule loops or cron jobs.
- Never modify `~/.zshrc` or existing dotfiles. New global Claude commands are
  allowed only as NEW files under `~/.claude/commands/` and only when the brief's
  route is the vault/agent-global.
- Never merge a PR yourself; draft PRs are the human approval gate.
- One rec = one branch; don't batch unrelated recs into one PR.

## Wrap-up

Print a summary table (id, title, disposition, artifact). If anything shipped or
needs a decision, end with the one-line list of draft PR URLs / open questions.
