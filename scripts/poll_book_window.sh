#!/usr/bin/env bash
# One-off timing probe: find out when the booking window opens for a date
# ~10 days out (book_ahead_days in config.toml, currently 10) by retrying
# `fs book --yes` every 10 minutes until it succeeds or a cutoff time passes.
#
# Not part of the reusable probe infra in scripts/probes/ -- this is a
# throwaway experiment to observe server behavior, same spirit as
# scripts/probes/write_probe.py but scoped to "what time does today's
# rolling window open", not booking cleanup. Deliberately makes real
# bookings against the live account.
#
# Usage: ./scripts/poll_book_window.sh [cutoff_HH:MM] [interval_seconds]
#   cutoff_HH:MM     stop trying after this local time (default 09:00)
#   interval_seconds seconds between attempts (default 600 = 10 min)

set -euo pipefail

CUTOFF="${1:-09:00}"
INTERVAL="${2:-600}"

cd "$(dirname "$0")/.."
source .venv/bin/activate

cutoff_epoch=$(date -j -f "%H:%M" "$CUTOFF" +%s 2>/dev/null || date -d "today $CUTOFF" +%s)

echo "Polling 'fs book --yes' every ${INTERVAL}s until success or ${CUTOFF} (whichever first)."
echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
echo

attempt=0
while true; do
    attempt=$((attempt + 1))
    now=$(date '+%Y-%m-%d %H:%M:%S')
    now_epoch=$(date +%s)

    echo "--- attempt $attempt @ $now ---"
    set +e
    output=$(fs book --yes --json 2>&1)
    code=$?
    set -e
    echo "$output"
    echo "exit code: $code"
    echo

    if [ "$code" -eq 0 ]; then
        echo "SUCCESS at $now (attempt $attempt) -- booking window is open."
        exit 0
    fi

    if [ "$now_epoch" -ge "$cutoff_epoch" ]; then
        echo "Reached cutoff ${CUTOFF} without success after $attempt attempts. Stopping."
        exit 1
    fi

    sleep "$INTERVAL"
done
