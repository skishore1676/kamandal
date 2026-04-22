#!/bin/bash
set -euo pipefail
UID_VAL=$(id -u)
launchctl bootout gui/${UID_VAL} ~/Library/LaunchAgents/com.kamandal.shadow.loop.plist >/dev/null 2>&1 || true
launchctl bootout gui/${UID_VAL} ~/Library/LaunchAgents/com.kamandal.shadow.youtube.plist >/dev/null 2>&1 || true
launchctl bootstrap gui/${UID_VAL} ~/Library/LaunchAgents/com.kamandal.shadow.loop.plist
launchctl bootstrap gui/${UID_VAL} ~/Library/LaunchAgents/com.kamandal.shadow.youtube.plist
launchctl enable gui/${UID_VAL}/com.kamandal.shadow.loop
launchctl enable gui/${UID_VAL}/com.kamandal.shadow.youtube
launchctl kickstart -k gui/${UID_VAL}/com.kamandal.shadow.loop
