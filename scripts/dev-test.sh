#!/usr/bin/env bash
# dev-test.sh — run pytest with the right UV cache and a trailing tail.
#
# Born from undrudge's own digest flagging the
# `UV_CACHE_DIR=.uv-cache uv run pytest -q 2>&1 | tail -N` compound the
# agent kept retyping.
#
# Usage:
#   ./scripts/dev-test.sh                  # quiet, last 50 lines
#   ./scripts/dev-test.sh -v               # verbose pytest output
#   ./scripts/dev-test.sh -k canonicalize  # filter by keyword
#   TAIL_N=200 ./scripts/dev-test.sh -v    # show more lines
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

export UV_CACHE_DIR="$ROOT/.uv-cache"

TAIL_N="${TAIL_N:-50}"

# Default to quiet when no args; otherwise honor what the caller passed.
if (( $# == 0 )); then
    set -- -q
fi

uv run pytest "$@" 2>&1 | tail -"$TAIL_N"
