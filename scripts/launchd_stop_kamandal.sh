#!/bin/bash
set -euo pipefail
UID_VAL=$(id -u)
launchctl bootout gui/${UID_VAL} ~/Library/LaunchAgents/com.kamandal.shadow.loop.plist >/dev/null 2>&1 || true
launchctl bootout gui/${UID_VAL} ~/Library/LaunchAgents/com.kamandal.shadow.youtube.plist >/dev/null 2>&1 || true
