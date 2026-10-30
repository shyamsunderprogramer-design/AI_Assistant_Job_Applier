# Phase 3 history-durability repair — 27–28 October 2026

**Implementation and isolated verification: PASS. Overall acceptance: CONDITIONAL pending controlled
live restart and activation verification.** The previous two history-loss regressions now pass.
The latest full suite finished with **1,849 passed, 1 skipped, no failures**. No Phase 4 work,
new submission capability, real submission, paid AI call, commit, push or merge was performed.

This report supersedes the history-blocker status in
[the recovery report](phase3-recovery-verification.md). It does not erase that report's original
evidence or remaining limitations unrelated to this narrowly approved repair.

## 1. Root cause

`runner.record_application` committed the job's Applied state before appending its JSONL record.
`execution.answer` similarly committed the chosen-item/job state before writing history and marking
its documents sent. A failed append or process crash left a committed application without history;
retry recognized Applied and refused to recreate the record. There was no durable payload to recover.

Both original tests reproduced that failure in a fresh isolated source copy before this repair:
**2 failed**. Neither regression's assertions were weakened.

## 2. Implementation and migration

The additive `apply_history_outbox` table has:

| Field | Purpose |
|---|---|
| `event_id` | Stable UUID primary key, also exported as `history_event_id` |
| `job_id` | Indexed application job identifier |
| `payload` | Complete immutable JSON history, including timestamp, run/attempt and document references |
| `destination` | Absolute history-file destination captured when the event is created |
| `created_at` | Event creation time |
| `exported_at` | Nullable export acknowledgement, indexed for pending recovery |

Both confirmed-application recording paths enqueue this payload **inside the same SQLite transaction**
as the job/item changes. Failure to serialize or insert it rolls back the state transition. Linked
document versions are marked sent in that transaction. The original application timestamp is retained;
recovery does not derive a new timestamp, job title, document list or run/attempt from current state.

Export runs after commit. A stable sidecar file lock serializes cooperating exporters. Under that
lock the exporter checks the event ID, preserves all previous history bytes, writes a complete new
file to a private temporary path, fsyncs it, atomically replaces the history file, and fsyncs its
directory before acknowledging the event in SQLite. Existing lines are not reserialized or rewritten
semantically. A crash before publication leaves the previous history intact; a crash after publication
but before acknowledgement is deduplicated by event ID. Conflicting content for an existing event ID
is rejected, not silently accepted. Persistent errors leave the event pending.

`execution.recover`, already called during app startup and queue recovery, exports pending events.
Repeated recording of an already-recorded job may retry its pending export but does not invent a new
event. Existing conditional state updates serialize competing confirmations. Outbox acknowledgement
does not delete its original payload. Normal application-history readers tolerate the added ID field.

`ensure_history_outbox` is a narrow migration entry point that creates only the empty table and its
two indexes. Normal `Base.metadata.create_all` also creates it on startup. There is **no historical
backfill or status migration**. Older code ignores the additive table; rollback should leave the table
and pending events intact. Older non-cooperating history writers must not run alongside the new
exporter, so a clean activation remains required.

## 3. Regression results

- Baseline original history-loss regressions: **2 failed**, establishing the defect.
- First repair-focused run: **181 passed, 1 sandbox-related process-inspection failure**. Thread
  warnings exposed an unnecessary unique-job constraint when existing stress fixtures reset a job
  between independent scenarios. That constraint was removed; event identity and the existing atomic
  state claim provide deduplication. The assertions were retained.
- Corrected focused run: **200 passed**, with unhandled-thread warnings treated as errors.
- Full suite: **1,849 passed, 1 skipped in 201 seconds**. The skip is the opt-in live Word test.
  This includes Phase 1–3, legacy submission gates, multiprocessing, history, and desktop/mobile
  browser regressions. No runtime changes followed this suite.

Tests ran in an isolated source copy with temporary databases, fictional applications, mocked models
and employer pages. No real employer form was opened or filled. Test children block network access.

Retained evidence:
[original failing regressions](../logs/phase3-history-repair-20261027T154611Z/baseline-regressions.log),
[focused results](../logs/phase3-history-repair-20261027T154611Z/verification/focused-02.log),
[full-suite log](../logs/phase3-history-repair-20261027T154611Z/verification/full-suite.log),
[JUnit](../logs/phase3-history-repair-20261027T154611Z/verification/full-suite.xml), and
[198 browser diagnostic files](../logs/phase3-history-repair-20261027T154611Z/verification/browser-diagnostics.zip).
The seven runtime/test source files were byte-compared against the passing isolated suite after the
agent interruption; all matched. Their hashes are retained beside the test logs.

## 4. Crash, concurrency and migration verification

For both the runner and chosen-confirmation paths, real child processes terminate at:

- Before database commit, after the outbox insert has been flushed.
- After commit, before export begins.
- During a partial temporary-file write.
- Before atomic publication.
- After publication, before database acknowledgement.
- During the uncommitted acknowledgement transaction.

For each committed case, recovery itself is then killed after publication. Two subsequent fresh
recovery processes produce exactly one observable line, retain the original identifiers/payload/time,
and acknowledge the event. Uncommitted cases retain neither Applied nor an outbox event.

Six concurrent recorders produce one application event. Six concurrent recovery processes export
three pending/recorded applications exactly once each. Existing concurrent confirmation and queue
tests also pass. Tests verify that legacy lines remain byte-for-byte prefixes and old Applied jobs
without outbox events are not backfilled.

The migration was also applied twice to an isolated copy of the verified real database backup, followed
by normal engine startup. All **8 existing tables' complete row hashes and existing SQL schema objects
remained unchanged**. Only the outbox table and its two indexes appeared; its row count remained zero.
SQLite integrity was `ok` before and after.

## 5. Real-data integrity and backup evidence

Evidence folder: `logs/phase3-history-repair-20261027T154611Z/` (ignored by Git, private directory).

- Initial online SQLite backup: **1,204,559,872 bytes**, integrity `ok`; SHA-256 and all-table row
  manifests retained. It predates the outbox table. SQLite's backup API safely includes WAL state.
- Fresh pre-activation backup: **1,214,558,208 bytes**, integrity `ok`, SHA-256
  `4f243b469724be003dc5c039525c7eb5a0ff9a8992e8b1d0a60d93fff39a89ae`.
- The controlled migration script stopped **before any DDL** because the live table already existed.
  It matches the tested schema and contains **0 events / 0 pending events**. Normal application startup
  can create it automatically; the exact creating process and time were not established. This must
  not be described as a completed controlled migration or a verified web-process activation.
- Compared with the initial backup, existing schema objects are unchanged; only the expected new
  table/indexes are present. All **19 recorded application states/timestamps** remain unchanged.
- **9 of 10 protected-file hashes are unchanged**, including the application history, applicant profile,
  answers and resume additions. `data/apply/unconfirmed.json` changed concurrently and was preserved.
- Apply-run and document-artifact tables are unchanged. Live queue items/events changed: a pending
  confirmation became failed, six queued items were cancelled, and finished entries were hidden.
  These changes were observed outside the isolated tests; their actor was not established. No existing
  records were restored, overwritten or silently reconciled by this repair. Whole-database or whole-
  workspace immutability cannot be claimed while the user's running application remains active.

## 6. Live safety-gate activation

Repeated read-only process/database checks found no active application/prepare process, no running
ApplyRun and no item currently being validated/prepared/filled or waiting in an open form. Task PID
markers referred to processes that no longer existed. The web process remains PID **48764**.

**Restart not performed.** The configured mailbox watcher starts 90 seconds after startup and may
record real email-driven application updates; morning catch-up is also enabled. The user's activation
instruction explicitly requires approval when restarting would affect real data. Restart is therefore
pending approval for those normal background effects. No mailbox was accessed to test that behavior,
and no background job was deliberately started or stopped.

Legacy manual-only gates and redirect restrictions remain intact and pass their regression tests.
Their activation in the old web process cannot be asserted from a freshly rendered template alone.
After approval, recheck activity, restart only the idle web service, verify the new process and served
manual-mode response without opening a form, and compare application/profile integrity again.

## 7. Files modified by this approved repair

- New `backend/apply/history_outbox.py`: transactional payload and idempotent export.
- `backend/apply/runner.py`: enqueue in the state transaction; export committed payloads.
- `backend/apply/execution.py`: atomic chosen confirmation/document state; recovery export.
- `data_engineering/db/models.py`: additive outbox model.
- `data_engineering/db/migrate.py`: narrow additive migration entry point.
- New `qa/tests/test_history_outbox.py`: 18 durability/migration/concurrency cases.
- `qa/tests/test_history_recovery_gap.py`: explanatory docstring only; original assertions retained.
- `README.md`, `docs/auto-apply-v3.md`, `docs/phase3-recovery-verification.md`, and this report:
  current status with prior evidence preserved.
- Ignored evidence folder: baseline diff/status, backup manifests, read-only diagnostics, isolated
  migration/activation scripts, test logs/XML and browser diagnostics. Database backups are kept
  separately from browser/test archives and contain private application data.

Unrelated uncommitted and untracked files were preserved. No learning/RAG implementation was added.

## 8. Remaining risks

- Controlled live activation and its verification remain outstanding; mixed old/new writers are not
  a supported deployment state.
- A full disk or persistent filesystem error can delay history export; the original event remains
  durable in SQLite. Recovery must eventually run successfully to make the JSONL view current.
- Exactly-once export assumes cooperating writers and intact storage. Manual log edits/deletion,
  older append-only writers, filesystem loss and corrupt databases are outside that guarantee.
- Atomic replacement copies the current JSONL file for each new event; this is appropriate for the
  existing small personal history, not an unbounded high-volume stream.
- No missing historical record was reconstructed. Other previously documented Phase 3 limitations
  (legacy preflight coverage, layout/accessibility and essay-write concurrency) are unchanged.

## 9. Acceptance recommendation

**CONDITIONAL.** The demonstrated history-loss defect is repaired and every enabled regression
passes, including process deaths and concurrent recovery. Final live acceptance requires the approved
controlled restart and verification that the serving process uses the patched safety/history code.
No Phase 4 or unattended-submission expansion is needed for that step.

The subsequently approved controlled activation was checked on 28 October and stopped before
restart under its stricter watcher condition: there is no existing hold for background watchers.
See the [activation preflight](phase3-live-activation-preflight.md). PID 48764 remains unchanged;
the tested implementation is not yet verified in that serving process.
