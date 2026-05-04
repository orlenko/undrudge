#!/usr/bin/env bash
# install.sh — render undrudge launchd plists with the current $HOME, copy
# them into ~/Library/LaunchAgents, and (re)bootstrap them.
#
# Idempotent. Edit the templates next to this script and re-run to change
# schedules or args.

set -euo pipefail

cd "$(dirname "$0")"

LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA" "$HOME/.local/share/undrudge/logs"

for src in undrudge.*.plist; do
    label="${src%.plist}"
    dest="$LA/$src"
    sed "s|__HOME__|$HOME|g" "$src" > "$dest"

    # bootout returns nonzero if the label isn't loaded — that's fine on
    # first install. Swallow it; bootstrap below is the load that matters.
    launchctl bootout "gui/$UID/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$dest"

    echo "loaded: $label"
done

cat <<'EOF'

To remove later:
    launchctl bootout gui/$UID/undrudge.gather
    launchctl bootout gui/$UID/undrudge.analyze
    launchctl bootout gui/$UID/undrudge.analyze-meta
    rm ~/Library/LaunchAgents/undrudge.*.plist

To run a job once on demand (without waiting for its schedule):
    launchctl kickstart gui/$UID/undrudge.gather

Logs:
    ~/.local/share/undrudge/logs/{gather,analyze,analyze-meta}.log
EOF
