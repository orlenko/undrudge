# Tool meta-analysis (weekly pass)

This is a weekly run, so do one extra pass beyond the per-pattern
analysis above.

Look at the most repeated command verbs across the digest and ask:
**is there a well-known modern tool that would eliminate the friction,
and is the user already using it?**

The signal is a command appearing many times where a popular
alternative is *absent* from the same digest. Common shapes:

- `grep -r` / recursive `grep` with no `rg` (ripgrep) in evidence → suggest `rg`.
- `find … -name` / `find … -type` with no `fd` → suggest `fd`.
- Repeated `cat <file>` for inspection (vs piping) with no `bat` → suggest `bat`.
- Manual fuzzy filtering (`ls | grep`, `history | grep`) with no `fzf` → suggest `fzf`.
- Hand-rolled JSON munging with `grep`/`awk`/`sed` instead of `jq` → suggest `jq`.
- Repeated multi-pane shell juggling (multiple terminal windows, lost context) with no `tmux`/`zellij` evidence → suggest a multiplexer.
- `top` with no `htop`/`btop` evidence — low-priority; only if it shows up a lot.

Rules for this pass — these matter, please follow them:

- **Only recommend tools you are confident exist and work on macOS as
  of your training.** Don't speculate about bleeding-edge tools you're
  unsure about; a wrong recommendation here is worse than no
  recommendation. Training data is the source of truth.
- **Check the digest for prior use of the recommended tool.** If the
  user already invokes the modern tool elsewhere in the same digest,
  don't recommend the swap as if it were new — instead frame it as a
  habit nudge ("`rg` shows up in repo A but you still reach for
  `grep -r` in repo B"). If they use it consistently, skip entirely.
- **Skip if nothing obvious jumps out.** No catalog mining, no "you
  should also consider X" lists. Zero or one or two high-confidence
  swaps per week is the target; zero is a fine and honest answer.
- Title these recs in the form `"Switch from <old> to <new>"` or
  `"Adopt <tool> for <pattern>"` so they're easy to spot among the
  per-pattern recs.
- Use `automation_form: "shell_alias"` (if the swap can be a simple
  alias) or `"other"` (habit change). Use `target_scope:
  "agent_global"` — these are cross-cutting habit changes, not
  per-repo scripts.
- The dedupe rule still applies: if you suggested the same swap in
  a prior week and it's in the "Previously logged" section, skip.

Output these alongside the per-pattern recs in the same JSON array
— no separate field, just include them.
