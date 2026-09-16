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

PROJECT="/Volumes/Storage/D Drive /Rep/AI_Assistant_Job_Applier"
PY="$PROJECT/.venv/bin/python"

# Hold the Mac awake for the length of the run.
#
# Without this the 6am run freezes about sixty seconds in: pmset wakes the
# machine, launchd starts the scrape, macOS then sees no user activity, applies
# `sleep 1`, and suspends it mid-board. The job does not fail -- it stops until
# something wakes the Mac again, which is worse, because the log then looks
# like a hang rather than a sleep. A full run takes one to three and a half
# hours, so this is not a corner case; it is every single morning.
#
#   -i  no idle sleep    -m  no disk sleep    -s  no system sleep while on AC
#
# The assertion lives exactly as long as the command it wraps, so the Mac is
# free to sleep again the moment the run ends.
CAFFEINATE=""
if command -v caffeinate >/dev/null 2>&1; then
  CAFFEINATE="caffeinate -ims"
fi

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
    $CAFFEINATE "$PY" -u main.py daily >> "$LOG" 2>&1
    # Capture the run's own exit code HERE. `STATUS=$?` below sits after the
    # case block, so anything added under it silently becomes what gets
    # reported -- the export succeeding would have masked a failed scrape.
    TASK_STATUS=$?
    # The probe finds new boards continuously, and the cloud run seeds from
    # config/boards.csv -- so without this the two drift apart silently and
    # the cloud keeps scraping a months-old list. Exporting costs a second,
    # and its own success is not the pipeline's success.
    "$PY" -m tools.boards export >> "$LOG" 2>&1 || log "board export failed (not fatal)"
    ;;
  discover)
    # Weekly, not daily: a full sweep is hours of polite probing.
    $CAFFEINATE "$PY" -u main.py discover --names data/mailbox_names.txt --max-slugs 2 >> "$LOG" 2>&1
    ;;
  enrich)
    bash data/companies_build/enrich_loop.sh >> "$LOG" 2>&1
    ;;
  *)
    echo "{\"ok\":false,\"error\":\"unknown task $TASK\"}"
    exit 1
    ;;
esac
STATUS="${TASK_STATUS:-$?}"
log "=== $TASK finished, exit $STATUS ==="

# Rebuild the standalone console so the file on disk is never stale. It owes
# nothing to any account or network, which is the point of it.
if [ "$TASK" = "daily" ]; then
  "$PY" tools/build_dashboard.py "$PROJECT/data/console/dashboard.html" >> "$LOG" 2>&1 \
    && log "rebuilt data/console/dashboard.html"
fi

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
