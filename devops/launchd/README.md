# Scheduled runs

These are copies of what is installed in `~/Library/LaunchAgents/`. The copies
are here so the schedule is in version control; launchd only reads the ones in
the home directory.

| agent | when | what |
|---|---|---|
| `…AI_Assistant_Job_Applier.daily` | 06:00 daily | scrape → score → export → digest |
| `…AI_Assistant_Job_Applier.enrich` | 02:23 and 14:23 | continue website/careers enrichment |
| `…AI_Assistant_Job_Applier.discover` | Sunday 03:07 | probe for new boards |
| `…AI_Assistant_Job_Applier.backup` | 02:40 daily | jobs.db + your settings → `/Volumes/Storage/AI Job Applier backups/<date>/`, last 14 kept |
| `…AI_Assistant_Job_Applier.health` | Sunday 09:30 | weekly health report → `data/reports/`, shown at `/health` |

All of them call `devops/daily_pipeline.sh <task>`, which takes a lock so two runs
can never overlap, logs to `data/daily_run.log`, and notifies only when there is
something to say — a new strong match, or a failure.

## Managing them

    launchctl list | grep AI_Assistant_Job_Applier                     # are they registered
    launchctl kickstart -p gui/$UID/com.AI_Assistant_Job_Applier.daily   # run now
    launchctl bootout gui/$UID/com.AI_Assistant_Job_Applier.daily        # stop
    launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.AI_Assistant_Job_Applier.daily.plist

After editing a plist, `bootout` then `bootstrap` it — launchd caches the old
definition otherwise.

## What to check when a run seems to have not happened

1. `data/launchd_<task>.log` — what launchd itself captured, including a crash
   before the script got going.
2. `data/daily_run.log` — the run's own output.
3. `ls data/*.lock` — a lock left behind by a hard kill. The script breaks one
   older than six hours by itself.

The Mac must be awake at the scheduled time. launchd runs a missed job once on
wake, so a closed lid delays a run rather than skipping it.
