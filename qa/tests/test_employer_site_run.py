"""Step 4 of the Morning shortlist plan (7 Nov 2026), on a run: a site's browser check that does not clear
hands the job to the person's own browser -- it waits for their word, is never Failed and never Applied,
and keeps its history; a closed posting fails with its reason and Retry puts it back. The posting is
followed by a made-up follower (anyform.reach): no browser, no network, a throwaway database."""

import pytest

from backend.apply import anyform, execution, greenhouse, runner, shortlist
from backend.apply.execution import APPLIED, FAILED, PENDING, QUEUED
from qa.tests.test_execution_queue import client, db, item_of  # noqa: F401
from qa.tests.test_other_boards import Browser, on_board
from qa.tests.test_prepared_documents import desk  # noqa: F401

POSTING = "https://himalayas.example/companies/versa/jobs/3"


@pytest.fixture
def follow(db, desk, monkeypatch):  # noqa: F811
    """Where each job's posting leads, made up: job id -> (kind, url, error, stop)."""
    leads, opened, waits = {}, [], []

    def reach(page, url, *, say=print, check_s=180, hops=5):
        waits.append(check_s)
        kind, there, error, stop = leads[int(url.rstrip("/").split("/")[-1])]
        if stop == "check":
            say("   The site is checking the browser first. If it shows a box to tick, tick it in the window.")
        return anyform.Reached(page, kind, there, error, stop)
    monkeypatch.setattr(anyform, "reach", reach)
    monkeypatch.setattr(runner, "open_own_browser", lambda url: opened.append(url) or True)
    monkeypatch.setattr(greenhouse, "watch_submission", lambda page, timeout_s, **kw: ("submitted", {}))
    from backend.apply import prepare
    monkeypatch.setattr(prepare, "open_questions", lambda job, applicant: [])
    Browser.forms = []
    return leads, opened, waits


def entry(job_id):
    item = item_of(job_id)
    return {"state": item.state, "action": item.action, "outcome": item.outcome, "note": item.note}


def messages(job_id):
    return [e["message"] for e in execution.events_after(0, item_id=item_of(job_id).id)]


def test_a_check_that_does_not_clear_goes_to_the_persons_own_browser_never_failed_never_applied(db, desk, follow):
    cfg, _, _ = db
    leads, opened, waits = follow
    on_board(3, "himalayas", POSTING)
    leads[3] = (None, POSTING, "the site's browser check did not clear", "check")
    execution.add([3])
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert waits == [runner.CHECK_WAIT_S] == [180]                 # 3 minutes in the app's browser
    item = item_of(3)
    assert item.state == PENDING and item.state not in (FAILED, APPLIED)
    assert item.action.startswith(runner.CHECK_HANDOFF) and "Send it from your own Chrome" in item.action
    assert opened == [POSTING]                                          # once the app's window is closed
    handed = anyform.take_unconfirmed(3)
    assert handed["handoff"] is True and handed["url"] == POSTING
    assert shortlist.stage(entry(3))["label"] == "Ready for manual submission"
    said = messages(3)
    assert any("checking the browser" in m for m in said)              # its history kept, step by step
    assert not any(m.startswith("Applied") for m in said)


def test_the_persons_word_settles_a_handed_off_job_and_retry_brings_it_back(db, desk, follow):
    cfg, _, _ = db
    leads, _, _ = follow
    on_board(3, "himalayas", POSTING)
    leads[3] = (None, POSTING, "the site's browser check did not clear", "check")
    execution.add([3])
    runner.run(cfg, chosen=True, browser_factory=Browser)
    execution.confirm(item_of(3).id, went=False)
    assert item_of(3).state == FAILED and item_of(3).outcome.startswith("Not sent: you said so")
    execution.retry(item_of(3).id)
    assert item_of(3).state == QUEUED


def test_the_person_saying_it_went_records_it_as_applied(db, desk, follow):
    cfg, _, _ = db
    leads, _, _ = follow
    on_board(3, "himalayas", POSTING)
    leads[3] = (None, POSTING, "the site's browser check did not clear", "check")
    execution.add([3])
    runner.run(cfg, chosen=True, browser_factory=Browser)
    execution.confirm(item_of(3).id, went=True)
    assert item_of(3).state == APPLIED                                   # only on the person's word


def test_a_closed_posting_fails_with_its_reason_and_retry_puts_it_back(db, desk, follow):
    cfg, _, _ = db
    leads, opened, waits = follow
    on_board(3, "ashby", "https://jobs.ashbyhq.com/lumos/3")
    leads[3] = (None, "https://jobs.ashbyhq.com/lumos",
                "the posting has closed or moved: jobs.ashbyhq.com showed another page instead of this job", "gone")
    execution.add([3])
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert item_of(3).state == FAILED and opened == []
    assert item_of(3).outcome == ("Not sent: the posting has closed or moved: jobs.ashbyhq.com showed another "
                                  "page instead of this job. Retry puts it back in the queue.")
    assert shortlist.stage(entry(3))["label"] == "Failed / blocked"
    execution.retry(item_of(3).id)
    assert item_of(3).state == QUEUED


def test_a_form_that_could_not_open_twice_says_so(db, desk, follow):
    cfg, _, _ = db
    leads, _, _ = follow
    on_board(3, "ashby", "https://jobs.ashbyhq.com/lumos/3")
    leads[3] = (None, "https://jobs.ashbyhq.com/lumos/3",
                "could not open its form (Page.goto: net::ERR_ABORTED; tried 2 times)", None)
    execution.add([3])
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert item_of(3).outcome == ("Not sent: could not open its form (Page.goto: net::ERR_ABORTED; tried 2 times). "
                                  "Retry puts it back in the queue.")


def test_a_sign_in_hand_off_keeps_its_old_words(db, desk, follow, monkeypatch):
    """Only a check's hand-off says why in its waiting note; a Lever or sign-in hand-off reads as before."""
    cfg, _, _ = db
    leads, _, _ = follow
    on_board(3, "jobicy", "https://posting.example/3")
    leads[3] = ("signin-handoff", "https://login.example/x", None, None)
    monkeypatch.setattr(anyform, "sign_in_barrier", lambda page: "Sign-in was blocked in the app's browser.")
    execution.add([3])
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert item_of(3).state == PENDING and item_of(3).action.startswith("Send it from your own Chrome")
