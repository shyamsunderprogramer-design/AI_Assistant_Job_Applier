# Phase 3 recovery verification — 26–27 October 2026

> **Superseding history result:** the separately approved
> [history-durability repair](phase3-history-durability-repair.md) resolves the two failures below.
> Its full suite has 1,849 passes and one opt-in skip. Overall acceptance is now CONDITIONAL pending
> controlled live activation. The original recovery findings and results below are retained as evidence.

**Acceptance recommendation: FAIL.** Legacy automatic-submission gaps are gated off, and the
existing Phase 1–3 regression groups pass. Two new acceptance tests demonstrate that an application
can be committed as Applied while permanently losing its linked JSONL history after a write failure.
This must be resolved before accepting the durability/history guarantee.

**Recovery suite before the later sign-in/question report:** 1,818 passed, 2 failed, 1 skipped;
1,821 collected, about 177 seconds. Follow-up verification is recorded in the addendum below.
**Latest complete follow-up:** 1,828 passed, 2 failed, 1 skipped (1,831 collected); an additional
new-tab sign-in regression passed separately. The same two history failures remain the only failures.
The skipped test is the opt-in live Word test. No real employer form was opened, filled or submitted
by this verification, and no paid AI service was used. No commit, push, merge, reset, stash, cleanup,
production migration or Phase 4 work was performed.

Evidence is retained under
[`logs/phase3-recovery-2026-10-27-k7a2izw8`](../logs/phase3-recovery-2026-10-27-k7a2izw8/).
The [final test log](../logs/phase3-recovery-2026-10-27-k7a2izw8/last-suite.log),
[JUnit results](../logs/phase3-recovery-2026-10-27-k7a2izw8/last-suite.xml), and
[diagnostic archive](../logs/phase3-recovery-2026-10-27-k7a2izw8/verification-evidence.zip)
include unsuccessful attempts as well as successful runs. Logs are already ignored by Git.

## 1. Repository state before work

- Branch: `reorganise-by-role`; HEAD: `392bf94` (“Where a job is and who can take it…”).
- 62 modified tracked files and 33 untracked files; no staged changes. The full initial status,
  recent commits, tracked diff, untracked source snapshots and SHA-256 manifest were captured.
- Most of Phase 1–3 was uncommitted; `execution.py`, `chosen.py`, `chosen_pages.py`, the Phase 3
  document and many tests were untracked. Ownership cannot be proven from Git for uncommitted work.
- Read README constraints, the Phase 3 proposal/handoff, queue, runner, eligibility, preparation,
  history, migrations, routes and relevant tests. No applicable `AGENTS.md` or `CLAUDE.md` was found.
- Initial real database: 17,547 jobs; all four chosen-workflow tables empty. The unchanged history
  file establishes that 18 applications were present at takeover. The handoff's 15 was historical.
- Resume work, deployment/operations changes, configuration, audit documents and tooling files from
  other sessions were preserved. Existing discovery processes and memory hooks continued changing
  their own files during this work; their changes were not rolled back.

## 2. Claude work already completed

Verified in source and regression tests, rather than accepted solely from the handoff:

- Phase 1: durable items/runs/events, conditional state claims, live-entry/run uniqueness, cancel,
  pause, retry, recovery and duplicate-confirmation protections.
- Phase 2: event streaming, reconnect/fallback polling, per-item event versions and progress history.
- Phase 3: five eligibility decisions, stored evidence/unknowns, prepared-document checksums and
  attempt copies, questions-first handling, assisted boards, and linked application records.
- Auto-apply redraw stabilization: unchanged rows/buttons retain their DOM nodes/text.
- Test fixture repairs: actual temporary document files and a multiprocessing helper written once
  through a draft-and-rename operation.

These features existed on arrival. Their remaining history failure is described below; the original
documentation's “recorded in one step” wording overstated database-plus-file atomicity.

## 3. Incomplete or inconsistent work discovered

- Legacy entry points had deliberately retained old submission rules. The documentation both
  acknowledged this and elsewhere claimed that the new restrictions applied globally.
- Assistant confirmation promised manual Submit while the legacy runner could submit automatically.
- The handoff said each original browser stop now had a cause, then admitted their diagnostics had
  been overwritten. Later reproductions establish defects, not the causes of those particular runs.
- Missing green-card information was treated as a negative answer for a non-citizen.
- A short/pre-filled form could evade the generic follower's field-count heuristic, letting its
  “Apply now” submit button be treated as navigation.
- A database commit and a JSONL append are separate operations. Recovery does not reconstruct a
  missing history line after the commit succeeds and the append fails.
- A digest test's assumed high score depended on available search-profile context. It failed in a
  clean source copy without the real profile. Its fixture now points away from the personal profile,
  and its threshold check uses known high/low scores, asserting both inclusion and exclusion.
- All 313 Python files inspected parsed successfully. No partially written Python file was found.

## 4. Submission safety by entry point

“Automatic” below means the application itself presses the final Submit control. Filling can still
upload documents to an employer; no such live interaction was used for verification.

| Entry point | Eligibility enforced | Phase 3 fingerprints / required questions | Before recovery | After recovery |
|---|---|---|---|---|
| Apply to these | Five decisions, rechecked before opening; unanswered eligibility requirements wait | Yes / Greenhouse and JazzHR ahead; live form checks also apply | Automatic only for Eligible, native Greenhouse, saved switch on, no remaining form blockers | Existing restrictions retained |
| Prepare these | Decision checked; no form execution | Yes / supported boards read questions ahead | No submission | No submission |
| Apply to next 10 | Legacy blocking rules, locations/age, selection/caps | No / no Phase 3 preflight | Could automatically submit Greenhouse despite warnings or unknowns | Always manual Submit |
| Morning prepared / Start applying | Legacy rules, still-open/location/age checks | No / its older questionnaire, not Phase 3 enforcement | Same automatic Greenhouse behavior | Always manual Submit |
| Job-page Auto-apply | Legacy blocking rules; explicitly selected job, any board | No / no Phase 3 preflight | Direct or redirected Greenhouse could submit automatically | Always manual Submit |
| Assistant apply, one job or count | Confirmation starts corresponding legacy runner | Same gaps as corresponding legacy path | Could contradict its manual-Submit promise | Always manual Submit; promise now matches runner |

The gate is in `runner.run` for legacy runs and separately in `runner.apply_one` for direct legacy
callers. `_apply_anywhere` explicitly passes `auto=False` when following another board to Greenhouse.
The next-up preview reports the effective manual mode, and all affected submission notices were
updated. The saved applicant preference was not changed; no new automatic capability was enabled.

The generic follower additionally excludes form-associated controls, including implicit Submit
buttons and controls associated through the HTML `form` attribute. Unrecognized forms remain with
the person. A mocked minimal form reproduces the unsafe classification and verifies the guard.

**Incident evidence:** the real history records Backblaze job 13984 as `how=auto` at
`2026-10-26T18:46:50.610883+00:00` (1:46 PM Chicago), source Greenhouse, with no chosen-queue link.
The database agrees on Applied and the timestamp. The entry point “Apply to next 10” is supported by
Claude's handoff; the history record itself does not store an entry-point identifier. The reported
warning eligibility is not an independently reconstructed historical decision/profile snapshot.

**Authorization limit:** chosen runs still use the existing saved switch plus the explicit start
action. There is no durable per-execution authorization record; that proposed Phase 4 feature was
not implemented. No old server process was restarted, and no real run was started to demonstrate
activation. No apply worker was seen in the original process inspection; future CLI workers import
the patched runner. The user's later October 27 log shows an active run, so the earlier observation
does not describe the current live process. Existing server-side imports/notices may require an
ordinary restart to refresh. No active user run was interrupted or restarted by this work.

## 5. Confirmed defects and fixes

| Defect | Change | Evidence |
|---|---|---|
| Legacy automatic Submit bypasses Phase 3 checks | Manual-only gate at runner and legacy helper | Eight new safety cases, including all legacy argument paths and direct-helper bypass attempts |
| Cross-board Greenhouse redirect inherits automatic permission | Redirect explicitly disables automatic Submit | Direct redirect regression plus existing chosen-board tests |
| Generic follower mistakes a short form's Submit for navigation | Reject form-associated controls | Browser test using a fictional minimal form |
| Unanswered green-card field treated as “no” | Unknown unless both citizenship and green-card answers explicitly rule it out; rule version `.3` | Six explicit combinations; 43,740-combination compatibility test retains all other expectations |
| UI describes old/global permission | Scope notices to chosen jobs; legacy previews/manual wording | Desktop/mobile browser checks; final header parsed separately |
| New diagnostics could be overwritten or wait indefinitely | Unique directories, flushed event logs, traces/screenshots/HTML, bounded stability wait | Fifteen moving-page runs and a never-settles test |

**Unfixed acceptance defect:** `runner.record_application` commits `mark_applied` before `write_audit`.
`execution.answer` similarly commits the item and job before writing history and marking documents
sent. An `OSError` at that point leaves Applied with no history; retry returns false or is refused,
and recovery cannot repair it. The two new tests simulate this failure in temporary databases.
Both still fail. They are neither skipped nor marked expected-failure. No historical record was edited.

## 6. Browser interruption investigation

- Preserved and reviewed Claude's later summaries: 20 CPU-load runs after redraw fixes, 15 CPU-load
  runs after the stability wait, and 3 quiet runs, all reporting zero failures. These are prior evidence,
  not new load tests performed here.
- New WebKit and Chromium click-during-redraw tests pass at 1440 and 390 pixels. They check a row's
  Cancel during another row's redraw and Prepare during a card redraw; requests occur once.
- Fifteen new WebKit Jobs-page review runs alternate desktop/phone widths. Fictional delayed cards
  move the filter bar by 490 pixels; the bounded wait detects stability before review. Selection,
  review and closing the dialog start no application or writer.
- The production Jobs-page layout shift itself remains. Waiting in a test does not fix a real
  person's moving click target. The original three runs' individual causes remain unknown.
- Submission notices were checked on four pages in light/dark themes at desktop/phone sizes.
  Keyboard theme activation and dialog Escape were checked; this is not a complete screen-reader
  or keyboard audit. Selected screenshots were inspected visually.
- Axe: Applicant and Questions scans clean; Auto-apply on phones reports the existing non-focusable
  scrolling `#chosen-activity`; job pages report existing contrast issues in resume/letter checklist
  text. These are separately recorded UI issues, not passed accessibility criteria.
- New test-harness failures were retained: WebKit initial Tab focus was first a SUMMARY and then
  BODY. The check now uses a known focus position and verifies keyboard activation explicitly.
- A later temporary copy was missing modules and failed collection. The source repository remained
  intact. The deletion cause was not established; temporary-directory cleanup is suspected. The final
  complete run used a fresh source copy with fresh file timestamps.

## 7. Earlier test-change audit

Compared the recovered `orig_tests` sources to the takeover snapshots, inspected the saved session
edit records (including the three additional Phase 3 edits), and retained per-file diffs.

- Fictional missing document paths became actual temporary PDF-like bytes. Existing fingerprint,
  state, outcome, concurrency and recovery assertions were retained. Process fixtures use separate
  document directories. Claude's reported 13 affected failures were not independently recounted by
  rerunning that historical broken version; the actual fixture changes were audited.
- Six earlier Jobvite expectations changed to reflect assisted support: selection/review/order,
  transfer, run, list display and route scenarios. Blocked examples became closed or already-applied
  jobs. The transfer test still checks two distinct refusal reasons and duplicate-add behavior.
- Three Phase 3 scenarios changed similarly. The Eligible/warnings automatic-submission test uses
  two Greenhouse fixtures so it still tests the permission distinction rather than different adapters.
- The obsolete Jobvite refusal-text assertion was removed. Dedicated assisted tests assert that a
  redirected Greenhouse form is not submitted automatically, while an Eligible native Greenhouse
  form retains its existing behavior.
- The multiprocessing helper now writes once, atomically renamed, before sequential child starts.
  No concurrency or crash assertions were loosened. New full-suite process/crash cases pass.

No audited submission, idempotency, concurrency, recovery or authorization assertion was weakened
to conceal a defect. This does **not** mean the earlier tests covered all crash windows: the new
history failures demonstrate the missing coverage. Changes made during this recovery are listed
separately below, including the intentional legacy automatic-to-manual expectations.

## 8. Tests and remaining failures

| Verification | Result |
|---|---|
| First focused isolated run | 262 passed; one OS process-identity test failed because sandbox denied `ps`; passes outside that restriction |
| First complete isolated suite | 1,807 passed, 2 failed, 1 skipped: obsolete legacy-mode expectation and profile-dependent digest fixture, both corrected |
| Final complete isolated suite | **1,818 passed, 2 failed, 1 skipped** |
| Durable queue / hardening / live progress | 19 / 112 / 10 passing |
| Chosen selection | 26 passing |
| Eligibility / prepared documents / questions / assisted boards / normal linked history | 21 / 18 / 11 / 6 / 4 passing |
| New legacy submission safety | 8 passing |
| Redraw browser regression | 4 passing: both engines, both widths |
| New browser verification | 6 passing, including the 15-iteration stability scenario |
| History write failure, runner and chosen confirmation | **2 failing acceptance regressions** |

All test runs used temporary databases, fictional jobs/documents, mocked providers/employer pages
and isolated browser contexts. Test copies contained no production database, applicant/answer files,
personal search profile or `.env`. Existing network/AI-command guards remained enabled. Browser
requests were fulfilled by mock routes or restricted to each temporary loopback server.

After the final suite, one Questions-page header sentence was narrowed to “Your chosen jobs”; its
template parsing and exact text were checked. No runtime logic changed after that suite.

## 9. Real-data integrity verification

- Read-only SQLite integrity check: `ok`. No production engine initialization/migration was used.
- 18 JSONL entries, 18 distinct jobs, zero missing jobs, zero database/history timestamp mismatches.
- All 29 referenced resume/cover-letter files exist. These are legacy records: zero stored document
  fingerprints, so their historical contents cannot be checksum-verified from those records.
- Application log, applicant settings and answer files matched their takeover hashes at the October 26
  protected-file check. These counts and hashes are a historical snapshot, not a claim that the user's
  live activity on October 27 left them unchanged. No application status or historical submission was
  edited by this work.
- Production databases, WAL/SHM files, discovery progress/logs, board CSV and some tooling state
  changed concurrently. Whole-database byte identity therefore cannot be claimed. Tests ran in
  separate source trees; these concurrent changes were preserved, not normalized or restored.
- Chosen-workflow consistency, attempts, fingerprints, timestamps and retry deduplication were
  verified with fictional linked records. Normal operations pass; the write-failure gap above does not.

## 10. Exact files modified by this recovery

These are changes relative to takeover, not the full pre-existing Git diff:

- `backend/apply/runner.py` — legacy/redirect gates and effective preview.
- `backend/apply/anyform.py` — exclude form controls from navigation.
- `backend/core/eligibility.py` — unknown green-card handling and rule version.
- `frontend/templates/apply.html`, `questions.html`, `applicant.html`, `job.html` — accurate submission notices.
- `qa/tests/test_apply_runner.py`, `test_auto_apply.py` — manual legacy behavior assertions.
- `qa/tests/test_eligibility_decisions.py` — explicit missing-answer cases; precise compatibility exception.
- `qa/tests/test_anyform.py` — short-form navigation safety regression.
- `qa/tests/test_daily.py` — isolated profile path and deterministic high/low digest scores.
- `qa/tests/test_apply_page_presses.py` — mobile coverage, preserved diagnostics, correct uncertainty wording.
- New `qa/browser_checks.py` — bounded stability wait and unique diagnostic capture.
- New `qa/tests/test_legacy_submission_safety.py` — legacy entry-point and redirect safety.
- New `qa/tests/test_phase3_browser_verification.py` — isolated desktop/mobile checks.
- New `qa/tests/test_history_recovery_gap.py` — two unresolved acceptance regressions.
- `README.md`, `docs/auto-apply-v3.md`, new `docs/phase3-recovery-verification.md` — current behavior and evidence.
- New ignored evidence directory under `logs/phase3-recovery-2026-10-27-k7a2izw8/`.

No model/schema/migration, real applicant/profile/answers, or existing application data file was
edited by the recovery work. Concurrent board/tooling changes are not attributed to this patch.

## 11. Unresolved risks and approval boundary

1. **Blocking: committed applications can lose their linked history.** Proposed next change for
   review: an additive durable pending-history table keyed by job/application, written in the same
   database transaction as the application state. An idempotent exporter would append the stored
   payload once, recover after interruption, and preserve the original timestamp and document link.
   Validate crashes before commit, after commit, during append, and after append before acknowledgement,
   including concurrent exporters. Do not backfill or alter historical records without separate approval.
   **Not implemented or enabled:** this crosses the requested durable-storage/migration approval boundary.
2. Legacy paths remain manual-only and still lack Phase 3 fingerprints and questions-first enforcement.
   Do not remove the gate simply because regression tests pass.
3. Required questions are read ahead only on supported boards. Failed question fetches can return no
   questions; live form validation is then the remaining check. No real form behavior was verified.
4. The Jobs-page layout shift and the recorded accessibility findings remain.
5. The existing essay-draft writer uses the older prepared-file save path without the new encompassing
   answer-update lock. Concurrent draft/answer updates remain a risk outside this patch.
6. Saved-switch authorization is not a durable per-run authorization record. Phase 4 remains deferred.
7. Normal retry/confirmation deduplication passes; that is not equivalent to atomic durable history.

## 12. Acceptance recommendation

**FAIL Phase 3 acceptance for now.** Retain the manual-only legacy safety gate. The unresolved
history-write failure contradicts the promised linked-history and crash-recovery guarantees, despite
all existing Phase 1–3 tests passing. Approval is needed for the proposed durable-history change
before enabling a schema/storage change in the real application. No new automatic submission
capability is proposed.

## Addendum: October 27 sign-in blockage and short questions

The user supplied a later live Jobicy log showing an account page classified as an application, zero
filled fields/files, and a wait for `dynamic_email`/CAPTCHA. They also supplied Google's insecure-browser
sign-in rejection and asked who answers LinkedIn and short motivation questions. This evidence is
user-provided; no real site was opened to reproduce it.

Confirmed source defects and narrowly scoped fixes:

- The generic follower/watcher had no recognition of those explicit account/sign-in barriers. It now
  returns through the existing normal-browser handoff, both during initial navigation and while
  waiting. It retains the original posting rather than a login callback URL and does not record Applied.
  Normal navigation text saying “Sign in” alone does not trigger this behavior. Other unrecognized
  account wording may still require a manual handoff; this is not a universal authentication detector.
- The essay classifier required over 40 characters, excluding “What Excites You About This Role?”;
  the Questions UI also omitted its wording. Both now recognize the short motivation prompt.
- Identically worded essay questions could merge across jobs, and short motivation answers could enter
  the global exact-answer bank. Essay prompts now remain separate by job; current-job answers are
  allowed, while general-bank essay answers are neither learned nor reused. Existing bank records
  are not deleted or migrated. LinkedIn still reads the user's saved profile and remains blank if absent.

Additional files changed relative to the initial recovery list: `backend/apply/essays.py`,
`backend/apply/prepare.py`, `backend/apply/answers.py`, `backend/apply/profile.py`,
`qa/tests/test_apply_answers.py`, `qa/tests/test_other_boards.py`, `qa/tests/test_jazzhr_apply.py`, and
[`docs/apply-learning-proposal.md`](apply-learning-proposal.md). The already-listed runner, anyform,
Questions template and three test files also gained the corresponding fixes/regressions.
Those three test files are `test_auto_apply.py`, `test_anyform.py` and
`test_phase3_browser_verification.py`. The JazzHR test now places its job-specific answer in
`job_answers`, matching `runner.answers_for`; its original fill assertion remains, with new assertions
that a global-bank essay is ignored and clearing the job scope removes the answer again.

Existing written-answer retrieval was inspected, not replaced. A proposal covers capturing questions
from unfamiliar forms, approved answer provenance, and separate non-personal navigation memory.
This extension is not enabled, and no self-training/RAG engine was introduced. The history-outbox
migration remains a separate approval decision.

The first follow-up focused browser run was blocked by sandbox process/loopback restrictions
(103 passed, 12 skipped, 8 setup errors). After allowing the isolated browser process/local server,
all 123 focused checks passed. The next complete suite passed 1,825 tests with only the two known
history-recovery failures and the opt-in Word skip. A final suite includes the additional job-scoping
and exact-answer reuse regressions; its evidence is retained separately from the original archive.

That intermediate final run had 1,827 passes, the two history failures, and the JazzHR fixture failure
described above. After correcting the fixture, the complete suite finished with **1,828 passed,
2 failed, 1 skipped in 184 seconds**. The new-tab sign-in test was then added and passed independently
(1 passed); no runtime code changed after the complete run. The mobile draft screenshot was visually
inspected. Evidence, including failed runs, is in
[`signin-and-questions`](../logs/phase3-recovery-2026-10-27-k7a2izw8/signin-and-questions/):
[complete log](../logs/phase3-recovery-2026-10-27-k7a2izw8/signin-and-questions/accepted-suite.log),
[JUnit](../logs/phase3-recovery-2026-10-27-k7a2izw8/signin-and-questions/accepted-suite.xml),
[popup regression](../logs/phase3-recovery-2026-10-27-k7a2izw8/signin-and-questions/popup.log), and
[browser diagnostics](../logs/phase3-recovery-2026-10-27-k7a2izw8/signin-and-questions/browser-diagnostics.zip).
The log filename `accepted-suite` labels that verification attempt, not Phase 3 acceptance;
the recommendation remains **FAIL**. No production process was restarted to activate these changes.
