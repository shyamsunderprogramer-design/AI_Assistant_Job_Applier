# Phase 3 live activation — 29 October 2026

**Result: technical PASS.** The restarted web app runs the tested Phase 3 code, including the
history repair and the rule that older paths never press Submit; the mailbox watcher and morning
catch-up are held; no application, queue, history or settings record changed. Decisions recorded
the same morning: keep *Apply to next 10*, the morning's prepared runs, a job page's Auto-apply and
the assistant always waiting for the person's Submit (until Phase 4); keep the green-card rule
(a blank answer is "needs your answer", not "not eligible"); restart without waiting for the
careers finder, which is a separate process and does not write the application records.

No real application, paid AI call, mailbox sync, migration, backfill, commit or push was made.

## Saved first (both job-discovery runs untouched)

`/Volumes/Storage/AI Job Applier backups/before-phase3-activation/2026-10-29/` (README and
`manifest.json` with every file's SHA-256):

- `data/jobs.db` (1,253,429,248 bytes) and `data/companies.db` (572,669,952 bytes): SQLite online
  backups taken while the finders kept writing; `integrity_check` ok on both copies.
- The two finders' progress and results (`data/probe_state.json`, `data/careers/state.json`,
  `progress.json`, `careers_sites.csv`, `new_boards.csv`, name-domain files), the application
  history, queue files, settings and answer files, and the original launch definition.
- At that moment: board finder 500,000 of 1,361,080 names, 14,959 new boards; careers finder
  1,279,484 of 1,360,831 companies, 134,547 careers sites. Both resume where they stop
  (`devops/probe_runner.py` `resume_point`; `careers_finder.py` "it resumes where it stopped").

## The restart

| Check | Result |
|---|---|
| Before: apply / prepare runs | None running (task status idle; no live run; no job mid-way) |
| Discovery processes | Board finder PID 26664 and careers finder PID 78022 each in their own process group, parent launchd: not part of the web app |
| Launch definition | `~/Library/LaunchAgents/com.AI_Assistant_Job_Applier.webapp.plist`: one key added, `EnvironmentVariables.JOB_APPLIER_HOLD_STARTUP_WATCHERS = "1"`; `plutil -lint` OK; original archived beside the backup |
| Pages before restart | Every page rendered through the current code: all OK |
| Restart | `launchctl bootout` of the web app only; old PID 48764 (since 26 Oct 11:57) exited in about 2 s; no other web process; `launchctl bootstrap` started PID **15416** at 08:47 |
| Hold | Its log: "Startup watcher inbox-watch held…" and "…morning-catch-up held…; no thread or timer started"; nothing from the mailbox or catch-up in the hours since |
| Loaded code | All 364 files of the tested source manifest (`logs/phase3-startup-hold-20261029T013101Z/source-manifest.json`) match the workspace; *Apply to next 10* now answers `auto_submit: false` from the running app |
| Startup recovery | Ran (it exports pending history first): no error; history outbox empty, nothing pending |
| App health | No tracebacks; `/`, `/apply`, `/apply/questions`, `/applications`, a job page, `/health` all 200 |
| Records, before vs after | Identical: 19 applications (status and day), queue items 12, runs 2, steps 223, documents 23, outbox 0 and its table definition, and 15 settings, answers, history and resume files |
| Discovery after | Both finders kept their PIDs and start times and kept moving (500,000 → 506,080; 1,279,484 → 1,303,954 by 11:37) |
| Tests (made-up data) | Full suite 1,868 passed, 1 opt-in skip (this morning, same code); submission, history, recovery, hold and chosen-jobs suites again after the restart: 270 passed. A tripwire refusing any direct opening of the person's databases saw none |

## Found on the way

- **The morning run's fetch failed two days running** ("database is locked", 28 and 29 Oct): three
  programs write `data/jobs.db` at once (careers finder, board finder, the daily fetch). On the 28th
  no backup was running; on the 29th the backup above ended 18 s before the failure, so it may have
  added to the load. The other stages still ran. Not changed here (Job Discovery).
- The app had been running the code it loaded at 26 Oct 11:57 while new processes it started ran the
  current code: an old and a new history writer side by side. The restart ended that.

## Still open

1. **The hold stays on** until the person approves releasing it: remove the one key from the launch
   definition (or restore the archived copy), then `launchctl bootout` and `bootstrap` the web app
   again. Until then replies in the mailbox do not move applications on by themselves, and a missed
   6 AM run is not caught up by the web app (the separate `com.AI_Assistant_Job_Applier.daily` job
   still runs at 6 AM).
2. All of Auto Apply v3.0 is uncommitted; committing needs the person's word.
3. Limitations from the earlier reports stand: older paths have no Phase 3 document or question
   checks (they wait for Submit instead); the Jobs page moves as its status cards fill in; the essay
   writer's save is outside the answers lock.
