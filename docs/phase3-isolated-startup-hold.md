# Phase 3 isolated startup-hold verification — 29 October 2026 UTC

**Isolated preparation: READY FOR ACTIVATION. Live Phase 3 acceptance remains
CONDITIONAL.** No live restart, watcher activation, mailbox synchronization,
submission, paid AI call, migration, backfill, commit, push, merge or Phase 4
implementation occurred. Activation is not automatically scheduled.

## Implementation

The process environment variable `JOB_APPLIER_HOLD_STARTUP_WATCHERS=1` causes both
watcher starters to return before creating any thread or timer. Their startup
log messages name the held watcher and control. With the variable absent or `0`,
the existing thread names, daemon behavior, initial waits and recurring intervals
are preserved: mailbox 90/1800 seconds; morning catch-up 60/900 seconds.

This is a startup control, not a stop signal to existing workers. No live
environment, `.env`, launchd definition, mailbox settings or discovery setting
was changed. The hold is **not active in the existing web process**. The source
change affects newly invoked watcher starters; it cannot stop already-running
threads. There are no new dependencies or schema changes.

## Changed files in this task

- New `backend/core/startup.py`: shared explicit hold check and safe startup log.
- `backend/apply/inbox.py`: hold before constructing the mailbox thread; the return
  type now permits `None` when held.
- `backend/api/morning_pages.py`: hold before constructing the catch-up thread.
- New `qa/tests/test_startup_watcher_hold.py`: nine regression/integration cases.
- `qa/tests/conftest.py`: decode percent-escaped SQLite URI paths before applying
  the existing production-database guard. Existing assertions and other isolation
  fixtures are preserved.
- This report, the [activation/rollback runbook](phase3-controlled-activation-runbook.md),
  and a current-status note in `docs/auto-apply-v3.md`.

The resume-provider fallback implementation, its tests/report, autonomous-apply
design, existing startup recovery code and all unrelated edits are preserved.

## Tests and evidence

Tests ran in a separate source copy under the evidence directory, with temporary
HOME/TMPDIR, a minimal environment, fictional jobs/documents and mocked providers,
mailbox actions and employer forms. No `.env`, production `data` directory,
applicant/answer files or real resumes were copied. Existing fixtures prohibit
external services, AI CLI execution and opening the source copy's production-
designated database. Crash-test children use temporary database paths and block
network access. Read-only production integrity observations were separate from
test execution and imported no application code.

| Run | Result | Explanation |
|---|---|---|
| focused-01 | 148 passed, 1 failed | Existing process-identity test could not inspect its own temporary children inside the sandbox |
| focused-02 | 318 passed, 1 failed | New isolation regression found a percent-encoded URI bypass in the test guard; the isolated nonexistent database failed to open, with no production access |
| focused-03 | **319 passed, 0 failures** | Process inspection permitted; URI guard corrected; unhandled thread exceptions treated as errors |

The full application suite was not rerun in this task. The 319 relevant tests
cover the nine new startup cases, existing inbox/morning behavior, outbox and
original history-loss regressions, linked history, queue and execution hardening,
prepared documents, required questions, other boards, legacy submission safety,
provider fallbacks and test isolation.

The new startup cases verify the actual `backend.api` entry point with no server
or external services: held startup schedules nothing; direct calls are also held;
normal starts and original timing remain intact; removing the hold schedules
nothing until a new starter call; history recovery still exports one pending
fictional event exactly once across two startups with unchanged schema; encoded
SQLite URIs cannot bypass the test guard.

## History and submission verification

The existing outbox tests pass real child-process deaths before commit, after
commit, during temporary export, before/after publication, and during acknowledgement
for both recording paths. Recovery itself can die and subsequently export exactly
one observable event. Concurrent recorders/recovery preserve identity, timestamps,
attempt/document references and existing legacy history without backfill.

Separately, the read-only observed live outbox SQL was recreated in an **empty
temporary database**. Its table/index definitions exactly match the current model.
Running the narrow migration twice left that existing schema unchanged; integrity
was `ok` and its row count remained zero. No migration ran on the live database.

Legacy next-batch, prepared, job-page, assistant and direct-helper paths still
refuse automatic Submit even when the saved preference enables it. Redirecting
another board to Greenhouse does not activate auto-submit. These are isolated
mock-form results, not a claim of activation in the existing web process.

## Integrity and running processes

- All **23 protected-file paths** have identical before/after hashes or identical
  absence, including application history, saved queues, mailbox state, profile,
  answers, configuration, resume documents, provider repair and autonomy design.
- Read-only row hashes are identical for all **six captured groups**: application
  status/timestamps, items, runs, events, artifacts and outbox. The application
  status selection contains 9,941 rows, including non-default statuses; this is
  not a count of submitted applications. The outbox schema is also unchanged.
- All 364 files in the final isolated-source manifest match the corresponding
  workspace source files after testing.
- Web PID **48764** retained its original 2026-10-26 11:57:35 start time and UID
  501. Careers PID **78022** retained its original 2026-10-15 15:55:59 start time
  and UID 501. Neither was signaled, interrupted or restarted.
- Discovery files were not opened for writing by this work. Discovery is still
  running independently; its database/files are not asserted to be frozen. This
  task did not snapshot or restore discovery state or interfere with its progress.

## Remaining activation work and risks

Follow the runbook only after separate activation authorization and successful
idle-worker, backup/schema and fresh integrity preflight. Persist the hold in the
intended service's own launch definition so supervisor restarts/login do not
silently release it. The installed definition's location must be verified; any
write outside the approved project scope needs explicit scope/access approval.

The hold covers two web startup watchers, not existing threads, manual endpoints
or independent schedules. Ordinary startup still initializes the database and
recovers history/queue state. Pending recovery effects require an explicit
decision before a live restart. Live loaded-code and no-overlap verification
remain outstanding. Do not declare Phase 3 fully complete from these test results.

Evidence: [private diagnostic directory](../logs/phase3-startup-hold-20261029T013101Z/),
[passing log](../logs/phase3-startup-hold-20261029T013101Z/focused-03.log),
[JUnit results](../logs/phase3-startup-hold-20261029T013101Z/focused-03.xml),
[integrity comparison](../logs/phase3-startup-hold-20261029T013101Z/comparison.json),
[schema verification](../logs/phase3-startup-hold-20261029T013101Z/schema-verification.json).
Earlier failed-run logs are preserved under distinct filenames.
