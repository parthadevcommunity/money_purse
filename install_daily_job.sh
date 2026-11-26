#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(command -v python3)"
LABEL="com.parthadevcommunity.moneypurse.daily-refresh"
AGENT_DIR="$HOME/Library/LaunchAgents"
AGENT_PATH="$AGENT_DIR/$LABEL.plist"
LOG_DIR="$ROOT/logs"

mkdir -p "$AGENT_DIR" "$LOG_DIR"
"$PYTHON" - "$ROOT" "$PYTHON" "$AGENT_PATH" "$LABEL" <<'PY'
import plistlib
import sys
from pathlib import Path

root, python, agent_path, label = sys.argv[1:]
root = Path(root)
plist = {
    "Label": label,
    "ProgramArguments": [python, str(Path(root) / "daily_refresh_publish.py")],
    "WorkingDirectory": str(root),
    "StartCalendarInterval": [
        {"Weekday": weekday, "Hour": 17, "Minute": 0}
        for weekday in range(1, 6)
    ],
    "StandardOutPath": str(Path(root) / "logs" / "daily_refresh_publish.log"),
    "StandardErrorPath": str(Path(root) / "logs" / "daily_refresh_publish_error.log"),
    "EnvironmentVariables": {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONUNBUFFERED": "1",
    },
}
with open(agent_path, "wb") as stream:
    plistlib.dump(plist, stream)
PY

launchctl bootout "gui/$(id -u)" "$AGENT_PATH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AGENT_PATH"
launchctl enable "gui/$(id -u)/$LABEL"
echo "Installed $LABEL for 5:00 pm Monday-Friday; the NSE holiday check skips market holidays."
echo "Logs: $LOG_DIR"
