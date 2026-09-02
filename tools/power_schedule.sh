#!/bin/bash
# Keeps a Mac awake on a schedule (e.g. so unattended automation can run) by
# disabling idle system sleep during a wake/sleep window, then restoring
# whatever your machine's sleep setting was before, when you're done.
#
# Needs sudo -- run this yourself in a terminal, not from an agent/script that
# can't answer an interactive password prompt.
#
# Usage:
#   ./power_schedule.sh enable <wake HH:MM> <sleep HH:MM> [days]
#   ./power_schedule.sh disable
#
# Example:
#   ./power_schedule.sh enable 08:00 17:30 MTWTF
#
# [days] uses pmset's day-letter format (M T W T F S U for Mon..Sun, e.g. MTWTF
# for weekdays, MTWTFSU for every day). Defaults to weekdays if omitted.
set -euo pipefail

STATE_FILE="$HOME/.power_schedule_prev_sleep"
MODE="${1:-}"

case "$MODE" in
  enable)
    WAKE="${2:?Usage: $0 enable <wake HH:MM> <sleep HH:MM> [days]}"
    SLEEP_TIME="${3:?Usage: $0 enable <wake HH:MM> <sleep HH:MM> [days]}"
    DAYS="${4:-MTWTF}"

    # Save whatever the current sleep timeout is (battery and AC can differ) so
    # 'disable' restores it exactly, whatever it was -- don't hardcode a value.
    if [[ ! -f "$STATE_FILE" ]]; then
      awk '
        /^Battery Power:/ { section="battery" }
        /^AC Power:/      { section="ac" }
        /^[[:space:]]*sleep[[:space:]]/ { print section, $2 }
      ' <(pmset -g custom) > "$STATE_FILE"
      echo "Saved current sleep settings to $STATE_FILE for later restore."
    fi

    echo "Scheduling wake ${WAKE} / sleep ${SLEEP_TIME} on ${DAYS}..."
    sudo pmset repeat wake "$DAYS" "${WAKE}:00" sleep "$DAYS" "${SLEEP_TIME}:00"

    echo "Disabling idle system sleep (both battery and AC) so it doesn't nap mid-window..."
    sudo pmset -a sleep 0

    echo "Done. Display can still sleep on its own (screen only, harmless) -- system won't."
    echo "Revert anytime with: $0 disable"
    ;;
  disable)
    echo "Cancelling the wake/sleep schedule..."
    sudo pmset repeat cancel

    if [[ -f "$STATE_FILE" ]]; then
      while read -r SOURCE MINUTES; do
        FLAG="-b"; [[ "$SOURCE" == "ac" ]] && FLAG="-c"
        echo "Restoring $SOURCE sleep timeout to ${MINUTES} min..."
        sudo pmset "$FLAG" sleep "$MINUTES"
      done < "$STATE_FILE"
      rm -f "$STATE_FILE"
    else
      echo "No saved previous setting found -- leaving sleep=0 as is."
      echo "Set it back manually if needed: sudo pmset -a sleep <minutes>"
    fi

    echo "Done."
    ;;
  *)
    cat <<EOF
Usage:
  $0 enable <wake HH:MM> <sleep HH:MM> [days]   e.g. $0 enable 08:00 17:30 MTWTF
  $0 disable
EOF
    exit 1
    ;;
esac
