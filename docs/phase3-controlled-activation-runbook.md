# Controlled Phase 3 activation and rollback

Prepared for a later, explicitly authorized activation. **No step below was
executed during the isolated watcher-hold work.** Do not automatically start this
procedure when careers discovery finishes.

## Startup control

Set `JOB_APPLIER_HOLD_STARTUP_WATCHERS=1` in the intended web service's environment
**before its Python process starts**. Only the exact value `1` enables the hold;
absent or `0` preserves existing behavior. Do not put it in the application's
`.env`: watcher startup precedes the existing configuration loader.

Both `inbox.watch` and `watch_mornings` return before constructing a thread. They
schedule no future callback. Each logs:

```text
Startup watcher inbox-watch held by JOB_APPLIER_HOLD_STARTUP_WATCHERS=1; no thread or timer started
Startup watcher morning-catch-up held by JOB_APPLIER_HOLD_STARTUP_WATCHERS=1; no thread or timer started
```

The control does not terminate an existing watcher, disable direct manual mailbox
actions, disable an independent scheduled launchd daily job, or stop discovery.
It does not suppress database initialization or history/queue recovery. These
facts are activation prerequisites, not reasons to modify those other systems.

## Preflight — stop on any failed prerequisite

1. Recheck the identity of careers PID 78022 using command, owner and original
   start time. Do not signal it. If it is still the careers worker, stop this
   activation attempt. Do not infer completion from PID absence alone; check the
   saved discovery completion state and whether a replacement worker exists.
2. Identify the actual web process and launchd service from their owner, command,
   working directory and loaded service definition. Previously observed: PID
   48764, UID 501, started 2026-10-26 11:57:35 local; expected label
   `com.AI_Assistant_Job_Applier.webapp`. These are historical identifiers, not
   sufficient authority to stop whatever now has that PID or label.
3. Verify no application/prepare run, submission, migration, mailbox update,
   discovery, scheduled pipeline or other conflicting writer is active. Check
   process identities and database run/item states; stale PID markers alone are
   insufficient. Confirm an independent schedule will not start during the
   verification window. Do not disable or stop unrelated services to satisfy this.
4. Make a fresh consistent SQLite backup using the backup API, not a copy of the
   main file that omits WAL contents. Verify its SHA-256 and `integrity_check`.
   Preserve prior backups. Capture application state/timestamps, all queue/run/
   event/artifact/outbox rows, exact outbox table/index definitions, history files,
   profile/answer/resume files, mailbox state, configuration and task markers.
   Store private snapshots separately from shareable diagnostic summaries.
5. Compare the outbox schema with the tested schema. Do not recreate its table or
   run an unnecessary migration. Check pending outbox events and recovery-eligible
   queue items. If recovery would change real records, stop for an explicit
   decision covering those effects; the hold does not prevent recovery writes.
6. Hash the tested source files and compare them with the current workspace.
   Preserve every existing edit and untracked file. Rerun affected isolated tests
   if any runtime file changed after verification.

## Stage the held service definition

1. Archive the exact actual service definition, its path, permissions, ownership,
   label, program arguments, working directory, environment and launch state in
   a fresh activation evidence directory. Do not assume the repository template
   is the currently installed definition. Do not log credential values.
2. Make a temporary candidate copy. Preserve every key and existing environment
   variable; add only `EnvironmentVariables.JOB_APPLIER_HOLD_STARTUP_WATCHERS = "1"`.
   Validate the plist and compare its parsed contents with the archived version.
   Only that one setting may differ. Preserve mailbox settings and schedules.
3. The intended service must retain this hold through supervisor restarts and
   login/reboot until restoration is approved. A shell `export`, global
   `launchctl setenv`, or a temporary bootstrap that reverts to an unheld installed
   definition on the next login is insufficient. Use the actual per-service
   definition that launchd will reload, with its original bytes retained for
   restoration. If that definition is outside the approved project scope, obtain
   explicit scope/access approval for that file before changing it. No such file
   was changed by this isolated work.

## Controlled cutover

1. Recheck idle status and take the final snapshot immediately before cutover.
2. Boot out **only the verified web service** (for the historical identity,
   `launchctl bootout gui/501/com.AI_Assistant_Job_Applier.webapp`). This operation
   is for the future approved activation, not permission to run it now.
3. Wait with a bounded timeout for the exact old process to exit. Verify no old
   watcher, application worker or history writer remains. If exit or ownership
   is uncertain, stop; do not force-kill or start a replacement alongside it.
4. Install the validated held service definition using the approved service path,
   preserving its permissions/ownership. Bootstrap only that definition, e.g.
   `launchctl bootstrap gui/501 <verified-held-plist-path>`. Never start a second
   development web server in parallel. Record the new PID, owner and start time.
5. Confirm the new service's effective environment includes the hold. Keep it set
   throughout verification and afterwards. Do not release it automatically.

## Post-start verification

- Correlate the new PID/start time/working directory/launch command with the tested
  source manifest. Confirm the startup entry point imports the patched recovery
  and runner modules from this checkout, with no alternate PYTHONPATH or stale
  deployment. Retain both new-startup hold messages and any recovery errors.
  Served templates alone are not proof that the backend code was activated.
- Verify no `inbox-watch` or `morning-catch-up` thread was started, no other process
  is acting as a writer, and no mailbox/catch-up launch or file update occurred.
  If runtime identity or execution evidence is insufficient, do not declare PASS.
- Use a static GET such as `/static/theme.css` to check HTTP availability. Do not
  use apply/start/sync controls, open employer forms, or call routes that invoke
  `cfg`, `recover`, or other writers as if they were read-only health checks.
- Run fictional-data verification in the isolated source/database environment;
  never insert test records into the live database. Reconfirm manual-only legacy
  and cross-board redirect behavior there. No real submission is a health check.
- Compare all application, queue, history, outbox, mailbox and protected-file
  snapshots. The approved service-definition hold change and activation logs are
  expected operational changes. Any other difference needs an established cause;
  do not repair or conceal it by restoring an old snapshot.

Accept Phase 3 only after the required live identity, no-overlap, hold, backend
activation and data-integrity evidence passes. Record any independent remaining
limitations from the earlier recovery report.

## Failure rollback and later restoration

- If startup or verification fails, stop only the newly started verified web
  service under the controlled activation authorization. Preserve diagnostics
  and all databases, queues and outbox events. Leave the hold in the service
  definition. Do not restore an old database, recreate tables or backfill history.
- Do not run an older pre-outbox history writer, or code that ignores the hold,
  as a quick rollback. That can lose history or start mailbox work. Leaving the
  web service stopped pending a safe correction is preferable; report this state.
- Do not use Git reset/clean/revert/stash or restore whole source directories.
  Any code rollback must be an individually reviewed patch that preserves the
  user's existing edits and retains the outbox and submission safeguards.
- When the user separately approves watcher restoration, verify no conflicting
  work, snapshot again, and remove only the hold variable from the actual web
  service definition. Restore the archived definition only if all other settings
  still match; otherwise preserve intervening changes.
- Perform another controlled web restart without overlap. Removing the variable
  alone does not create threads in the existing held process. Normal startup then
  resumes mailbox checks after 90 seconds and morning catch-up checks after 60
  seconds, with their original recurring intervals. Restoration approval must
  cover these potential real-data effects. Do not manually trigger a sync.
