#!/usr/bin/env bash
# claude-sandboxed.sh — bundled with undrudge.
#
# Runs Claude Code headless under nono's `claude-code` profile when nono is
# installed; otherwise exec's claude directly. The point: undrudge's analyze
# step never needs the user's separate ops/dotfiles repos to be present.
#
# This is a minimal allow-list — narrower than an interactive-use wrapper —
# because the analyzer is a one-shot non-interactive call: we pass the
# digest on stdin and parse JSON on stdout. No tool use that needs gh, gnpm,
# the keychain, etc.
set -euo pipefail

# Re-entry guard: don't double-sandbox. nono propagates NONO_CAP_FILE to
# every contained process; if we're already inside, exec claude directly.
if [[ -n "${NONO_CAP_FILE:-}" ]]; then
  exec claude --dangerously-skip-permissions "$@"
fi

# Fallthrough: if nono isn't installed, just run claude. The script remains
# safe to call from any machine.
if ! command -v nono >/dev/null 2>&1; then
  exec claude --dangerously-skip-permissions "$@"
fi

# ccstatusline reads $COLUMNS / $LINES; nono strips them. tput falls back
# cleanly when there's no TTY (headless cron context).
COLS="$(tput cols 2>/dev/null || echo 80)"
ROWS="$(tput lines 2>/dev/null || echo 24)"

exec nono run \
  --allow-cwd \
  --allow "$HOME/.claude" \
  --allow "$HOME/.claude.lock" \
  --allow "$HOME/.local/share/claude" \
  --allow "$HOME/.local/state/claude" \
  --allow "$HOME/.local/share/undrudge" \
  --read-file "$HOME/.gitconfig" \
  --read "$HOME/.config/configstore" \
  --profile claude-code \
  -- env "COLUMNS=$COLS" "LINES=$ROWS" claude --dangerously-skip-permissions "$@"
