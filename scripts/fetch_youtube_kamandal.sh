#!/bin/bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
LOCKROOT="$REPO_ROOT/data/runlocks"
LOCKDIR="$LOCKROOT/fetch_youtube.lock"
mkdir -p "$LOCKROOT"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  exit 0
fi
trap 'rmdir "$LOCKDIR"' EXIT
set -a
source "$REPO_ROOT/.env"
set +a
export PYTHONUNBUFFERED=1
DOW=$(date +%u)
if (( DOW > 5 )); then
  exit 0
fi
"$REPO_ROOT/.venv/bin/python" -m vol_crush.main --skip-backtest --fetch-sources youtube --source-limit 1
