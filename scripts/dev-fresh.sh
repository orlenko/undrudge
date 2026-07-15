#!/usr/bin/env bash
# dev-fresh.sh — reset to a clean undrudge sandbox under this repo, then
# forward args to a single `undrudge` invocation.
#
# Born from undrudge's own digest flagging the inline soup the agent kept
# retyping. The first rec to land as a real script.
#
# Usage:
#   ./scripts/dev-fresh.sh                            # init only
#   ./scripts/dev-fresh.sh doctor
#   ./scripts/dev-fresh.sh gather
#   ./scripts/dev-fresh.sh digest --window 24h --out -
#   ./scripts/dev-fresh.sh analyze --dry-run --window 24h
#
# All sandbox state and source-history paths are rooted at this repo's
# directory, so this helper never reads real Claude, Codex, or atuin history.
# Cleanup is just `rm -rf .test-cfg .test-data` (handled on every run).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

export UV_CACHE_DIR="$ROOT/.uv-cache"
export UNDRUDGE_CONFIG="$ROOT/.test-cfg/config.toml"
export XDG_DATA_HOME="$ROOT/.test-data"

rm -rf "$ROOT/.test-cfg" "$ROOT/.test-data"

uv run undrudge init

# `undrudge init` renders production-friendly history defaults. Rewrite only
# the three input paths for this disposable dev sandbox; all are intentionally
# empty. Write through a sibling file so a failed sed never truncates config.
SOURCES="$ROOT/.test-data/sources"
mkdir -p "$SOURCES/claude" "$SOURCES/codex/sessions" "$SOURCES/codex/archived_sessions"
sed \
    -e "s|^projects_root = .*|projects_root = \"$SOURCES/claude\"|" \
    -e "s|^home = .*|home = \"$SOURCES/codex\"|" \
    -e "s|^db = .*history\.db.*|db = \"$SOURCES/atuin.db\"|" \
    "$UNDRUDGE_CONFIG" > "$UNDRUDGE_CONFIG.tmp"
mv "$UNDRUDGE_CONFIG.tmp" "$UNDRUDGE_CONFIG"

if (( $# > 0 )); then
    echo "---"
    uv run undrudge "$@"
fi
