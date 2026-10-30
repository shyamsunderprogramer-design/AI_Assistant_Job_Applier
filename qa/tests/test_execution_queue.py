"""The chosen jobs' queue (backend/apply/execution.py): kept in the database, gone through one at a
time, never lost, never applied to twice. A temporary database file and a fake browser: nothing here
opens a real form, a model or the network (conftest blocks them too)."""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.apply import chosen, execution, prepare, runner
from backend.apply.execution import (APPLIED, CANCELLED, FAILED, LABELS, MOVES, PENDING, PREPARING, QUEUED,
                                     READY, RUNNING, SKIPPED, VALIDATING, WAITING, QueueError)
from data_engineering.db import session as session_mod
from data_engineering.db.models import ApplyEvent, ApplyItem, ApplyRun, Job
from data_engineering.db.session import get_session, init_engine

NOW = datetime.now(timezone.utc)
ROWS = {  # id: company, board, fit, status, open
    1: ("Globex", "greenhouse", 0.95, "Not Applied", True),
    2: ("Acme", "jazzhr", 0.80, "Not Applied", True),
    3: ("Initech", "jobvite", 0.85, "Not Applied", True),        # a board a run cannot fill
    4: ("Globex", "greenhouse", 0.70, "Not Applied", True),      # Globex's second job
    5: ("Hooli", "workday", 0.75, "Not Applied", True),
    6: ("Massive Dynamic", "greenhouse", 0.99, "Not Applied", True),   # never chosen
}


class Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class Person:
    max_per_day = 10
    stop_before_submit = True

    def __init__(self):
        self.authorisation, self.answers, self.job, self.job_answers = {}, {}, None, {}


class Page:
    def __init__(self):
        self.closed = False

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


class Context:
    def __init__(self):
        self.pages = []

    def new_page(self):
        self.pages.append(Page())
        return self.pages[-1]


@pytest.fixture
def db(tmp_path, monkeypatch):
    before = (session_mod._engine, session_mod._SessionFactory)
    url = f"sqlite:///{tmp_path / 'jobs.db'}"
    init_engine(url)
    with get_session() as s:
        for n, (company, board, fit, status, open_) in ROWS.items():
            s.add(Job(id=n, source=board, company=company, company_slug=company.lower(), external_id=str(n),
                      title="Platform Engineer", location="Remote", description="x " * 400, requirements="",
                      application_url=f"https://example.com/{n}", content_hash=f"h{n}", is_open=open_,
                      ats_match_score=fit, score_basis="full", status=status, posted_at=NOW - timedelta(days=2)))
    from data_engineering.scraper.filters import JobFilter
    monkeypatch.setattr(runner, "searched_places", lambda cfg: JobFilter())
    monkeypatch.setattr(runner, "load", lambda path: Person())
    monkeypatch.setattr(runner, "check_ready", lambda applicant: [])
    monkeypatch.setattr(runner, "say", lambda line: None)
    monkeypatch.setattr(prepare, "PREPARED", tmp_path / "prepared.json")
    yield Cfg({"limits.max_applications_per_day": 15}), url, tmp_path
    session_mod._engine, session_mod._SessionFactory = before


def item_of(job_id):
    with get_session() as s:
        item = s.query(ApplyItem).filter(ApplyItem.job_id == job_id).order_by(ApplyItem.id.desc()).first()
        s.expunge(item)
        return item


def states():
    with get_session() as s:
        return {i.job_id: i.state for i in s.query(ApplyItem).order_by(ApplyItem.id)}


def fake_form(results, *, opened=None, before=None):
    """A form filler that never opens a page: the result each job gets, recorded as the real one does."""
    def fill_form(cfg, page, job, applicant, head, ready, *, auto, wait_s):
        (opened if opened is not None else []).append(job.id)
        if before:
            before(job)
        result = results[job.id]
        if result == "submitted":
            runner.record_application(job.id, {"company": job.company, "title": job.title, "how": "you"})
        return runner.Outcome(job.id, job.company, job.title, result, how="you")
    return fill_form


@pytest.fixture
def documents(monkeypatch, tmp_path):
    made = []

    def prepare_documents(cfg, job, applicant, head, *, tailor):
        made.append(job.id)
        (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4 made-up resume")   # a real file: its checksum is kept
        return runner.Prepared(tmp_path / "resume.pdf", None, "tailored", tmp_path)
    monkeypatch.setattr(runner, "prepare_documents", prepare_documents)
    return made


# -- the states and their moves -------------------------------------------------------------------

def test_every_state_is_named_and_only_the_listed_moves_are_made(db):
    assert set(LABELS) == set(MOVES) == {QUEUED, VALIDATING, PREPARING, READY, RUNNING, WAITING, PENDING,
                                         APPLIED, FAILED, SKIPPED, CANCELLED}
    assert MOVES[APPLIED] == set() and QUEUED not in MOVES[PENDING]     # nothing undoes an application,
    execution.add([1])                                                  # and nothing that may have gone
    item = item_of(1)                                                   # is tried again by itself
    with pytest.raises(QueueError, match="cannot become"):
        execution.move(item.id, RUNNING)
    execution.move(item.id, VALIDATING)
    with pytest.raises(QueueError):
        execution.move(item.id, PREPARING, expect=QUEUED)               # someone else moved it first
    with get_session() as s:
        events = [e.state for e in s.query(ApplyEvent).filter(ApplyEvent.item_id == item.id).order_by(ApplyEvent.id)]
    assert events == [QUEUED, VALIDATING] and item_of(1).attempts == 1


def test_a_job_on_its_way_is_never_on_the_list_twice(db):
    assert execution.add([1, 2, 1]) == ([1, 2], [])
    assert execution.add([2, 4]) == ([4], [2])
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):                                  # the database itself refuses it
        with get_session() as s:
            s.add(ApplyItem(job_id=1, state=QUEUED, position=99))
    execution.cancel(item_of(1).id)
    assert execution.add([1]) == ([1], [])                              # finished, it may be chosen again


def test_the_list_survives_a_restart(db):
    cfg, url, _ = db
    execution.add([1, 2])
    execution.move(item_of(2).id, VALIDATING)
    execution.move(item_of(2).id, SKIPPED, outcome="Can't go: example.")
    init_engine(url)                                                    # the app, or the Mac, starts again
    assert states() == {1: QUEUED, 2: SKIPPED}
    assert item_of(2).outcome == "Can't go: example."


# -- a crash, and what may have gone -------------------------------------------------------------

def dead_pid():
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    return int(done.stdout)


def test_after_a_crash_unopened_forms_go_back_and_open_ones_wait_for_the_persons_word(db):
    execution.add([1, 2, 4, 5])
    with get_session() as s:
        run = ApplyRun(state="running", pid=dead_pid(), started_at=NOW.replace(tzinfo=None))
        s.add(run)
        s.flush()
        run_id = run.id
    for job_id, path in ((1, [VALIDATING]), (2, [VALIDATING, PREPARING]), (4, [VALIDATING, PREPARING, READY, RUNNING]),
                         (5, [VALIDATING, PREPARING, READY, RUNNING, WAITING])):
        for state in path:
            execution.move(item_of(job_id).id, state, run_id=run_id)
    assert execution.recover() == {"runs": 1, "requeued": 2, "to_confirm": 2}
    assert states() == {1: QUEUED, 2: QUEUED, 4: PENDING, 5: PENDING}
    assert "nothing was sent" in item_of(1).note
    assert "may have been sent" in item_of(4).action
    with get_session() as s:
        assert s.get(ApplyRun, run_id).state == "interrupted"
    assert execution.recover() == {"runs": 0, "requeued": 0, "to_confirm": 0}     # safe to run again


def test_a_process_that_reuses_a_dead_runs_number_is_not_that_run(db):
    with get_session() as s:
        run = ApplyRun(state="running", pid=os.getpid(), started_at=(NOW - timedelta(days=3)).replace(tzinfo=None))
        s.add(run)
    assert execution.current_run() is None and execution.recover()["runs"] == 1


def test_what_may_have_gone_is_never_tried_again_until_the_person_says(db):
    cfg, _, tmp_path = db
    execution.add([1])
    item = item_of(1)
    for state in (VALIDATING, PREPARING, READY, RUNNING, PENDING):
        execution.move(item.id, state, action="check" if state == PENDING else None)
    with pytest.raises(QueueError, match="Say first"):
        execution.retry(item.id)
    assert execution._next(cfg, Person(), 1, runner.searched_places(cfg)) == (None, "Done: nothing more on your list.")
    execution.confirm(item.id, went=False)
    assert item_of(1).state == FAILED
    execution.retry(item.id)
    assert item_of(1).state == QUEUED


def test_the_persons_yes_records_the_application_and_it_is_never_opened_again(db, documents, monkeypatch):
    cfg, _, _ = db
    execution.add([2])
    item = item_of(2)
    for state in (VALIDATING, PREPARING, READY, RUNNING, PENDING):
        execution.move(item.id, state)
    execution.confirm(item.id, went=True)
    assert item_of(2).state == APPLIED
    with get_session() as s:
        assert s.get(Job, 2).status == "Applied" and s.get(Job, 2).applied_at is not None
    assert json.loads(runner.AUDIT_LOG.read_text().splitlines()[-1])["job_id"] == 2
    with pytest.raises(QueueError, match="cannot become"):
        execution.move(item.id, QUEUED)                                  # applied is final
    opened = []
    monkeypatch.setattr(runner, "fill_form", fake_form({2: "submitted"}, opened=opened))
    execution.add([2])                                                   # put on the list again somehow:
    runner.run(cfg, chosen=True, browser_factory=Context)                # a run still never opens it
    assert opened == [] and item_of(2).state == APPLIED                  # brought in line with the job first
    assert item_of(2).outcome == "Marked Applied another way (its own page, or your email)."


# -- a run -------------------------------------------------------------------------------------------

def test_a_run_applies_to_exactly_the_chosen_jobs_one_a_company(db, documents, monkeypatch):
    cfg, _, _ = db
    opened = []
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "submitted", 2: "submitted", 3: "submitted",
                                                       4: "submitted", 5: "submitted"}, opened=opened))
    execution.add([2, 4, 1, 3, 5])
    outcomes = runner.run(cfg, chosen=True, browser_factory=Context)
    assert opened == [1, 3, 2, 5]                    # best fit first; Globex once; never 6; Jobvite assisted
    assert states() == {2: APPLIED, 4: QUEUED, 1: APPLIED, 3: APPLIED, 5: APPLIED}
    assert [o.result for o in outcomes] == ["submitted"] * 4
    with get_session() as s:
        run = s.query(ApplyRun).one()
        assert run.state == "stopped" and run.note.startswith("Nothing more can go now")


def test_eligibility_is_checked_again_just_before_each_job(db, documents, monkeypatch):
    cfg, _, tmp_path = db
    opened = []

    def close_acme_while_tailoring(cfg, job, applicant, head, *, tailor):
        if job.id == 2:
            with get_session() as s:
                s.get(Job, 2).is_open = False                # the posting closes meanwhile
        (tmp_path / "r.pdf").write_bytes(b"%PDF-1.4 made-up resume")
        return runner.Prepared(tmp_path / "r.pdf", None, "tailored", tmp_path)
    monkeypatch.setattr(runner, "prepare_documents", close_acme_while_tailoring)
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "submitted", 2: "submitted"}, opened=opened))
    execution.add([1, 2])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert opened == [1] and states() == {1: APPLIED, 2: SKIPPED}
    assert item_of(2).outcome == "Can't go: the posting has closed."
    with get_session() as s:                                         # left at its own last check, not sent back
        moved = [e.state for e in s.query(ApplyEvent).filter(ApplyEvent.item_id == item_of(2).id).order_by(ApplyEvent.id)
                 if e.state]
    assert moved == [QUEUED, VALIDATING, PREPARING, READY, SKIPPED]


def test_todays_limit_is_kept(db, documents, monkeypatch):
    cfg, _, _ = db

    class OneADay(Person):
        max_per_day = 1
    monkeypatch.setattr(runner, "load", lambda path: OneADay())
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "submitted", 2: "submitted"}))
    execution.add([1, 2])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert states() == {1: APPLIED, 2: QUEUED}
    with get_session() as s:
        assert s.query(ApplyRun).one().note == "Today's limit is reached: the rest wait for tomorrow."


def test_pause_stops_before_the_next_form_and_resume_goes_on(db, documents, monkeypatch):
    cfg, _, _ = db
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "submitted", 2: "submitted"},
                                                       before=lambda job: execution.request_pause()))
    execution.add([1, 2])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert states() == {1: APPLIED, 2: QUEUED}
    with get_session() as s:
        assert s.query(ApplyRun).one().state == "paused"
    monkeypatch.setattr(runner, "fill_form", fake_form({2: "submitted"}))
    runner.run(cfg, chosen=True, browser_factory=Context)              # Resume: a new run
    assert states() == {1: APPLIED, 2: APPLIED}


def test_a_cancelled_job_is_not_opened_and_retry_puts_it_back(db, documents, monkeypatch):
    cfg, _, _ = db
    opened = []
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "submitted", 2: "submitted"}, opened=opened))
    execution.add([1, 2])
    execution.cancel(item_of(2).id)
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert opened == [1] and states() == {1: APPLIED, 2: CANCELLED}
    execution.retry(item_of(2).id)
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert opened == [1, 2] and states() == {1: APPLIED, 2: APPLIED}


def test_no_answer_in_time_waits_for_the_persons_word_and_is_never_reopened(db, documents, monkeypatch):
    cfg, _, _ = db
    opened = []

    def waits(job):
        runner.say("   Waiting for you — check it, then press Submit (outlined in orange).")
        assert item_of(job.id).state == WAITING                          # the page shows it while it waits
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "timeout"}, opened=opened, before=waits))
    execution.add([1])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert states() == {1: PENDING} and "may have gone" in item_of(1).action
    runner.run(cfg, chosen=True, browser_factory=Context)                # pressed again: still not reopened
    assert opened == [1]


def test_an_error_with_a_form_open_leaves_it_waiting_for_the_persons_word(db, documents, monkeypatch):
    cfg, _, _ = db

    def broken(*args, **kwargs):
        raise RuntimeError("the page went away")
    monkeypatch.setattr(runner, "fill_form", broken)
    execution.add([1])
    with pytest.raises(RuntimeError):
        runner.run(cfg, chosen=True, browser_factory=Context)
    assert states() == {1: PENDING}
    with get_session() as s:
        assert s.query(ApplyRun).one().state == "stopped"


def test_one_run_at_a_time(db):
    execution.start_run(os.getpid())
    with pytest.raises(execution.Busy):
        execution.start_run(os.getpid())


def test_a_job_applied_to_another_way_is_applied_here_too(db):
    execution.add([1, 2])
    with get_session() as s:
        s.get(Job, 2).status = "Applied"
    assert execution.sync_with_jobs() == 1 and states() == {1: QUEUED, 2: APPLIED}


def test_the_queue_kept_in_the_file_moves_into_the_database_once(db):
    chosen.PATH.parent.mkdir(parents=True, exist_ok=True)
    chosen.PATH.write_text(json.dumps({"selected": [5], "queue": [{"job_id": 1}, {"job_id": 2}]}))
    assert chosen.migrate_queue() == 2 and chosen.migrate_queue() == 0
    assert states() == {1: QUEUED, 2: QUEUED} and chosen.selected_ids() == [5]


# -- the page --------------------------------------------------------------------------------------

@pytest.fixture
def client(db, monkeypatch):
    from backend.api import app as webapp
    cfg = db[0]
    monkeypatch.setattr(webapp, "cfg", lambda: cfg)
    monkeypatch.setattr("backend.api.chosen_pages.cfg", lambda: cfg)
    started = []
    monkeypatch.setattr("backend.api.chosen_pages.start_task",
                        lambda task, args: started.append((task, args)) or webapp.jsonify(ok=True))
    monkeypatch.setattr("backend.api.chosen_pages.task_pid", lambda task: None)
    return webapp.app.test_client(), started


def test_the_page_shows_each_job_and_the_activity_and_takes_the_controls(client):
    c, started = client
    execution.add([1, 3])
    with get_session() as s:
        s.get(Job, 3).is_open = False                                 # closed: it cannot go
    view = c.get("/apply/chosen").get_json()
    assert [(i["job_id"], i["label"], i.get("turn")) for i in view["items"]] == [(1, "Queued", "ready"),
                                                                                (3, "Queued", "blocked")]
    assert view["next_run"] == 1 and view["run"]["live"] is False and view["events"][0]["message"]
    assert c.post("/apply/chosen/pause").status_code == 409             # nothing running
    for body in ({}, {"item": "1"}, {"item": True}, {"item": -1}):
        assert c.post("/apply/chosen/cancel", json=body).status_code == 400
    assert c.post("/apply/chosen/confirm", json={"item": 1, "went": "yes"}).status_code == 400
    assert c.post("/apply/chosen/retry", json={"item": item_of(1).id}).status_code == 409   # queued: no retry
    assert c.post("/apply/chosen/start").get_json() == {"ok": True}
    assert started == [("apply-chosen", ["apply", "--chosen"])]
    assert c.post("/apply/chosen/cancel", json={"item": item_of(1).id}).get_json() == {"ok": True}
    assert c.post("/apply/chosen/start").status_code == 409             # nothing left that can go


def test_the_start_is_refused_while_a_run_is_alive(client):
    c, started = client
    execution.add([1])
    execution.start_run(os.getpid())
    assert c.post("/apply/chosen/start").status_code == 409 and started == []
