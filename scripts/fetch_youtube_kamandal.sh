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

force_full_loop() {
  case "${KAMANDAL_FORCE_FULL_LOOP:-${KAMANDAL_FORCE_LOOP:-}}" in
    1|true|TRUE|yes|YES|on|ON)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

DOW=$(date +%u)
if force_full_loop; then
  echo "KAMANDAL_FORCE_FULL_LOOP enabled; running YouTube loop outside the normal schedule."
else
  if (( DOW > 5 )); then
    exit 0
  fi
fi
"$REPO_ROOT/.venv/bin/python" -m vol_crush.main --skip-backtest --fetch-sources youtube --source-limit 1
