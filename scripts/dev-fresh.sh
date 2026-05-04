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
# All sandbox state is rooted at this repo's directory, so cleanup is just
# `rm -rf .test-cfg .test-data` (handled at the top of every run).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

export UV_CACHE_DIR="$ROOT/.uv-cache"
export UNDRUDGE_CONFIG="$ROOT/.test-cfg/config.toml"
export XDG_DATA_HOME="$ROOT/.test-data"

rm -rf "$ROOT/.test-cfg" "$ROOT/.test-data"

uv run undrudge init

if (( $# > 0 )); then
    echo "---"
    uv run undrudge "$@"
fi
