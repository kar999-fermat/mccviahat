#!/usr/bin/env bash
# auto_queue.sh: run one queued trace per boot, then reboot for the next one.
#
# Started by cron at every boot (installed by: python3 scripts/run/make_queue.py ... --install).
# Reads ~/mcc_queue.txt, one trace per line:
#     <prompt json> <label> <run label> <output root>
# Works for 70b and 405b: it uses whatever model scripts/model/model_fetch.sh last set.
#
# Per boot:
#   1. log when the reboot was requested and when the node came back up
#   2. take the first line off the queue (removed BEFORE running, so a crash can
#      never turn into an endless reboot loop)
#   3. wait MCC_COOLDOWN_S seconds (default 600) so the node cools down after the
#      previous trace, and wait for Docker
#   4. run run_prompts_fulltrace.py (which does the usual server reset), then check_trace.py
#   5. reboot if anything is left in the queue; otherwise stop
#
# Progress:   tail -f ~/mcc_auto.log
# Stop early: rm ~/mcc_queue.txt   (the current trace finishes, nothing else starts)
# Remove:     crontab -l | grep -v auto_queue.sh | crontab -

QUEUE="${MCC_QUEUE:-$HOME/mcc_queue.txt}"
LOG="$HOME/mcc_auto.log"
STAMP="$HOME/.mcc_reboot_requested"
COOLDOWN_S="${MCC_COOLDOWN_S:-600}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

exec >>"$LOG" 2>&1
cd "$REPO" || exit 1

BOOT_TIME="$(uptime -s 2>/dev/null)"
echo "=== $(date '+%F %T') boot (node up since ${BOOT_TIME:-unknown})"
if [ -f "$STAMP" ]; then
  REQ="$(cat "$STAMP")"
  if [ -n "$BOOT_TIME" ]; then
    SECS=$(( $(date -d "$BOOT_TIME" +%s) - $(date -d "$REQ" +%s) ))
    echo "    reboot requested at $REQ -> node up at $BOOT_TIME (${SECS}s)"
  fi
  rm -f "$STAMP"
fi

LINE="$(grep -v '^[[:space:]]*$' "$QUEUE" 2>/dev/null | head -n1)"
if [ -z "$LINE" ]; then
  echo "Queue empty: all done."
  exit 0
fi
grep -v '^[[:space:]]*$' "$QUEUE" | tail -n +2 > "$QUEUE.tmp" && mv "$QUEUE.tmp" "$QUEUE"
read -r JSON LABEL RUN OUTROOT <<< "$LINE"
OUTROOT="${OUTROOT:-data/raw/fulltrace/batch_auto}"

echo "    next: $RUN ($LABEL, $JSON) -> $OUTROOT"
echo "    cooling down ${COOLDOWN_S}s before starting..."
sleep "$COOLDOWN_S"
for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 10; done

echo ">>> $(date '+%F %T') starting $RUN"
python3 scripts/run/run_prompts_fulltrace.py --json "$JSON" --label "$LABEL" \
  --run_label "$RUN" --output-root "$OUTROOT"
python3 scripts/run/check_trace.py "$OUTROOT/$RUN"
echo ">>> $(date '+%F %T') finished $RUN"

if grep -q '[^[:space:]]' "$QUEUE" 2>/dev/null; then
  echo "    $(grep -c '[^[:space:]]' "$QUEUE") trace(s) left; rebooting."
  date '+%F %T' > "$STAMP"
  sudo reboot
else
  echo "Queue empty: all done."
fi
