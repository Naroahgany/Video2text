#!/usr/bin/env bash
set -u

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$PROJECT_ROOT/scripts/start-local.sh"

if [ ! -f "$SCRIPT" ]; then
  echo "[ERROR] Missing startup helper: $SCRIPT"
  echo "Please re-download the project zip or restore the scripts directory."
  read -r -p "Press Enter to close... " _
  exit 1
fi

chmod +x "$SCRIPT" 2>/dev/null || true
"$SCRIPT"
EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
  echo
  echo "Startup failed. Please read the message above and try again."
  read -r -p "Press Enter to close... " _
fi

exit "$EXIT_CODE"
