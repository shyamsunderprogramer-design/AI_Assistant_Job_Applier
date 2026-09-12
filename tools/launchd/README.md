# Scheduled runs

These are copies of what is installed in `~/Library/LaunchAgents/`. The copies
are here so the schedule is in version control; launchd only reads the ones in
the home directory.

| agent | when | what |
|---|---|---|
| `…jobapplier.daily` | 07:13 daily | scrape → score → export → digest |
| `…jobapplier.enrich` | 02:23 and 14:23 | continue website/careers enrichment |
| `…jobapplier.discover` | Sunday 03:07 | probe for new boards |

All three call `tools/daily_pipeline.sh <task>`, which takes a lock so two runs
can never overlap, logs to `data/daily_run.log`, and notifies only when there is
something to say — a new strong match, or a failure.

## Managing them

    launchctl list | grep jobapplier                     # are they registered
    launchctl kickstart -p gui/$UID/com.ssdaggupati.jobapplier.daily   # run now
    launchctl bootout gui/$UID/com.ssdaggupati.jobapplier.daily        # stop
    launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.ssdaggupati.jobapplier.daily.plist

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
