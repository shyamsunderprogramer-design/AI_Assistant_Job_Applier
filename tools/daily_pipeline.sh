#!/bin/bash
# One scheduled run of the daily pipeline: scrape -> score -> export -> digest.
#
# Written to be driven by something that is not a person: a scheduler gets no
# terminal, no PATH it did not set, and no chance to answer a prompt. So this
# hardcodes the interpreter, logs everything with timestamps, refuses to run
# twice at once, and exits non-zero when a stage fails so the caller can alert.
#
#   tools/daily_pipeline.sh [daily|discover|enrich]
#
# Prints a one-line JSON summary on stdout as its last line, for a scheduler
# to branch on. Human-readable output goes to data/daily_run.log.

set -uo pipefail

PROJECT="/Volumes/Storage/D Drive /Rep/AI_Assitant_Job_Applier"
PY="$PROJECT/.venv/bin/python"
TASK="${1:-daily}"
LOG="$PROJECT/data/daily_run.log"
LOCK="$PROJECT/data/${TASK}.lock"
STALE_HOURS=6

cd "$PROJECT" || { echo '{"ok":false,"error":"project directory missing"}'; exit 1; }
[ -x "$PY" ] || { echo '{"ok":false,"error":"venv interpreter missing"}'; exit 1; }
mkdir -p "$PROJECT/data"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

# mkdir is the atomic primitive here: macOS has no flock, and a lock FILE can
# be left behind by a kill -9 with no way to tell it from a live run.
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +$((STALE_HOURS * 60)) 2>/dev/null)" ]; then
    log "breaking a lock older than ${STALE_HOURS}h"
    rmdir "$LOCK" 2>/dev/null
    mkdir "$LOCK" 2>/dev/null || { echo '{"ok":false,"error":"locked"}'; exit 0; }
  else
    log "$TASK already running — skipping this tick"
    echo '{"ok":true,"skipped":"already running"}'
    exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

log "=== $TASK starting ==="
case "$TASK" in
  daily)
    "$PY" -u main.py daily >> "$LOG" 2>&1
    ;;
  discover)
    # Weekly, not daily: a full sweep is hours of polite probing.
    "$PY" -u main.py discover --names data/mailbox_names.txt --max-slugs 2 >> "$LOG" 2>&1
    ;;
  enrich)
    bash data/companies_build/enrich_loop.sh >> "$LOG" 2>&1
    ;;
  *)
    echo "{\"ok\":false,\"error\":\"unknown task $TASK\"}"
    exit 1
    ;;
esac
STATUS=$?
log "=== $TASK finished, exit $STATUS ==="

# The summary is what a scheduler reads to decide whether to notify.
SUMMARY="$("$PY" tools/run_summary.py "$TASK" "$STATUS" 2>/dev/null)"
[ -n "$SUMMARY" ] \
  || SUMMARY="{\"ok\":false,\"task\":\"$TASK\",\"exit\":$STATUS,\"error\":\"summary failed\"}"
echo "$SUMMARY"

# launchd cannot branch on output, so the decision to interrupt lives here.
# Silence is the default: a notification every morning saying "nothing new"
# trains you to swipe it away, and then you miss the one that mattered.
if [ "${NOTIFY:-1}" = "1" ]; then
  MESSAGE="$(NOTIFY_SUMMARY="$SUMMARY" "$PY" - <<'PYEOF'
import json, os
try:
    s = json.loads(os.environ["NOTIFY_SUMMARY"])
except Exception:
    raise SystemExit(0)
if not s.get("ok"):
    print(f"{s.get('task', 'run')} failed (exit {s.get('exit')}) — see data/daily_run.log|Basso")
elif s.get("new_strong"):
    top = (s.get("highlights") or [{}])[0]
    where = f" — top: {top.get('score')}% {top.get('company')}" if top.get("company") else ""
    print(f"{s['new_strong']} new strong matches, {s.get('open')} open{where}|")
PYEOF
)"
  if [ -n "$MESSAGE" ]; then
    BODY="${MESSAGE%|*}"
    SOUND="${MESSAGE##*|}"
    if [ -n "$SOUND" ]; then
      osascript -e "display notification \"${BODY//\"/}\" with title \"Job pipeline\" sound name \"$SOUND\"" 2>/dev/null
    else
      osascript -e "display notification \"${BODY//\"/}\" with title \"Job pipeline\"" 2>/dev/null
    fi
    log "notified: $BODY"
  fi
fi
exit "$STATUS"
