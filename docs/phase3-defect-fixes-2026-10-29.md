# Phase 3 — the five confirmed gaps, fixed (29 Oct 2026)

Live since 30 Oct 2026, 04:06: the web app was restarted once, as approved (new process 54051; mailbox watcher
and morning catch-up still held). `data/apply/auto_submit.hold` still stops every run from pressing Submit by
itself until its removal is approved (the person's own *Submit by itself* setting is unchanged). Nothing was
committed.

| Gap | Root cause | Fix |
|---|---|---|
| A. Greenhouse fail-open | A failed read of the questions, the CAPTCHA, a field or its "required" mark counted as "nothing there" (`greenhouse.fill`) | `FillResult.unchecked`: every failed check counts as needing the person; a field whose marking cannot be read counts as required; the run says what could not be checked |
| B. Questions not read = none asked | `prepare.open_questions` and `jazzhr.fetch_questions` returned `[]` on errors | `QuestionsUnread`; the failure is kept beside the job's answers (`questions_unread`); a run reads again before the form and never sends an unread one by itself; a later read clears it |
| C. Saves at the same moment | The answer bank was rewritten in place with no lock; essay drafts saved outside the prepared file's lock; written answers locked within one process only | `backend/apply/filesafe.py`: a cross-process lock and whole-file writes (private temp file, renamed) for the bank, written answers, general answers; drafts saved under the prepared file's lock |
| D. Applied with no history | The job page's status, the assistant and Applied-page stages set Applied directly | `runner.record_marked`: a history line saying "marked" (where), in the same transaction, through the outbox; `applied_at` untouched (it means when an application went out); none when a line exists |
| E. Documents not vouched for | Only the checksum was checked before an automatic send | `execution.documents_unready`: the resume must be the very file a checked tailoring run made (all checks passed, no drafting problem, not to be made again); a cover letter (no kept checks) leaves Submit to the person |

**Tests added (27):** `test_apply_greenhouse.py` +6, `test_questions_first.py` +6, `test_saves_at_once.py` 3
(multi-process; both bank and written tests fail 3 of 3 on the old code), `test_marked_applied.py` 4,
`test_prepared_documents.py` +8. Tests that expect an automatic send now give it a made-up board that answers
and a made-up checked tailoring run; conftest keeps tests off the live hold and the real tailoring records.

**Results:** full suite 1,895 passed, 0 failed, 1 skipped (opt-in Word); no test opened the person's database
(tripwire); pages render; the real-process crash demonstration passes; application, queue, history and
settings records identical to the post-restart snapshot.

**Remaining risks.** No per-document approval or record of which model wrote a resume exists (Phase 4's
authorization records); a form with a cover letter waits for the person. Until the restart, the web app's own
saves (answer bank, Did-it-go) use the old unlocked code beside new-code runs. Other boards' questions are
still seen only on the form. Setting a job back from Applied keeps its history line.

**Recommendation:** CONDITIONAL — ready for controlled activation; accept after it.

**Activation (approved; done 30 Oct 2026, 04:06, every check passed):** render pages; confirm no run or prepare is going; restart only the web app
(`launchctl bootout` / `bootstrap` of the existing definition — the watcher hold stays); check the loaded
files' fingerprints, `/apply/next` (`auto_submit: false`), startup recovery and the records snapshot. Then,
separately approved: delete `data/apply/auto_submit.hold` to let Eligible, ready Greenhouse jobs go by
themselves again; release the watcher hold.
