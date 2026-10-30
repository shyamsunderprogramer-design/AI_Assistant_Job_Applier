# Phase 3 controlled activation preflight — 28 October 2026

**Result: stopped before restart. Recommendation: CONDITIONAL.** The implementation remains
verified by 1,849 passing tests and one opt-in skip. This preflight did not change runtime code,
configuration, database schema or application records, and did not run a mailbox sync or submission.

| Requested check | Evidence / result |
|---|---|
| Application activity | No live ApplyRun, active application item or apply/prepare process observed; task PID markers refer to absent processes |
| Intended web process | PID **48764** before and after; no restart performed |
| Other work | Background careers work remains running; it was not interrupted |
| Backup validity | Latest saved backup's SHA-256 matches its manifest; fresh `PRAGMA integrity_check` returns `ok` |
| Fresh snapshot | Application-state columns, all queue/run/event/artifact/outbox records and ten protected files captured read-only |
| Outbox | Exact live table/index SQL matches the previously verified schema; zero events; no migration attempted |
| Prevent mixed writers | No writer cutover attempted because the restart prerequisite below is not satisfied |
| Loaded-code activation | Not verified in the existing web process; freshly rendered templates alone would not establish it |
| Real-data comparison | All six captured record snapshots and all ten protected-file hashes are unchanged between preflight and postflight |

## Blocker under the user's activation condition 7

`backend/api/__main__.py` unconditionally starts the mailbox and morning-catch-up threads.
`inbox.watch` first checks mail after 90 seconds, then every half hour. `watch_mornings` checks
after 60 seconds, then every 15 minutes. The mailbox is configured and morning catch-up is enabled.
No existing hold/maintenance control prevents these threads from acting during activation checks.
Their delays are not a guarantee that verification will finish before they run, and guessing that
it will finish in time would not meet the user's requirement.

No watcher configuration was changed and no process was stopped. The next approval needed is for
a temporary process-local hold on those watchers during activation verification, followed by release
with their existing settings. That hold has not been implemented. There is no need to recreate the
outbox table or repeat the history migration.

Evidence is retained in
[`logs/phase3-activation-preflight-20261028T175527Z`](../logs/phase3-activation-preflight-20261028T175527Z/).
`preflight.json` records the backup/schema checks and fresh hashes; `postflight.json` records the
comparison after this read-only check. No commit, push, merge, paid AI call, real submission,
historical backfill or Phase 4 work was performed.
