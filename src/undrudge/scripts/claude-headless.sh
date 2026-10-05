#!/usr/bin/env bash
# claude-headless.sh — bundled with undrudge.
#
# Runs Claude Code headless with the flags the analyze step needs. The
# point: undrudge's analyze step never needs the user's separate
# ops/dotfiles repos to be present.
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
exec claude --dangerously-skip-permissions --no-session-persistence "$@"
