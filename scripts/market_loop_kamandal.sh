#!/bin/bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
LOCKROOT="$REPO_ROOT/data/runlocks"
LOCKDIR="$LOCKROOT/market_loop.lock"
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
NOW=$((10#$(date +%H%M)))
if (( DOW > 5 )); then
  exit 0
fi
if (( NOW < 832 || NOW > 1510 )); then
  exit 0
fi
"$REPO_ROOT/.venv/bin/python" -m vol_crush.main --skip-backtest
