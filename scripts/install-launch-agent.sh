#!/bin/bash
# Optional: make the local stack + tunnels come back after a reboot.
# Installs a per-user LaunchAgent that runs scripts/boot-local-stack.sh at login.
#
#   install:    scripts/install-launch-agent.sh
#   uninstall:  scripts/install-launch-agent.sh --uninstall
#
# Nothing here needs sudo — it is a user agent, not a system daemon.
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LABEL=com.dfi.localstack
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
LOGDIR=$(cd "$SCRIPT_DIR/.." && pwd)/data/imports

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $PLIST"
  exit 0
fi

mkdir -p "$(dirname "$PLIST")" "$LOGDIR"
cat >"$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$SCRIPT_DIR/boot-local-stack.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>$LOGDIR/launchagent.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/launchagent.log</string>
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
echo "installed $PLIST"
echo "the stack + tunnels will now start automatically at login/reboot"
