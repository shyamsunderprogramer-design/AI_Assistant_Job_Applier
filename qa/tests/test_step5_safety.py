"""Step 5 of the Morning shortlist plan (7 Nov 2026): final safety.

  1. Apply to these goes to exactly the jobs waiting when the person pressed: one added meanwhile waits
     for the next press, one cancelled meanwhile is not gone to, two runs never go at once, and a crash
     leaves nothing sent or lost.
  2. No morning pick is ever sent by the app: Greenhouse, Workday, JazzHR and any other site's form,
     with the person's Submit setting at "send by itself" and the auto-Submit hold on or off -- while a
     job the person chose themselves is still sent by itself where it always was (so the check can fail).
  3. The record: one application, its first dates kept, its history whole, under queue operations at
     the same moment from several threads on a database file.

Made-up jobs; a throwaway database file per test; forms and boards stood in for: no browser, network or
real application, and the person's own records never read (conftest)."""

import json
import os
import threading
from datetime import datetime

import pytest

from backend.apply import anyform, execution, greenhouse, jazzhr, runner, shortlist, workday
from backend.apply.execution import APPLIED, CANCELLED, FAILED, PENDING, QUEUED
from data_engineering.db.models import ApplyEvent, ApplyItem, ApplyRun, ShortlistPick
from data_engineering.db.session import get_session
from qa.tests.test_eligibility_decisions import Answers, as_person, posted
from qa.tests.test_execution_queue import Context, client, db, dead_pid, documents, item_of  # noqa: F401
from qa.tests.test_morning_pick_submit import SAYS_IT_SPONSORS, made_ready, picked_by_the_morning

# -- 1. exactly the jobs pressed for -------------------------------------------------------------------


def doing(went, meanwhile=None):
    """A form filler that opens no page: each job's form 'sent by the person'; `meanwhile` runs while the
    first one is open -- the person (or the morning) acting on the list during the run."""
    def fill_form(cfg, page, job, applicant, head, ready, *, auto, wait_s):
        went.append(job.id)
        if meanwhile and len(went) == 1:
            meanwhile()
        runner.record_application(job.id, {"company": job.company, "title": job.title, "how": "you"})
        return runner.Outcome(job.id, job.company, job.title, "submitted", how="you")
    return fill_form


def test_a_job_added_while_the_run_goes_waits_for_the_next_press(db, documents, monkeypatch):
    cfg, _, _ = db
    went = []
    monkeypatch.setattr(runner, "fill_form", doing(went, lambda: execution.add([5])))
    execution.add([1, 6])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert went == [6, 1] or went == [1, 6]
    assert 5 not in went and item_of(5).state == QUEUED and item_of(5).attempts == 0
    assert item_of(1).state == item_of(6).state == APPLIED
    went.clear()
    runner.run(cfg, chosen=True, browser_factory=Context)               # the next press: its turn
    assert went == [5]


def test_a_job_cancelled_while_the_run_goes_is_not_gone_to(db, documents, monkeypatch):
    cfg, _, _ = db
    went = []
    execution.add([1, 6])
    other = {}

    def cancel_the_other():                     # the one whose form is not open
        other["job"] = ({1, 6} - set(went)).pop()
        execution.cancel(item_of(other["job"]).id)
    monkeypatch.setattr(runner, "fill_form", doing(went, cancel_the_other))
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert len(went) == 1 and other["job"] not in went
    assert item_of(other["job"]).state == CANCELLED and item_of(went[0]).state == APPLIED


def test_the_press_is_written_down_and_only_those_entries_go(db, documents, client, monkeypatch):
    cfg, _, _ = db
    c, started = client
    went = []
    monkeypatch.setattr(runner, "fill_form", doing(went))
    execution.add([1, 6])
    assert c.post("/apply/chosen/start").get_json() == {"ok": True}
    (task, args), = started
    pressed = [int(i) for i in args[args.index("--items") + 1].split(",")]
    assert task == "apply-chosen" and sorted(pressed) == sorted([item_of(1).id, item_of(6).id])
    execution.add([5])                                                  # after the press, before the run starts
    runner.run(cfg, chosen=True, browser_factory=Context, only=pressed)
    assert sorted(went) == [1, 6] and item_of(5).state == QUEUED


def test_the_command_line_takes_the_entries_pressed_for():
    import main
    args = main.build_parser().parse_args(["apply", "--chosen", "--items", "4,9"])
    assert args.chosen and main._items(args.items) == [4, 9]
    assert main._items(main.build_parser().parse_args(["apply", "--chosen"]).items) is None   # the old page's start


def test_two_presses_at_the_same_moment_make_one_run(db):
    results, gate = [], threading.Barrier(2)

    def press():
        gate.wait()
        try:
            results.append(execution.start_run(os.getpid()).id)
        except execution.Busy:
            results.append("busy")
    threads = [threading.Thread(target=press) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(map(str, results)).count("busy") == 1
    with get_session() as s:
        assert s.query(ApplyRun).filter(ApplyRun.state == "running").count() == 1


def test_a_crash_with_a_form_open_sends_nothing_and_loses_nothing(db):
    execution.add([1, 6])
    with get_session() as s:
        run = ApplyRun(state="running", pid=dead_pid(), started_at=datetime(2026, 11, 7, 12))
        s.add(run)
        s.flush()
        first, second = s.query(ApplyItem).order_by(ApplyItem.position).all()
        first.state, first.run_id = "running", run.id                    # its form was open
        added = (first.added_at, second.added_at)
    fixed = execution.recover()
    assert fixed["to_confirm"] == 1 and item_of(1).state == PENDING and item_of(6).state == QUEUED
    assert "may have been sent" in item_of(1).action                    # the person says whether it went
    assert (item_of(1).added_at, item_of(6).added_at) == added          # first dates kept
    assert not runner.AUDIT_LOG.exists()                                 # nothing recorded as sent


# -- 2. no morning pick is sent by the app, on any form -------------------------------------------------

class Boards:
    """Every kind of form the app fills, stood in for: what each was let to do. The person 'presses
    Submit' on none of them in time, so a form the app did not send waits for the person's word."""

    def __init__(self, monkeypatch):
        self.seen = {}
        mp = monkeypatch

        def gh_fill(page, job_id, url, applicant, resume=None, letter=None, *, submit=False, evidence=None, **kw):
            self.seen[job_id] = ("greenhouse", submit)
            return greenhouse.FillResult(job_id=job_id, url=url, submitted=submit)
        mp.setattr(greenhouse, "fill", gh_fill)
        mp.setattr(greenhouse, "await_submission", lambda page, timeout_s, **kw: "submitted")
        mp.setattr(greenhouse, "challenge_shown", lambda page: False)
        mp.setattr(greenhouse, "watch_submission", lambda page, timeout_s, **kw: ("timeout", {}))

        def wd_apply(page, job_id, url, applicant, resume, **kw):
            self.seen[job_id] = ("workday", False)                    # it has no Submit to be given
            return "timeout", greenhouse.FillResult(job_id=job_id, url=url)
        mp.setattr(workday, "apply", wd_apply)

        def jz_fill(page, job_id, url, applicant, resume, **kw):
            self.seen[job_id] = ("jazzhr", False)
            return greenhouse.FillResult(job_id=job_id, url=url)
        mp.setattr(jazzhr, "fill", jz_fill)
        mp.setattr(jazzhr, "watch_submission", lambda page, timeout_s, **kw: ("timeout", {}))

        def any_reach(page, url, **kw):
            return anyform.Reached(page, "form", url)

        def any_fill(page, job_id, url, applicant, *a, **kw):
            self.seen[job_id] = ("any site", False)
            return greenhouse.FillResult(job_id=job_id, url=url)
        mp.setattr(anyform, "reach", any_reach)
        mp.setattr(anyform, "fill", any_fill)
        mp.setattr(anyform, "show_submit", lambda *a, **kw: False)
        mp.setattr(anyform, "watch", lambda page, *a, **kw: ("timeout", {}, page))
        from backend.apply import prepare
        mp.setattr(prepare, "open_questions", lambda job, applicant: [])


@pytest.mark.parametrize("held", [False, True])
def test_no_morning_pick_is_sent_by_the_app_on_any_kind_of_form(db, documents, monkeypatch, held):
    """The person's setting: send by itself where nothing is left for them. The hold: off, or on."""
    cfg, _, _ = db
    if held:
        execution.AUTO_SUBMIT_HOLD.write_text("held by the test")
    assert execution.auto_submit_held() is held                         # the test's own hold, never the live one
    for n in (1, 2, 3, 5, 6):
        posted(n, SAYS_IT_SPONSORS)                                     # Eligible: nothing in doubt
    as_person(monkeypatch, Answers(stop_before_submit=False, requires_sponsorship=True, authorised_to_work=False))
    made_ready(db, monkeypatch, 1, 2, 3, 5, 6)                          # documents made, checked, approved
    for n in (2, 3, 5, 6):
        picked_by_the_morning(n)                                         # 1 is the person's own choice
    boards = Boards(monkeypatch)
    execution.add([1, 2, 3, 5, 6])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert boards.seen == {1: ("greenhouse", not held), 2: ("jazzhr", False), 3: ("any site", False),
                           5: ("workday", False), 6: ("greenhouse", False)}
    for n in (2, 3, 5, 6):
        assert item_of(n).state == PENDING                              # waiting for the person's word
        assert not item_of(n).outcome or "sent automatically" not in item_of(n).outcome
    lines = runner.AUDIT_LOG.read_text().splitlines() if runner.AUDIT_LOG.exists() else []
    sent = [json.loads(line) for line in lines]
    assert all(r["job_id"] == 1 for r in sent)                           # the app sent only the person's own...
    assert (item_of(1).state == APPLIED) is (not held)                   # ...and only with the hold off


def test_the_morning_pick_rule_holds_whatever_its_shortlist_setting_says(db, documents, monkeypatch):
    """A pick stays a pick after the shortlist is switched off: the rule reads the record, not the switch."""
    cfg, _, _ = db
    posted(6, SAYS_IT_SPONSORS)
    as_person(monkeypatch, Answers(stop_before_submit=False, requires_sponsorship=True, authorised_to_work=False))
    made_ready(db, monkeypatch, 6)
    picked_by_the_morning(6)
    assert not shortlist.enabled(cfg)
    boards = Boards(monkeypatch)
    execution.add([6])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert boards.seen == {6: ("greenhouse", False)} and item_of(6).state == PENDING


# -- 3. the record ----------------------------------------------------------------------------------------

def test_two_yes_it_went_at_once_make_one_application(db):
    execution.add([1])
    item = item_of(1).id
    with get_session() as s:
        s.get(ApplyItem, item).state = PENDING
    gate, errors = threading.Barrier(2), []

    def say_yes():
        gate.wait()
        try:
            execution.confirm(item, went=True)
        except Exception as exc:                  # the second is refused: it is applied already
            errors.append(type(exc).__name__)
    threads = [threading.Thread(target=say_yes) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert item_of(1).state == APPLIED and len(errors) <= 1
    with get_session() as s:
        said = [e.message for e in s.query(ApplyEvent).filter(ApplyEvent.item_id == item)]
    assert sum(m.startswith("Applied") for m in said) == 1               # one application in its history
    lines = runner.AUDIT_LOG.read_text().splitlines() if runner.AUDIT_LOG.exists() else []
    assert len([line for line in lines if json.loads(line)["job_id"] == 1]) <= 1


def test_adding_the_same_job_from_two_places_at_once_puts_it_on_the_list_once(db):
    gate, outcomes = threading.Barrier(4), []

    def add():
        gate.wait()
        try:
            outcomes.append(execution.add([1]))
        except execution.QueueError:
            outcomes.append("refused")
    threads = [threading.Thread(target=add) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with get_session() as s:
        assert s.query(ApplyItem).filter(ApplyItem.job_id == 1).count() == 1


def test_a_failed_job_retried_keeps_its_first_date_and_its_whole_history(db, documents, monkeypatch):
    cfg, _, _ = db
    execution.add([1])
    first = item_of(1).added_at

    def fails(cfg, page, job, applicant, head, ready, *, auto, wait_s):
        out = runner.Outcome(job.id, job.company, job.title, "not-filled")
        out.detail = "the form could not be reached"
        return out
    monkeypatch.setattr(runner, "fill_form", fails)
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert item_of(1).state == FAILED
    before = len(execution.events_after(0, item_id=item_of(1).id))
    execution.retry(item_of(1).id)
    assert item_of(1).state == QUEUED and item_of(1).added_at == first
    assert len(execution.events_after(0, item_id=item_of(1).id)) > before   # added to, never rewritten


def test_the_tests_never_touch_the_persons_own_records(db):
    """The databases and files here are this test's own (conftest's guard)."""
    from backend.config.loader import PROJECT_ROOT
    assert runner.AUDIT_LOG != PROJECT_ROOT / "data" / "applications.jsonl"
    assert execution.AUTO_SUBMIT_HOLD != PROJECT_ROOT / "data" / "apply" / "auto_submit.hold"
    with get_session() as s:
        assert "data/jobs.db" not in str(s.get_bind().url)
    assert ShortlistPick.__tablename__ == "shortlist_picks"
