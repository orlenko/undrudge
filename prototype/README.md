# Prototype dispatcher (vendored reference)

Verbatim snapshot of the working `undrudge-dispatch` prototype as it ran
in production, captured as the first commit of the `undrudge dispatch`
port so the port can diff against reality and the loose files have
version control.

These files are **reference only** — not packaged into the wheel, not
run by `undrudge`. Once `undrudge dispatch run` is merged and deployed,
the launchd job's ProgramArguments swap to it and these can be retired.

Snapshot origin (2026-06-10):
- `undrudge-dispatch`        ← ~/.local/bin/undrudge-dispatch
- `commands/undrudge-check.md` ← ~/.claude/commands/undrudge-check.md
- `config.json`             ← ~/.config/undrudge-dispatch/config.json  (pending)
- `undrudge.dispatch.plist` ← ~/Library/LaunchAgents/undrudge.dispatch.plist  (pending)
- `commands/undrudge-queue.md` ← <vault>/.claude/commands/undrudge-queue.md  (pending)

The three "pending" files live under sandbox-blocked paths; added in a
follow-up commit.
