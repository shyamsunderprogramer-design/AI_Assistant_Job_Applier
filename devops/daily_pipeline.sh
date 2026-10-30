#!/bin/bash
# One scheduled run of the daily pipeline: scrape -> score -> export -> digest.
#
# Written to be driven by something that is not a person: a scheduler gets no
# terminal, no PATH it did not set, and no chance to answer a prompt. So this
# hardcodes the interpreter, logs everything with timestamps, refuses to run
# twice at once, and exits non-zero when a stage fails so the caller can alert.
#
#   devops/daily_pipeline.sh [daily|discover|enrich|backup|health]
#
# Prints a one-line JSON summary on stdout as its last line, for a scheduler
# to branch on. Human-readable output goes to data/daily_run.log.

set -uo pipefail

# The project is the folder this script sits in, wherever the app was installed (pwd -P: the
# real path, the one the web app matches to find this run). PIPELINE_PROJECT exists for the
# tests (qa/tests/test_daily_pipeline_sh.py); launchd never sets it.
PROJECT="${PIPELINE_PROJECT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
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

# The 6 AM start can be switched off on the Jobs page (data/daily_auto_off). The
# scheduled tick then does nothing, and the run happens only when Start on that
# page is pressed -- which sets MANUAL=1 and runs whatever the switch says.
if [ "$TASK" = "daily" ] && [ "${MANUAL:-0}" != "1" ] && [ -f "$PROJECT/data/daily_auto_off" ]; then
  log "daily: the 6 AM start is switched off on the Jobs page — not starting"
  echo '{"ok":true,"skipped":"switched off"}'
  exit 0
fi

# Nothing to look for before there is a resume and a search made from it: a new install's first
# 6 AM, or Start pressed before the Welcome page's first step (backend/core/setup_state.py).
if [ "$TASK" = "daily" ]; then
  NOT_READY="$("$PY" -m backend.core.setup_state why 2>/dev/null)"
  if [ -n "$NOT_READY" ]; then
    log "daily: not starting — $NOT_READY"
    echo '{"ok":true,"skipped":"not set up"}'
    exit 0
  fi
fi

# mkdir is the atomic primitive here: macOS has no flock, and a lock FILE can
# be left behind by a kill -9 with no way to tell it from a live run.
PIDFILE="$PROJECT/data/${TASK}.pid"
# Whether the run holding the lock is alive: its pid, still running this script.
owner_alive() {
  local owner
  owner="$(cat "$PIDFILE" 2>/dev/null)"
  [ -n "$owner" ] && ps -p "$owner" -o command= 2>/dev/null | grep -q "daily_pipeline.sh"
}
if ! mkdir "$LOCK" 2>/dev/null; then
  # A lock whose run is alive is kept however old it is: a scheduled run can wait
  # hours for the lid to open, and breaking its lock would start a second run. One
  # whose run is gone (killed, or stopped from the Jobs page, neither of which runs
  # the trap below) is cleared at once instead of costing the next morning's run.
  # No pid file at all is a run from before pids were kept: the old 6-hour rule.
  if owner_alive || { [ ! -f "$PIDFILE" ] && [ -z "$(find "$LOCK" -maxdepth 0 -mmin +$((STALE_HOURS * 60)) 2>/dev/null)" ]; }; then
    log "$TASK already running — skipping this tick"
    echo '{"ok":true,"skipped":"already running"}'
    exit 0
  fi
  log "clearing a lock its run left behind"
  rmdir "$LOCK" 2>/dev/null
  mkdir "$LOCK" 2>/dev/null || { echo '{"ok":false,"error":"locked"}'; exit 0; }
fi
echo $$ > "$PIDFILE"
trap 'rmdir "$LOCK" 2>/dev/null; rm -f "$PIDFILE"' EXIT

SUMMARY=""
log "=== $TASK starting ==="

# Wait for the Mac to be open before a scheduled run starts.
#
# launchd fires the 6 AM tick even in a Power Nap wake with the lid shut, and on
# battery caffeinate cannot hold the Mac awake: on 18 Oct the run started at 06:02
# in such a wake and crept along a few boards per wake-up for four hours, until the
# lid opened at 10:07. A Power Nap wake has no graphics ("CPU Disk Network"); a real
# one does ("CPU Graphics Audio Network"), lid open or on an external display. So a
# scheduled run waits for graphics, holding its lock, and starts within a minute of
# the lid opening. A run started from the Jobs page needs no waiting: someone is there.
CLOUD_FLAG=""
if [ "$TASK" = "daily" ]; then
  # A scheduled morning takes the GitHub run's postings when that has read the boards (laptop shut
  # or off), and reads them here when it has not (devops/cloud_results.py). Start now reads them here.
  [ "${MANUAL:-0}" = "1" ] || CLOUD_FLAG="--cloud"
  if [ "${CATCH_UP:-0}" = "1" ]; then
    log "started when the Mac came on — the 6 AM start had not run (the Mac was off)"
  fi
  if [ "${MANUAL:-0}" = "1" ]; then
    log "started from the Jobs page"
  elif command -v pmset >/dev/null 2>&1 && ! pmset -g systemstate 2>/dev/null | grep -q "Graphics"; then
    log "waiting for the Mac to be opened — it is asleep with the lid shut"
    until pmset -g systemstate 2>/dev/null | grep -q "Graphics"; do sleep "${WAKE_POLL:-60}"; done
    log "the Mac is open — starting"
  fi
fi
case "$TASK" in
  daily)
    $CAFFEINATE "$PY" -u main.py daily $CLOUD_FLAG >> "$LOG" 2>&1
    # Capture the run's own exit code HERE. `STATUS=$?` below sits after the
    # case block, so anything added under it silently becomes what gets
    # reported -- the export succeeding would have masked a failed scrape.
    TASK_STATUS=$?
    # The probe finds new boards continuously, and the cloud run seeds from
    # backend/config/boards.csv -- so without this the two drift apart silently and
    # the cloud keeps scraping a months-old list. Exporting costs a second,
    # and its own success is not the pipeline's success.
    "$PY" -m data_engineering.boards export >> "$LOG" 2>&1 || log "board export failed (not fatal)"
    # The GitHub run reads the same list, encrypted: sent only when it changed.
    "$PY" -m devops.cloud_results push-boards >> "$LOG" 2>&1 || log "board list not sent to the cloud (not fatal)"
    # Ready-made "skills this job wants" for the best jobs, so the job page
    # opens with them. Uses the writing model; a failure is not the pipeline's.
    $CAFFEINATE "$PY" -u main.py suggest --top 10 >> "$LOG" 2>&1 || log "suggestions failed (not fatal)"
    # No resume is tailored here: only when the person presses Auto-apply on a job, or
    # Prepare now on the Apply page. Their choice on 21 Oct 2026, after a morning that
    # tailored resumes for 10 jobs they had not picked (main.py prepare-apply, which ran here).
    ;;
  discover)
    # Weekly, not daily: a full sweep is hours of polite probing.
    $CAFFEINATE "$PY" -u main.py discover --names data/mailbox_names.txt --max-slugs 2 >> "$LOG" 2>&1
    ;;
  enrich)
    bash data/companies_build/enrich_loop.sh >> "$LOG" 2>&1
    ;;
  backup)
    # Nightly: jobs.db and the person's own settings, to the Storage drive.
    SUMMARY="$("$PY" devops/backup_db.py 2>> "$LOG")"
    TASK_STATUS=$?
    log "backup: $SUMMARY"
    ;;
  health)
    # Weekly: what went wrong unattended. Shown at /health in the web app.
    SUMMARY="$("$PY" devops/health_report.py 2>> "$LOG")"
    TASK_STATUS=$?
    log "health: $SUMMARY"
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
  "$PY" devops/build_dashboard.py "$PROJECT/data/console/dashboard.html" >> "$LOG" 2>&1 \
    && log "rebuilt data/console/dashboard.html"
fi

# The summary is what a scheduler reads to decide whether to notify.
[ -n "$SUMMARY" ] || SUMMARY="$("$PY" devops/run_summary.py "$TASK" "$STATUS" 2>/dev/null)"
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
if s.get("task") == "health":
    from backend.config.loader import web_url
    n = len(s.get("problems") or [])
    print((f"Weekly health: {n} thing(s) to look at" if n else "Weekly health: all good")
          + f" — {web_url('/health')}|" + ("Basso" if n else ""))
elif not s.get("ok"):
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
