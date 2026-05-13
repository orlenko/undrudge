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

# Deterministic-headless flags for `claude`:
#   --no-session-persistence don't write a session record to disk;
#                            each analyze run is independent.
# (We previously also pinned `--bare`, which disables hooks, LSP,
# plugin sync, auto-memory, *and* keychain reads. The keychain disable
# breaks OAuth-authenticated installs — claude refuses to start with
# "Not logged in" unless ANTHROPIC_API_KEY is in the env. Dropping
# --bare keeps the user's normal auth flow working at the cost of
# slightly less determinism; revisit if we ever provision an API key.)
CLAUDE_FLAGS=(--dangerously-skip-permissions --no-session-persistence)

# Re-entry guard: don't double-sandbox. nono propagates NONO_CAP_FILE to
# every contained process; if we're already inside, exec claude directly.
if [[ -n "${NONO_CAP_FILE:-}" ]]; then
  exec claude "${CLAUDE_FLAGS[@]}" "$@"
fi

# Fallthrough: if nono isn't installed, just run claude. The script remains
# safe to call from any machine.
if ! command -v nono >/dev/null 2>&1; then
  exec claude "${CLAUDE_FLAGS[@]}" "$@"
fi

# ccstatusline reads $COLUMNS / $LINES; nono strips them. tput falls back
# cleanly when there's no TTY (headless cron context).
COLS="$(tput cols 2>/dev/null || echo 80)"
ROWS="$(tput lines 2>/dev/null || echo 24)"

# nono drops grants for paths that don't exist at sandbox-init time;
# the `claude` profile grants $HOME/.claude.lock, but if the file is
# missing on a fresh machine the grant is silently skipped and claude
# then blocks for the 900s timeout trying to create it. Touching with
# -a creates an empty lock without churning mtime if it already exists.
touch -a "$HOME/.claude.lock"

# Profile is shipped as the `always-further/claude` pack on nono ≥ 0.48
# (built-in `claude-code` was removed in 0.48). Install once with:
#     nono pull always-further/claude
# claude-code probes /home on startup (Linux-shaped code path; /home
# doesn't even exist on macOS). nono 0.49 promoted that path from
# "noisy warning" to "permanently restricted", which kills the whole
# sandbox non-deterministically. --bypass-protection lifts the
# restriction and --read grants read-only access; the path may not
# exist on macOS but the override still suppresses the permanent
# denial. (Was --override-deny in 0.48; renamed in 0.49.)
exec nono run \
  --allow-cwd \
  --allow "$HOME/.claude" \
  --allow "$HOME/.local/share/claude" \
  --allow "$HOME/.local/state/claude" \
  --allow "$HOME/.local/share/undrudge" \
  --read-file "$HOME/.gitconfig" \
  --read "$HOME/.config/configstore" \
  --bypass-protection /home --read /home \
  --profile claude \
  -- env "COLUMNS=$COLS" "LINES=$ROWS" claude "${CLAUDE_FLAGS[@]}" "$@"
