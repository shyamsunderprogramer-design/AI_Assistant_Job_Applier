# Auto Apply v3.0 — where it stands

> **Live activation, 29 Oct 2026: technical PASS.** The web app was restarted at 08:47 with its
> mailbox watcher and morning catch-up held; it runs the tested code (364 of 364 files match), the
> older paths wait for Submit, and no application, queue, history or settings record changed. The
> hold stays on until you release it. See [the activation report](phase3-activation-2026-10-29.md).

> **Isolated startup-hold update, 29 Oct 2026 UTC:** both web startup watchers now
> support an explicit temporary hold; 319 relevant isolated tests pass. No live
> restart or hold activation occurred. See [verification and remaining activation work](phase3-isolated-startup-hold.md).

> **History repair update:** the approved transactional-outbox repair passes all enabled regressions.
> See [its verification report](phase3-history-durability-repair.md). Overall acceptance is conditional
> on controlled live activation; no Phase 4 work or additional automatic submission was enabled.

> **Recovery verification, 26 Oct 2026:** the earlier verification below is historical evidence,
> not final acceptance. See [the recovery report](phase3-recovery-verification.md) for current
> results and unresolved failures. Legacy batch, prepared, job-page and assistant runs now always
> wait for the person to press Submit, regardless of the saved switch. A cross-board redirect
> never enables automatic submission. Only the existing chosen-jobs workflow retains its
> restricted automatic path; no new submission capability was enabled. No real settings or
> application records were changed.

*Written 26 Oct 2026, updated the same day. Phases 1 and 2 are built, verified and accepted; Phase 3 is
built and verified, its final verification done the same afternoon (below), awaiting your acceptance;
nothing below is committed yet.*

Auto Apply v3.0 turns "apply to the jobs I chose" into one durable, observable workflow: you tick
jobs on the Jobs page, review whether each can go, add them to the **Your chosen jobs** list on the
Auto-apply page, press **Apply to these**, and watch every step happen. "Phase" below means the
v3.0 plan's phases, not the original build plan's P1–P10 in the README.

| Part | State | Verified by |
|---|---|---|
| Exact job selection (step 1) | Built, verified | `qa/tests/test_chosen_jobs.py` (26 tests); WebKit check, 40 points |
| **Phase 1** — durable execution | Built, hardened, verified | `test_execution_queue.py` (19), `test_execution_hardening.py` (112); old flaws put back one at a time: 10 of 10 caught |
| **Phase 2** — real-time monitoring | Built, verified, **accepted 26 Oct** | `test_live_progress.py` (10); WebKit check, 18 points (restart, blocked stream, phone) |
| The company's own page for a job | Built, verified | `test_employer_page.py` (49); WebKit check, 10 points; 6 page variants rendered |
| **Phase 3** — workflow integration | Built, verified 26 Oct, **awaiting acceptance** | `test_eligibility_decisions.py` (15), `test_prepared_documents.py` (18), `test_questions_first.py` (11), `test_other_boards.py` (6), `test_history_linkage.py` (4), `test_apply_page_presses.py` (2, in WebKit and Chromium); submission-safety rules broken one at a time: 12 of 12 caught; WebKit checks: 20 + 12 + 10 + 7 + 7 points |
| Phases 4–7 | Not started | — |

Full suite on 26 Oct, after Phase 3's final verification: **1,795 passed, 0 failed, 1 skipped** (the
opt-in Word test). Every test runs on made-up jobs in throwaway databases; no test may open
`data/jobs.db` through the app or directly, or change the person's own records (three guards in
`qa/tests/conftest.py`), reach the network, or start an AI command.

## Phase 1 — durable execution (verified)

Code: `backend/apply/execution.py`, tables in `data_engineering/db/models.py`, routes in
`backend/api/chosen_pages.py`.

- **Kept in the database**, so a crash or a restart of the app or the Mac never loses where a job
  stands: `apply_items` (the list), `apply_runs` (each press of Apply to these), `apply_events`
  (every step, append-only).
- **Eleven states** — Queued, Validating, Preparing, Ready, Running, Waiting for you, Pending
  confirmation, Applied, Failed, Skipped, Cancelled — and only the moves the `MOVES` table allows.
- **Never twice:** one live entry per job and one live run, each held by a unique index; every move
  is a conditional update naming the state it expects, so of two processes acting at once exactly
  one succeeds (tested with six real processes on one database file).
- **Checked again** just before each job goes and again before its form opens; a job applied to
  another way meanwhile becomes Applied, never opened.
- **Cancel** works until a form opens and is refused, with the reason, once it is open; **Pause**
  stops before the next form; **Retry** puts back a failed, skipped or cancelled job, never one that
  may have gone.
- **Recovery** (at app start, on page load, and live while a run is watched): a form not yet open goes
  back in the queue; a form that was open becomes *Pending confirmation* and is never retried by
  itself. A run started a moment ago is left alone; a job cancelled meanwhile stays cancelled.
- **Recorded once:** an application is marked and logged in one step, whoever says it went (the run,
  "Yes, it went" on either page, an employer's email); the first answer stands and a later one is
  refused with "Nothing was changed".
- **Upgrades** are additive (`data_engineering/db/migrate.py`): older databases gain the tables,
  columns and guards at the next start without losing a row.

## Phase 2 — real-time monitoring (verified, accepted)

- **Live stream** (`GET /apply/chosen/stream`, server-sent events): every move, every line a run
  writes (tailoring rounds, opening the form, fields filled, what it waits for), and the run's own
  news, as it is written.
- **On the page:** each job's current step and for how long, what it needs from you, how it ended;
  **Steps** opens its whole history to the second (`GET /apply/chosen/events?item=`); the card says
  *Live*, *Reconnecting…* or *Checking every few seconds*.
- **Reconnects** — refresh, dropped connection, app restart, laptop reopened — resume from the last
  step the page had (the browser's Last-Event-ID): nothing missed, nothing twice. Every job carries
  the id of its latest step, so an older picture never replaces a newer one.
- **Fallback:** without the stream the page looks every 2 seconds while anything moves (20 when idle)
  and tries the stream again a minute later.
- **Applied** is shown only from the queue's own state — a confirmation page the app saw, or your
  "Yes, it went". A form's own "Thank you" text is a step, not a confirmation.

## Phase 3 — workflow integration (verified, awaiting acceptance)

Built in five steps, each checked before the next; nothing here sends anything by itself that did
not before. Code: `backend/core/eligibility.py`, `backend/apply/runner.py`, `execution.py`,
`chosen.py`, `prepare.py`, `tracker.py`, `backend/api/chosen_pages.py`, `app.py`; pages `index.html`,
`apply.html`, `application.html`.

1. **Eligibility decisions.** Every chosen job has one of five decisions -- *Eligible*, *Eligible,
   with warnings*, *Needs your answer*, *Not eligible*, *Unverified* -- with its reasons, the
   posting's own words for each requirement, what is unknown, what rules it out, when and by which
   rules (`RULE_VERSION`). Read from the rules the app already had (`eligible()` gives exactly the
   answers it gave before: 43,740 combinations compared). An unanswered profile question is never
   taken for a yes or a no; a company's past H-1B sponsorship is a warning, never proof. The run
   sets a *Needs your answer* job aside and goes on; **Submit by itself sends only an Eligible job**
   (warnings or Unverified: you press Submit). Kept on the entry (`apply_items.decision`), each
   change written down as a step.
2. **Prepare these.** A button on the chosen card makes each job's resume (an approved tailored one
   reused, never made again) and cover letter ready, with no form opened and nothing sent, and
   writes down each file: where it is, its checksum, the file it was made from, the run and attempt
   (`apply_artifacts`). The run copies exactly those files into the attempt's own folder, checks
   them against their checksums just before the form opens, and never sends one changed or gone
   since (re-tailored, edited, flagged to be made again): it fails with the reason, and Retry makes
   it again. A writer that bills per use is used only after you say yes (both buttons ask); the
   fallbacks are only ever free ones.
3. **Questions first.** When documents are made ready, the form's questions are read from the
   board (Greenhouse, JazzHR) and kept with the job's answers; a job whose form has required
   questions nothing answers waits -- *waits for your answers · Answer them* -- while the rest go.
   The Questions page asks them beside your prepared applications; saved, the job goes on the next
   run with your answers filled in. Sensitive questions are never guessed. The answers file is now
   written whole and one change at a time, across processes.
4. **Any board, assisted.** A chosen job on a board with no filler of its own (Jobvite, Lever, ...)
   goes on a run: the app follows the posting to the employer's form and fills what it can; you
   finish it and press Submit -- never the app, even when the posting leads to a Greenhouse form.
   Lever forms are handed to your own browser. Apply to next 10 keeps its old rule.
5. **One record, linked.** An application sent from the list -- by the run, or by your "Yes, it
   went" on either page -- carries its entry, run, attempt and each document by name and checksum
   in its line in `data/applications.jsonl`; its files are kept as *sent*; the Applied page shows
   "From your chosen jobs ... run N, attempt M" with the checksums, and the list's applied row
   links to it. One record however many say it went; its first day stands.

**Acceptance results (26 Oct).** Full suite 1,792 passed, 0 failed, 1 skipped. WebKit on look-only
copies with made-up jobs, desktop and phone: Phase 3's five checks (17, 12, 10, 7, 7 points) and the
earlier ones again (selection 40, live view 16, live stream 18, company page 10) -- all passing; two
expectations in the earlier checks were updated on purpose (a Jobvite job now goes, assisted; each
job has one more step, its eligibility). The real-process demonstration (a worker killed mid-form,
recovery, duplicates refused, an uncertain one never retried) passes with Phase 3. Your files are
identical to the backup taken before Phase 3 (`before-phase3/2026-10-26`); the real database's queue
tables are empty and its 14 applications unchanged: nothing was sent.

### Final verification (26 Oct, afternoon)

**The three early stops.** Later checks reproduced the following candidate causes on look-only copies with made-up jobs
(every run kept, with its page events, requests and screenshots):

- *Prepare check* -- a fault in the check: it read the list as soon as three jobs had documents, while
  the last was still moving to Queued. Fixed in the check: 7 failures in 8 runs before, none after.
- *Auto-apply page* -- a fault in the page: it wrote its buttons' words and its whole list again at
  every step a run made. Safari's engine (WebKit) drops a click when the pressed button's words are
  written again between press and release (0 of 20 counted; Chromium 20 of 20), and both engines drop
  it when the button itself is drawn again. Fixed: each row is drawn again only when it changed, a
  button's words only when they differ. `test_apply_page_presses.py` presses during a redraw in both
  engines; it fails on both broken versions of the page and passes on the fix. After the fix the
  Prepare and Questions checks ran 18 times each with no stop, 10 of them with the processor busy.
- *Jobs page* -- the board-search and careers-finder cards above the filter bar fill in just after the
  page appears and push the selection bar down by about 490 px within half a second; a press during
  that lands on whatever moved under it (the assisted check's "Review" landed on the search box, 2 of
  15 runs with the processor busy). **Not fixed**: the cards are Job Discovery's, outside Phase 3. The
  checks now wait for the page to stop moving (15 of 15 then) and print how far it moved.
- Which cause stopped each of the three original runs is not known: their records were overwritten.

**Earlier tests changed in Phase 3** (rebuilt from the session's own record and run against today's
code: 18 of them fail, for exactly two reasons):

- 13 failed because their made-up documents named files that did not exist; Step 2 fingerprints
  the real file, so the stand-ins now write a small made-up PDF (`test_execution_queue.py`,
  `test_execution_hardening.py`, `test_live_progress.py`, `test_chosen_jobs.py`). With only that
  change every one of their original assertions holds.
- 6 expected the Jobvite job to be refused; under Step 4 it goes, assisted, and you press Submit:
  the run test (it is opened and applied), the list page (its blocked example is now a closed
  posting), the review, adding (now with two can't-go reasons again, as before), the chosen order,
  and the route test (its blocked example is an applied job). Three of Phase 3's own tests changed the
  same way. Only one assertion was removed without a replacement -- the Jobvite job's "a run fills
  Greenhouse, Workday and JazzHR forms" refusal, which no longer happens; a skipped job's reason is
  still checked in three other tests.
- The multi-process tests write their helper program once (a draft, then renamed): rewritten for every
  process, it was emptied while the one before was still starting (50 of 60 stopped silently with the
  rewrite slowed down). No assertion changed.
- No assertion about Submit, duplicates, concurrency or recovery was removed or loosened.

**Which paths use Phase 3's rules.**

| Path | Eligibility | Documents fingerprinted | Questions first | Who presses Submit |
|---|---|---|---|---|
| *Apply to these* (chosen jobs) | Five decisions, checked again before each form | Yes | Yes (Greenhouse, JazzHR) | The app only for an Eligible job on Greenhouse's own board, with Submit by itself on; else you |
| *Prepare these* | Checked; nothing opened | Yes | Yes | -- (sends nothing) |
| *Apply to next 10* | The older rule: blocks only (no warnings, no "needs your answer") | No | No: questions are seen on the form | The app for any Greenhouse form with nothing left blank, CAPTCHA or legal box, with Submit by itself on; Workday, JazzHR: you |
| Morning's prepared applications (*Start applying*) | The older rule | No | Its own questionnaire | As *Apply to next 10* |
| A job's own page, *Auto-apply* | The older rule; any board | No | No | As *Apply to next 10* -- including a Greenhouse form reached from another board's posting |
| The assistant's apply (after you confirm) | The older rule: for one job as a job page's, for a count as *Apply to next 10* | No | No | As those, though its message says you press Submit |

None of these was changed. A real example on 26 Oct, 1:46 PM: *Apply to next 10*, pressed in the app,
sent Backblaze's Greenhouse form by itself; under Phase 3's rules that job is *Eligible, with
warnings* (it doesn't say whether it sponsors a visa, and you need one), so *Apply to these* would have
left Submit to you.

**Eligible's words.** "Nothing in the posting rules you out" read as if skills and years had been
weighed. It now says: no clearance, citizenship or visa requirement in the posting stands against your
profile, and its skills, experience and qualifications are not part of this check (`RULE_VERSION`
2026-10-26.2). Both pages say what a decision covers, beside the list and on each badge.

**Submission safety, broken on purpose** (in a copy of the code, never the project's files), each
caught by the tests: another board's form sent by the app; a job with warnings or unverified sent by
the app; a job needing an eligibility answer going anyway; unanswered required form questions going
anyway; a Greenhouse form with a blank required field, a CAPTCHA or a legal box sent by the app; a
changed prepared document sent; the document it was made from changed and sent anyway; a second
record for one application; recovery retrying a form that may have gone; no check just before the
form opens; *Apply to next 10* taking any board. Two of these were first caught by nothing -- each rule
is kept in two places, so breaking one changed no outcome; a test for each place was added.

**Test isolation.** The salary rule reads a job's pay straight from `data/jobs.db`, so seven of Phase
3's question tests read the person's database by made-up job ids (read-only; all pass against an
empty stand-in, so no result depended on it). A third guard now refuses any direct opening of the
person's databases, and the salary rule reads the test's own.

**Your data after all of it.** Settings, answers and records are identical to the backup taken before
Phase 3, except the two changes the 1:46 PM *Apply to next 10* run made: its Backblaze line in
`data/applications.jsonl`, and four answers kept from the JazzHR form filled during it (gender, race,
two experience figures) in `backend/config/answers.yaml`. The database passes its check; its queue
tables are empty; 15 applications. No test or check sent anything or used a writer that bills.

## Limitations (as built)

- **Submit by itself** now applies only to *Apply to these*, for a job decided
  *Eligible* on Greenhouse's own board; every legacy run and every other form waits for you.
- The decision covers who may take a job (clearance, citizenship, sponsorship), the posting's state
  and your own rules (place, age). **Qualifications** -- skills, years, certifications, education --
  are not part of it; the match score is shown as before.
- A form's questions are read ahead for **Greenhouse and JazzHR** only; another board's are seen on
  the form, as before.
- **Apply to next 10**, the morning's prepared applications, a job page's Auto-apply and the
  assistant's apply keep their old rules: no decisions, no recorded checksums, no questions first
  (the table above). A job page's Auto-apply takes any board.
- *Apply to these* asks before using a writer that bills per use even when every job is prepared.
- A form waiting for you holds the run for up to 15 minutes, then becomes *Pending confirmation*.
- One Mac, one person, one live run at a time (*Prepare these* counts as one); each open Auto-apply
  tab holds one stream connection.
- Whether a saved company link is the job's own posting or a careers site is read from the link; an
  unusual link can be misread (one click switches it).
- Not yet built: submission modes and authorization records (Phase 4), the command center with batch
  summary and insights (Phase 5), friction metrics and feedback (Phase 6).

## Remaining risks

- **No real employer form yet** through the new chosen-jobs run: every test, by rule, used made-up
  jobs and fake browsers -- including the assisted path to other boards, which puts the any-site
  follower inside the run for the first time. The first real runs should be watched.
- **Historical safety defect, now gated off:** *Apply to next 10*, a job page's Auto-apply and the
  assistant's apply send a Greenhouse form by themselves for a job Phase 3 would leave to you
  (warnings, unverified, needs your answer) -- 26 Oct, 1:46 PM, Backblaze. A job page's Auto-apply
  also sent a Greenhouse form reached from another board's posting. The recovery gate makes these
  paths manual-only; removing that gate requires separate approval and verification.
- **The assistant's apply says you press Submit.** The recovery gate now makes its engine behavior
  match that promise, even with the saved switch on.
- **The Jobs page moves after it appears** (its board-search and careers cards fill in): a press in
  the first half second can land on what moved under it, *Clear selection* among them. Not fixed
  (Job Discovery's cards).
- The look-only copies used by the browser checks read the real board-search and careers progress,
  the real apply log and the account status from `data/` (read only: shown, never changed).
- The essay drafts writer (`backend/apply/essays.py`) still saves the answers file without the new
  lock; with whole-file writes a reader never sees half a file, but a draft saved at the same moment
  as an answer could still undo one of the two.
- The stream renews itself every 5 minutes (tested); a run of several hours has not been observed.
- One multi-process test failed once during a slow full run; a race in the test's own harness was
  found, proven and fixed, but that run's message was not captured, so the link is likely, not proven.
- The memory plugin's hook ("python: command not found") keeps failing in the Claude Code session
  that loaded it before the fix; its file on disk is fixed and runs cleanly. A new session ends it.
- 18 type-checker warnings remain, all type-only (13 on lines unchanged since the last commit); one
  unused branch is left in `runner.candidates(chosen_ids=...)`.
- **The work is uncommitted** (below): until it is committed, it exists only in this working tree.
  The data backup of 24 Oct (`/Volumes/Storage/AI Job Applier backups/2026-10-24`) covers the
  database and app files, not the code.

## Working tree, 26 Oct 2026 (not committed, not pushed)

- Branch `reorganise-by-role`, HEAD `392bf94` (20 Oct). Three local commits are not on
  `origin/reorganise-by-role`: `ea7efa2`, `d6fd460`, `392bf94`.
- **62 tracked files changed** (20,971 lines added, 1,975 removed) and **30 untracked files**, after
  Phase 3. Phase 3's new tests: `test_eligibility_decisions.py`, `test_prepared_documents.py`,
  `test_questions_first.py`, `test_other_boards.py`, `test_history_linkage.py`,
  `test_apply_page_presses.py` (final verification); changed besides the
  files below: `backend/core/eligibility.py`, `backend/apply/prepare.py`, `tracker.py`, `main.py`,
  `frontend/templates/application.html`.
- Auto Apply v3.0 and the company page — new: `backend/apply/execution.py`, `backend/apply/chosen.py`,
  `backend/api/chosen_pages.py`, `backend/core/joblinks.py`; tests `test_execution_queue.py`,
  `test_execution_hardening.py`, `test_live_progress.py`, `test_chosen_jobs.py`,
  `test_employer_page.py`, `test_isolation.py`. Changed: `backend/apply/runner.py`, `queue.py`,
  `anyform.py`, `backend/api/app.py`, `auto_apply.py`, `__main__.py`,
  `data_engineering/db/models.py`, `migrate.py`, `frontend/templates/apply.html`, `index.html`,
  `job.html`, `qa/tests/conftest.py` and the tests made to use their own database
  (`test_webapp_filters.py`, `test_tailoring_app.py`, `test_setup_page.py`).
- Earlier work of 20–24 Oct, also uncommitted: the location rules, Apply to next 10 and the watch
  list's place in it, job-alert import, Health page, morning pipeline, Mac installer, resume and
  tailoring changes, `ml/resume/titles.py`, the README rewrite.
- **Not to be committed:** `backend/config/boards.csv` (kept out by choice). Not this project's work:
  `.claude-flow/`, `.claude/proven-config*`, `agentdb.rvf*`, `ruvector.db` (tooling), and
  `DATABASE_TECHNICAL_AUDIT.md`, `RESUME_TAILORING_*.md`, `KT.md` (written by other sessions).

## Phase 3 — the proposal as approved (kept for the record)

The plan's Phase 3: connect explicit job selection, existing eligibility, existing resume
preparation, existing question handling, existing browser adapters and existing application
history — without replacing working subsystems. Selection is connected already; the other five are
connected only partly:

| Existing part | Connected now | Gap Phase 3 closes |
|---|---|---|
| Eligibility (`runner.blocked_reason`, `backend/core/eligibility.py`) | can go / can't, with a reason | the plan's five decisions, with unknowns and evidence |
| Resume and letter (`prepare.py`, `runner.prepare_documents`) | made during the run | made before it, on request; the exact file recorded |
| Questions (answer bank, Questions page, `prepare.open_questions`) | found when the form opens | found before the run; the job set aside until answered |
| Browser adapters (Greenhouse, Workday, JazzHR, any-site follower) | three boards in a run | any board, the others *assisted* (you submit) |
| Application history (`record_application`, Applied page) | by job | each entry linked to its record, files and run |

**Goals and what you would see**

1. **Eligibility decisions.** Each chosen job shows one of *Eligible*, *Eligible with warnings*,
   *Needs information*, *Ineligible* or *Unverified*, with the reasons, the posting's words behind
   them and what is unknown (a posting silent on sponsorship, for someone who needs it, is a
   warning, never a pass or a fail). Built on the existing rules — no second eligibility system;
   confirmed legal requirements cannot be overridden.
2. **Prepare before you start.** **Prepare these** on the chosen card makes each job's resume (the
   approved tailored one reused, a new tailoring only when needed, on your configured provider —
   never a silent switch to a paid one) and, where the employer asks for one, the cover letter,
   using the existing pipeline. Each job then shows *Documents ready* with the file that will be
   sent; the run uploads exactly that file (path and checksum recorded).
3. **Questions first.** While preparing, the forms whose questions can be read ahead (Greenhouse,
   JazzHR) are read; a question no saved answer covers puts the job under **Needs your answers**,
   with the exact question, and links to the Questions page where it is answered once for every
   form that asks. Sensitive questions (sponsorship, citizenship, clearance, EEO, consent) are never
   guessed. The run skips such a job and goes on with the others; once answered, it goes.
4. **Any board, honestly labelled.** A chosen job on a board without its own filler goes through the
   existing any-site follower — or the company's own posting you saved — in **assisted** form: the
   app fills what it can and waits for you to submit; it never submits these. Portals that refuse
   automation are handed to your own browser, with every answer listed, as the job page does now.
5. **One record.** An applied job's entry links to its application on the Applied page, the
   resume and letter sent, the form's picture and the run and attempt it went in; the Applied page
   shows the run in turn. Pending confirmation is never recorded as applied.

**Safety requirements**

- Submission authority does not change: *Submit by itself* still sends Greenhouse forms only; the
  any-site path never submits; no CAPTCHA, sign-in or verification is bypassed.
- Phase 1 and 2 guarantees stay: the states and moves, the conditional claims, recovery, recording
  once, the live stream. New states, if any, are additive and go through the same `MOVES` table.
- Schema changes are additive, made by `ensure_schema`, after a backup; no record is deleted or
  overwritten.
- No fabricated document content (the existing guard decides); no sensitive answer inferred.
- Tests use made-up jobs, a fake browser and mocked providers only: no real application, no paid
  AI call.

**Acceptance criteria**

- Every chosen job shows one of the five decisions with reasons, evidence and unknowns; an unknown
  is never shown as a pass; a mandatory requirement cannot be overridden.
- **Prepare these** readies documents without starting a run or opening a form; the file the run
  uploads is the one recorded (checked through the fake browser by checksum).
- A job with an unanswered required question known ahead is set aside with the exact question; the
  run goes on with the other jobs; after the answer, the next run sends it.
- A chosen job on another board is filled by the any-site follower and left for you to submit; the
  app never presses Submit on it; its outcome is recorded as for any other job.
- An applied entry links to its record and evidence; a pending one is not recorded as applied.
- The full suite passes, with every Phase 1 and Phase 2 test unchanged.

**Test plan**

- *Unit:* the decision mapping for each rule and each unknown; choosing and checksumming the
  document; classifying questions, sensitive ones above all.
- *Integration (fake browser):* a run that sets one job aside for answers and applies the rest, then
  sends it after the answer; the any-site path never pressing Submit; the uploaded file matching
  the recorded one; the record linking.
- *Failure injection:* a provider error while preparing (failed with the reason, nothing sent); a
  missing document (set aside); a crash while preparing (recovered to the queue).
- *Browser (WebKit, look-only copy):* the decisions, *Documents ready*, *Needs your answers* and the
  links, on a laptop and a phone.
- *Regression:* the full suite.

**Order and size.** Five steps, each a checkpoint you can stop at: (1) eligibility decisions,
(2) prepare before the run, (3) questions first, (4) any board assisted, (5) one record. About two
working days in all; step 4 carries the most risk, because it puts the any-site follower inside the
run.

**Depends on / leaves for later.** Uses Phase 1's queue and Phase 2's stream as they are. Leaves
submission modes and authorization records to Phase 4 and the command center layout to Phase 5:
Phase 3 adds to the existing chosen-jobs card only.
