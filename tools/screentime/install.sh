#!/usr/bin/env bash
# Install (or `uninstall`) the daylog screen-time forwarder as a LaunchAgent.
#
#   ./install.sh            check ActivityWatch, ask for the inlet URL, load the agent
#   ./install.sh uninstall  unload the agent and remove its files
#
# The script is copied to ~/Library/Application Support/daylog-screentime/, so the
# agent does not depend on this checkout (and launchd never needs ~/Documents access).
set -euo pipefail

LABEL="com.daylog.screentime"
HERE="$(cd "$(dirname "$0")" && pwd)"
SUPPORT="$HOME/Library/Application Support/daylog-screentime"
SCRIPT="$SUPPORT/daylog_screentime.py"
CONFIG="$SUPPORT/config.json"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/daylog-screentime.log"
AW_URL="${AW_URL:-http://localhost:5600}"

unload() {
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
}

if [[ "${1:-}" == "uninstall" ]]; then
  unload
  rm -f "$PLIST"
  rm -rf "$SUPPORT"
  echo "Uninstalled: agent unloaded, $PLIST and $SUPPORT removed."
  echo "The log is left at $LOG."
  exit 0
elif [[ -n "${1:-}" ]]; then
  echo "usage: $0 [uninstall]" >&2
  exit 2
fi

# 1. ActivityWatch must be running.
if ! curl -fsS -m 3 "$AW_URL/api/0/info" >/dev/null; then
  echo "ActivityWatch is not answering at $AW_URL." >&2
  echo "Install it (brew install --cask activitywatch), open it, and run this again." >&2
  exit 1
fi
echo "ActivityWatch is running at $AW_URL."

# 2. A Python 3.9+.
PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]] || ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
  echo "Need python3 (3.9 or newer) on PATH." >&2
  exit 1
fi
echo "Using $PYTHON ($("$PYTHON" --version 2>&1))."

# 3. The inlet URL, kept in config.json (mode 600), never in the plist.
mkdir -p "$SUPPORT"
KEEP=""
if [[ -f "$CONFIG" ]]; then
  read -r -p "Keep the inlet URL already configured? [Y/n] " KEEP
fi
if [[ -f "$CONFIG" && ! "$KEEP" =~ ^[Nn] ]]; then
  echo "Keeping $CONFIG."
else
  read -r -s -p "daylog inlet URL (https://<host>/health/<secret>): " INLET
  echo
  if [[ "$INLET" != https://*/health/* ]]; then
    echo "That does not look like https://<host>/health/<secret>." >&2
    exit 1
  fi
  (
    umask 077
    # Passed by environment, not argv, so it never shows up in `ps`.
    DAYLOG_INLET_URL="$INLET" "$PYTHON" -c '
import json, os, sys
with open(sys.argv[1], "w") as f:
    json.dump({"DAYLOG_INLET_URL": os.environ["DAYLOG_INLET_URL"]}, f, indent=2)
    f.write("\n")
' "$CONFIG"
  )
  chmod 600 "$CONFIG"
  echo "Wrote $CONFIG (mode 600)."
fi

# 4. The script, and a dry run so you can see what would be sent.
cp "$HERE/daylog_screentime.py" "$SCRIPT"
SAMPLE="$(mktemp)"
trap 'rm -f "$SAMPLE"' EXIT
echo
echo "Dry run, last hour (nothing is sent):"
"$PYTHON" "$SCRIPT" --dry-run --since 1 >"$SAMPLE"
head -n 30 "$SAMPLE"
if [[ "$(wc -l <"$SAMPLE")" -gt 30 ]]; then
  echo "  ..."
fi
echo

# 5. The LaunchAgent: every 15 minutes and at login.
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s|__PYTHON__|$PYTHON|g" -e "s|__SCRIPT__|$SCRIPT|g" -e "s|__LOG__|$LOG|g" \
  "$HERE/$LABEL.plist" >"$PLIST"
plutil -lint "$PLIST" >/dev/null
unload
launchctl bootstrap "gui/$UID" "$PLIST"
echo "Loaded $LABEL: it runs now and every 15 minutes."
echo "Log: tail -f \"$LOG\""
