"""The chosen jobs' queue under pressure (25 Oct 2026): several processes on one database file at the
same moment, a worker killed with its form open, an application recorded twice, older databases
upgraded, an import cut short, and every control tried in every state.

Real processes on a real SQLite file, with test_execution_queue's made-up jobs. Nothing here opens a
browser, asks a model or reaches the network: conftest refuses them in this process, and each child
process refuses them too (CHILD's first lines) before it does anything else."""

import json
import os
import subprocess
import sys
import time
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from backend.apply import anyform, chosen, execution, runner
from backend.apply.execution import (APPLIED, CANCELLED, CANT_CANCEL, FAILED, LABELS, MOVES, PENDING, PREPARING,
                                     QUEUED, READY, RUNNING, SKIPPED, VALIDATING, WAITING, Conflict, QueueError)
from backend.config.loader import PROJECT_ROOT
from data_engineering.db import session as session_mod
from data_engineering.db.models import FINISHED_STATES, ApplyEvent, ApplyItem, ApplyRun, Base, Job
from data_engineering.db.session import get_session, init_engine
from qa.tests.test_execution_queue import (NOW, ROWS, Context, client, db, documents, fake_form,  # noqa: F401
                                           item_of, states)

STATES = list(LABELS)

# -- other processes -------------------------------------------------------------------------------

CHILD = r'''
"""One process of a test: does one thing to the queue on the test's database file, says how it went."""
import json, os, socket, subprocess, sys, time
from pathlib import Path

url, folder, op, *args = sys.argv[1:]
folder = Path(folder)


def refuse(*a, **k):
    raise ConnectionRefusedError("a test process may not use the network")


socket.socket.connect = socket.socket.connect_ex = refuse          # nothing leaves this Mac
socket.create_connection = socket.getaddrinfo = refuse
_Popen = subprocess.Popen


class Popen(_Popen):                                                # and no program runs but ps
    def __init__(self, cmd, *a, **k):
        name = os.path.basename(str(cmd[0] if isinstance(cmd, (list, tuple)) else cmd).split()[0])
        if name != "ps":
            raise PermissionError(f"a test process may only run ps, not {name}")
        super().__init__(cmd, *a, **k)


subprocess.Popen = Popen

from backend.apply import anyform, chosen, execution, greenhouse, prepare, runner  # noqa: E402
from data_engineering.db.session import init_engine  # noqa: E402
from data_engineering.scraper.filters import JobFilter  # noqa: E402

chosen.PATH, anyform.UNCONFIRMED = folder / "chosen.json", folder / "unconfirmed.json"
prepare.PREPARED, runner.AUDIT_LOG = folder / "prepared.json", folder / "applications.jsonl"
greenhouse.RUNS_DIR = folder / "runs"
runner.say = lambda line: None
init_engine(url)


class Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class Person:
    max_per_day, stop_before_submit = 10, True

    def __init__(self):
        self.authorisation, self.answers, self.job, self.job_answers = {}, {}, None, {}


class Page:
    def is_closed(self):
        return False

    def close(self):
        pass


class Context:
    def new_page(self):
        return Page()


def wait_for(name):
    while not (folder / name).exists():
        time.sleep(0.001)


def say(name, value):
    """Written whole, then named: the test reads a file once it is there, and written in place it
    could be there and still empty -- a busy Mac once caught one so (JSONDecodeError, 25 Oct 2026)."""
    draft = folder / f".{name}.draft"
    draft.write_text(json.dumps(value))
    draft.rename(folder / name)


if op == "hold":            # a run whose job has got as far as args[1], and stays there
    item_id, upto = int(args[0]), args[1]
    run = execution.start_run(os.getpid())
    path = [execution.VALIDATING, execution.PREPARING, execution.READY, execution.RUNNING, execution.WAITING]
    for state in path[:path.index(upto) + 1]:
        execution.move(item_id, state, run_id=run.id)
    say("held", {"pid": os.getpid(), "run": run.id})
    time.sleep(600)
    raise SystemExit(0)

(folder / f"ready-{os.getpid()}").touch()
wait_for("go")
try:
    if op == "start":
        execution.start_run(os.getpid())
        result = "ok"
    elif op == "claim":
        execution.move(int(args[0]), execution.VALIDATING, expect=execution.QUEUED)
        result = "ok"
    elif op == "add":
        added, already = execution.add([int(args[0])])
        result = "added" if added else "already"
    elif op == "record":
        went = runner.record_application(int(args[0]), {"how": "test", "pid": os.getpid()})
        result = "recorded" if went else "had it"
    elif op == "keep":                    # a run keeping "did it go?" entries
        for n in range(int(args[0]), int(args[1])):
            anyform.keep_unconfirmed(n, {"how": "test"})
        result = "kept"
    elif op == "take":                    # the page taking others, at the same time
        result = f"took {sum(anyform.take_unconfirmed(n) is not None for n in range(int(args[0]), int(args[1])))}"
    elif op == "work":
        runner.searched_places = lambda cfg: JobFilter()
        own = folder / f"documents-{os.getpid()}"          # each process its own: they run at once
        own.mkdir()
        (own / "resume.pdf").write_bytes(b"%PDF-1.4 made-up resume")
        runner.prepare_documents = lambda cfg, job, applicant, head, *, tailor: runner.Prepared(
            own / "resume.pdf", None, "kept", own)

        def fill_form(cfg, page, job, applicant, head, ready, *, auto, wait_s):
            with (folder / "opened.log").open("a") as log:
                log.write(f"{os.getpid()} {job.id}\n")
            runner.record_application(job.id, {"company": job.company, "title": job.title, "how": "you"})
            return runner.Outcome(job.id, job.company, job.title, "submitted", how="you")
        runner.fill_form = fill_form
        execution.work(Cfg({"limits.max_applications_per_day": 15}), Person(), browser_factory=Context,
                       tailor=False)
        result = "ran"
    else:
        result = f"no such op: {op}"
except execution.Busy:
    result = "busy"
except execution.Conflict:
    result = "conflict"
except execution.QueueError as exc:
    result = f"refused: {exc}"
say(f"result-{os.getpid()}.json", result)
wait_for("done")            # alive until every process has tried: a run counts while its process lives
'''

ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""),
       "PYTHONPATH": str(PROJECT_ROOT), "LANG": "C", "LC_ALL": "C"}      # no keys of any kind


def spawn(folder, url, op, *args):
    # Written once, whole. Rewritten for every process, it was emptied and filled again while the one
    # before was still starting: that one read an empty program and stopped at once, with nothing said
    # (a full run under heavy load, 26 Oct 2026; 50 of 60 did so with the rewrite slowed down).
    script = folder / "child.py"
    if not script.exists():
        draft = folder / ".child.py.draft"
        draft.write_text(CHILD)
        draft.rename(script)
    return subprocess.Popen([sys.executable, str(script), url, str(folder), op, *map(str, args)],
                            cwd=str(folder), env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def wait_until(done, procs, what, timeout=90):
    end = time.monotonic() + timeout
    while not done():
        for p in procs:
            if p.poll() is not None:
                pytest.fail(f"a process stopped before {what}: {p.stderr.read()[-2000:]}")
        if time.monotonic() > end:
            pytest.fail(f"no {what} in {timeout} s")
        time.sleep(0.01)


def together(folder, url, op, *args, n=6):
    """`n` processes doing `op` at the same moment; how each one went."""
    return at_once(folder, url, [(op, *args)] * n)


def at_once(folder, url, jobs):
    """A process for each (op, *args), all at the same moment; how each one went."""
    n = len(jobs)
    procs = [spawn(folder, url, *job) for job in jobs]
    try:
        wait_until(lambda: len(list(folder.glob("ready-*"))) == n, procs, "every process ready")
        (folder / "go").touch()
        wait_until(lambda: len(list(folder.glob("result-*.json"))) == n, procs, "every result")
        return [json.loads(f.read_text()) for f in sorted(folder.glob("result-*.json"))]
    finally:
        (folder / "done").touch()
        for p in procs:
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


def audit_lines(folder, job_id):
    log = folder / "applications.jsonl"
    return [e for e in map(json.loads, log.read_text().splitlines()) if e["job_id"] == job_id] if log.exists() else []


def test_runs_started_at_the_same_moment_cannot_both_go(db):
    _, url, folder = db
    assert sorted(together(folder, url, "start")) == ["busy"] * 5 + ["ok"]
    with get_session() as s:
        assert s.query(ApplyRun).count() == 1


def test_a_job_taken_by_several_processes_at_once_goes_to_exactly_one(db):
    _, url, folder = db
    execution.add([1])
    item = item_of(1)
    assert sorted(together(folder, url, "claim", item.id)) == ["conflict"] * 5 + ["ok"]
    assert item_of(1).state == VALIDATING and item_of(1).attempts == 1
    with get_session() as s:
        moves = [e.state for e in s.query(ApplyEvent).filter(ApplyEvent.item_id == item.id).order_by(ApplyEvent.id)]
    assert moves == [QUEUED, VALIDATING]


def test_one_job_added_by_several_processes_at_once_is_on_the_list_once(db):
    _, url, folder = db
    results = together(folder, url, "add", 6)
    assert results.count("added") == 1, results
    assert all(r == "already" or r.startswith("refused: One of those jobs was added") for r in results if r != "added")
    with get_session() as s:
        assert s.query(ApplyItem).filter(ApplyItem.job_id == 6).count() == 1


def test_an_application_recorded_by_several_processes_at_once_is_recorded_once(db):
    _, url, folder = db
    assert sorted(together(folder, url, "record", 2)) == ["had it"] * 5 + ["recorded"]
    assert len(audit_lines(folder, 2)) == 1
    with get_session() as s:
        job = s.get(Job, 2)
        assert job.status == "Applied" and job.applied_at is not None


def test_runs_started_together_open_each_chosen_job_once(db):
    _, url, folder = db
    execution.add([1, 2, 5])
    results = together(folder, url, "work", n=3)
    assert "ran" in results and set(results) <= {"ran", "busy"}, results
    opened = [line.split() for line in (folder / "opened.log").read_text().splitlines()]
    assert sorted(int(job) for _, job in opened) == [1, 2, 5]          # each one once, all told
    assert len({pid for pid, _ in opened}) == 1                         # by one run
    assert states() == {1: APPLIED, 2: APPLIED, 5: APPLIED}
    assert all(len(audit_lines(folder, j)) == 1 for j in (1, 2, 5))


# -- a worker killed with a job in hand, and the app started again -----------------------------------

@pytest.mark.parametrize("upto, after", [(PREPARING, QUEUED), (RUNNING, PENDING), (WAITING, PENDING)])
def test_a_worker_killed_mid_job_leaves_it_safe_after_a_restart(db, documents, monkeypatch, upto, after):
    cfg, url, folder = db
    execution.add([1, 2])
    item = item_of(1)
    worker = spawn(folder, url, "hold", item.id, upto)
    try:
        wait_until(lambda: (folder / "held").exists(), [worker], "the worker holding its job")
        # While it lives, nothing takes its job from it.
        assert execution.recover() == {"runs": 0, "requeued": 0, "to_confirm": 0}
        assert item_of(1).state == upto
        with pytest.raises(execution.Busy):
            execution.start_run(os.getpid())
        if upto in (RUNNING, WAITING):
            with pytest.raises(Conflict, match="Its form is open"):
                execution.cancel(item.id)
        if upto == WAITING:
            with pytest.raises(QueueError, match="still open"):
                execution.confirm(item.id, went=True)
            execution.settled_elsewhere(1, went=True)                   # left to the run that has it
            assert item_of(1).state == WAITING
    finally:
        worker.kill()                                                    # SIGKILL: no goodbye
        worker.wait(timeout=30)

    init_engine(url)                                                     # the app starts again
    assert execution.recover()["runs"] == 1
    assert states() == {1: after, 2: QUEUED}
    with get_session() as s:
        assert [r.state for r in s.query(ApplyRun)] == ["interrupted"]
    assert execution.recover() == {"runs": 0, "requeued": 0, "to_confirm": 0}
    init_engine(url)                                                     # and again: nothing changes
    assert execution.recover() == {"runs": 0, "requeued": 0, "to_confirm": 0} and states()[1] == after

    opened = []
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "submitted", 2: "submitted"}, opened=opened))
    runner.run(cfg, chosen=True, browser_factory=Context)
    if after == QUEUED:
        assert opened == [1, 2] and states() == {1: APPLIED, 2: APPLIED}      # nothing had gone: it goes now
        return
    assert opened == [2] and item_of(1).state == PENDING                       # it may have gone: never reopened
    assert "may have been sent" in item_of(1).action
    execution.confirm(item.id, went=True)                                       # the person's word
    assert item_of(1).state == APPLIED and len(audit_lines(folder, 1)) == 1


def test_recovery_leaves_alone_a_run_that_starts_while_it_looks(db, monkeypatch):
    execution.add([1])
    item, started, real = item_of(1), [], execution._orphans

    def a_run_starts_meanwhile():                 # between recovery reading the runs and the jobs
        if not started:
            with get_session() as s:
                run = ApplyRun(state="running", pid=os.getpid(), started_at=NOW.replace(tzinfo=None))
                s.add(run)
                s.flush()
                started.append(run.id)
            for state in (VALIDATING, PREPARING, READY, RUNNING):
                execution.move(item.id, state, run_id=started[0])
        return real()
    monkeypatch.setattr(execution, "_orphans", a_run_starts_meanwhile)
    assert execution.recover() == {"runs": 0, "requeued": 0, "to_confirm": 0}
    assert item_of(1).state == RUNNING


def test_recovery_never_brings_back_a_job_cancelled_while_it_looks(db, monkeypatch):
    execution.add([1])
    with get_session() as s:
        run = ApplyRun(state="interrupted", pid=None, started_at=NOW.replace(tzinfo=None))
        s.add(run)
        s.flush()
        run_id = run.id
    execution.move(item_of(1).id, VALIDATING, run_id=run_id)
    real = execution._orphans

    def cancelled_meanwhile():
        found = real()
        execution.cancel(item_of(1).id)          # pressed on the page in that moment
        return found
    monkeypatch.setattr(execution, "_orphans", cancelled_meanwhile)
    assert execution.recover()["requeued"] == 0
    assert item_of(1).state == CANCELLED


# -- recorded once ------------------------------------------------------------------------------------

def test_an_application_is_recorded_once_whoever_says_it_went(db):
    _, _, folder = db
    first = NOW - timedelta(days=3)
    assert runner.record_application(2, {"how": "you"}, at=first) is True
    with get_session() as s:
        s.get(Job, 2).status = "Interviewing"                     # the person moved it on since
    assert runner.record_application(2, {"how": "email"}) is False
    with get_session() as s:
        job = s.get(Job, 2)
        assert job.status == "Interviewing" and job.applied_at == first.replace(tzinfo=None)
    assert [e["how"] for e in audit_lines(folder, 2)] == ["you"]
    with get_session() as s:
        s.get(Job, 4).status = "Offer"                            # a stage set by hand, never dated
    assert runner.record_application(4, {"how": "you"}) is True
    with get_session() as s:
        assert s.get(Job, 4).status == "Offer" and s.get(Job, 4).applied_at is not None
    assert runner.record_application(4, {"how": "you"}) is False and len(audit_lines(folder, 4)) == 1
    assert runner.record_application(999, {"how": "you"}) is True     # no such job: still written down


# -- every control, in every state --------------------------------------------------------------------

def put(job_id, state, **fields):
    """An entry for this job in this state, written straight into the table (the moves that lead to
    each state are made in test_execution_queue); any other live entry for the job is cancelled first."""
    with get_session() as s:
        s.query(ApplyItem).filter(ApplyItem.job_id == job_id, ApplyItem.state.notin_(FINISHED_STATES)) \
            .update({"state": CANCELLED}, synchronize_session=False)
        item = ApplyItem(job_id=job_id, state=state, position=0, **fields)
        s.add(item)
        s.flush()
        return item.id


def state_of(item_id):
    with get_session() as s:
        return s.get(ApplyItem, item_id).state


def events_of(item_id):
    with get_session() as s:
        return s.query(ApplyEvent).filter(ApplyEvent.item_id == item_id).count()


@pytest.mark.parametrize("here", STATES)
def test_from_each_state_only_the_moves_the_machine_lists_are_made(db, here):
    for to in STATES:
        item_id = put(1, here)
        if to in MOVES[here]:
            assert execution.move(item_id, to).state == to and events_of(item_id) == 1
            continue
        with pytest.raises(QueueError, match="cannot become") as refused:
            execution.move(item_id, to)
        assert not isinstance(refused.value, Conflict)          # a mistake is said, never passed over
        assert state_of(item_id) == here and events_of(item_id) == 0


CONTROLS = {  # the page's buttons: what each does, from which states, and where it leaves the job
    "cancel": (lambda i: execution.cancel(i), {QUEUED, VALIDATING, PREPARING, READY}, CANCELLED),
    "retry": (lambda i: execution.retry(i), {FAILED, SKIPPED, CANCELLED}, QUEUED),
    "yes, it went": (lambda i: execution.confirm(i, True), {PENDING, WAITING}, APPLIED),
    "no, it wasn't sent": (lambda i: execution.confirm(i, False), {PENDING, WAITING}, FAILED),
}


@pytest.mark.parametrize("here", STATES)
@pytest.mark.parametrize("control", CONTROLS)
def test_every_control_in_every_state(db, control, here):
    do, allowed, after = CONTROLS[control]
    item_id = put(2, here)
    if here in allowed:
        assert do(item_id).state == after
    else:
        with pytest.raises(QueueError):
            do(item_id)
        assert state_of(item_id) == here
    recorded = control == "yes, it went" and here in allowed
    with get_session() as s:
        assert (s.get(Job, 2).status == "Applied") is recorded
    assert len(audit_lines(db[2], 2)) == int(recorded)


def test_with_a_run_live_a_waiting_form_is_the_persons_to_finish(db):
    run = execution.start_run(os.getpid())
    item_id = put(2, WAITING, run_id=run.id)
    with pytest.raises(QueueError, match="still open"):
        execution.confirm(item_id, went=True)
    with pytest.raises(Conflict) as refused:
        execution.cancel(item_id)
    assert str(refused.value) == CANT_CANCEL[WAITING]
    execution.settled_elsewhere(2, went=True)
    assert state_of(item_id) == WAITING
    execution.finish_run(run.id, "done", "Done")
    execution.settled_elsewhere(2, went=True)                 # the run is over: the person's word stands
    assert state_of(item_id) == APPLIED


def test_clear_takes_only_finished_jobs_off_the_page_and_keeps_their_record(db):
    ids = {state: put(100 + n, state) for n, state in enumerate(STATES)}
    assert execution.hide_finished() == len(FINISHED_STATES)
    with get_session() as s:
        hidden = {i.state: i.hidden for i in s.query(ApplyItem).filter(ApplyItem.id.in_(ids.values()))}
    assert hidden == {state: state in FINISHED_STATES for state in STATES}
    assert execution.hide_finished() == 0


@pytest.mark.parametrize("here", STATES)
def test_a_job_applied_to_another_way_follows_only_where_no_form_is_open(db, here):
    item_id = put(2, here)
    with get_session() as s:
        s.get(Job, 2).status = "Applied"
    execution.sync_with_jobs()
    assert state_of(item_id) == (APPLIED if here in (QUEUED, VALIDATING, PREPARING, READY, PENDING) else here)


@pytest.mark.parametrize("here", STATES)
def test_did_it_go_answered_on_the_jobs_own_page_follows_only_where_it_may_have_gone(db, here):
    item_id = put(2, here)
    execution.settled_elsewhere(2, went=False)
    assert state_of(item_id) == (FAILED if here in (PENDING, WAITING) else here)


# -- "did it go?" answered twice: two tabs, or the job's own page (25 Oct 2026) ------------------------
# The first answer stands. A yes claims the job's entry and marks the job in one transaction; until
# then a yes recorded the job first and claimed after, so a no landing in between left the list
# saying Failed with the job marked Applied.

def test_a_late_yes_from_another_tab_cannot_overwrite_a_no(db):
    item_id = put(2, PENDING)
    assert execution.confirm(item_id, went=False).state == FAILED
    with pytest.raises(Conflict, match="answered a moment ago .* it is failed now"):
        execution.confirm(item_id, went=True)
    with get_session() as s:
        assert s.get(Job, 2).status == "Not Applied"
    assert state_of(item_id) == FAILED and audit_lines(db[2], 2) == []


def test_a_late_no_from_another_tab_cannot_overwrite_a_yes(db):
    item_id = put(2, PENDING)
    assert execution.confirm(item_id, went=True).state == APPLIED
    with pytest.raises(Conflict, match="answered a moment ago .* it is applied now"):
        execution.confirm(item_id, went=False)
    with get_session() as s:
        assert s.get(Job, 2).status == "Applied"
    assert state_of(item_id) == APPLIED and len(audit_lines(db[2], 2)) == 1


def test_a_no_landing_while_a_yes_is_on_its_way_stands_alone(db, monkeypatch):
    item_id, real, pressed = put(2, PENDING), anyform.unconfirmed, []

    def the_other_tab_says_no_meanwhile(job_id):         # the yes reads the form's entry, then claims
        if not pressed:
            pressed.append("no")
            execution.confirm(item_id, went=False)
        return real(job_id)
    monkeypatch.setattr(anyform, "unconfirmed", the_other_tab_says_no_meanwhile)
    with pytest.raises(Conflict, match="answered a moment ago"):
        execution.confirm(item_id, went=True)
    with get_session() as s:
        assert s.get(Job, 2).status == "Not Applied"           # recorded first, it was Applied here
    assert state_of(item_id) == FAILED and audit_lines(db[2], 2) == []


def test_yes_and_no_at_the_same_moment_one_stands_and_the_list_and_the_job_agree(db):
    import threading
    yeses = 0
    for _ in range(20):
        with get_session() as s:                              # each round from the start
            job = s.get(Job, 2)
            job.status, job.applied_at = "Not Applied", None
        item_id, results = put(2, PENDING), {}
        barrier = threading.Barrier(2)

        def press(went):
            barrier.wait()
            try:
                results[went] = execution.confirm(item_id, went=went).state
            except QueueError as exc:
                results[went] = str(exc)
        threads = [threading.Thread(target=press, args=(went,)) for went in (True, False)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        stood = [went for went, said in results.items() if said in (APPLIED, FAILED)]
        assert len(stood) == 1, results
        with get_session() as s:
            status = s.get(Job, 2).status
        assert (state_of(item_id), status) == ((APPLIED, "Applied") if stood == [True] else (FAILED, "Not Applied"))
        yeses += stood == [True]
    assert len(audit_lines(db[2], 2)) == yeses                  # one line for each yes that stood, no more


def test_the_jobs_own_page_cannot_answer_over_an_answer_given_first(client):
    c, _ = client
    anyform.keep_unconfirmed(2, {"company": "Acme", "title": "Platform Engineer", "how": "anyform"})
    item_id = put(2, PENDING)
    execution.confirm(item_id, went=False)                    # answered on the Auto-apply page
    late = c.post("/job/2/auto-apply/went", json={"went": True})     # the job's page, left open
    assert late.status_code == 409 and "answered already" in late.get_json()["error"]
    with get_session() as s:
        assert s.get(Job, 2).status == "Not Applied"
    assert state_of(item_id) == FAILED


def test_two_tabs_of_a_jobs_page_the_first_answer_stands(client, db):
    c, _ = client
    anyform.keep_unconfirmed(2, {"company": "Acme", "title": "Platform Engineer", "how": "anyform"})
    assert c.post("/job/2/auto-apply/went", json={"went": False}).get_json() == {"ok": True, "applied": False}
    assert c.post("/job/2/auto-apply/went", json={"went": True}).status_code == 409
    with get_session() as s:
        assert s.get(Job, 2).status == "Not Applied"
    assert audit_lines(db[2], 2) == []


def test_a_yes_on_the_jobs_page_settles_its_list_entry_with_one_record(client, db):
    c, _ = client
    anyform.keep_unconfirmed(2, {"company": "Acme", "title": "Platform Engineer", "how": "anyform"})
    item_id = put(2, PENDING)
    assert c.post("/job/2/auto-apply/went", json={"went": True}).get_json() == {"ok": True, "applied": True}
    assert state_of(item_id) == APPLIED and [e["how"] for e in audit_lines(db[2], 2)] == ["anyform"]
    with pytest.raises(QueueError):
        execution.confirm(item_id, went=True)                 # the list's own button, late
    assert len(audit_lines(db[2], 2)) == 1


def test_a_failure_while_a_yes_is_recorded_changes_nothing(db, monkeypatch):
    item_id, real = put(2, PENDING), runner.mark_applied

    def disk_full(session, job_id, now):
        raise OSError("disk full")
    monkeypatch.setattr(runner, "mark_applied", disk_full)
    with pytest.raises(OSError):
        execution.confirm(item_id, went=True)
    with get_session() as s:
        assert s.get(Job, 2).status == "Not Applied"
    assert state_of(item_id) == PENDING and audit_lines(db[2], 2) == []      # all or nothing: ask again
    # Only this one put back: monkeypatch.undo() would undo conftest's too, the redirect of the
    # application log among them -- it did once, and a test line went into the person's own log.
    monkeypatch.setattr(runner, "mark_applied", real)
    assert execution.confirm(item_id, went=True).state == APPLIED and len(audit_lines(db[2], 2)) == 1


def test_two_tabs_of_a_jobs_page_at_the_same_moment_one_answer_stands(client, db):
    import threading

    from backend.api import app as webapp
    yeses = 0
    for _ in range(15):
        with get_session() as s:
            job = s.get(Job, 2)
            job.status, job.applied_at = "Not Applied", None
        anyform.keep_unconfirmed(2, {"company": "Acme", "title": "Platform Engineer", "how": "anyform"})
        codes, barrier = {}, threading.Barrier(2)

        def press(went):
            tab = webapp.app.test_client()
            barrier.wait()
            codes[went] = tab.post("/job/2/auto-apply/went", json={"went": went}).status_code
        threads = [threading.Thread(target=press, args=(went,)) for went in (True, False)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert sorted(codes.values()) == [200, 409], codes
        with get_session() as s:
            status = s.get(Job, 2).status
        assert status == ("Applied" if codes[True] == 200 else "Not Applied")
        yeses += codes[True] == 200
    assert len(audit_lines(db[2], 2)) == yeses


def test_a_run_and_the_page_changing_the_did_it_go_list_at_once_lose_nothing(db):
    _, url, folder = db
    for n in range(1, 41):
        anyform.keep_unconfirmed(n, {"how": "test"})              # forms waiting for an answer
    results = at_once(folder, url, [("keep", 101, 141), ("take", 1, 41)])
    assert sorted(results) == ["kept", "took 40"]
    left = json.loads(anyform.UNCONFIRMED.read_text())
    assert sorted(map(int, left)) == list(range(101, 141))        # every new one kept, every taken one gone


# -- a form that asks for more than one thing ----------------------------------------------------------

def test_a_further_request_from_a_waiting_form_replaces_the_one_shown_and_the_activity_keeps_both(
        db, documents, monkeypatch):
    cfg, _, _ = db
    shown = []

    def two_requests(job):
        runner.say("   Waiting for you — tick the CAPTCHA box.")
        shown.append(item_of(job.id).action)
        runner.say("   Waiting for you — check it, then press Submit (outlined in orange).")
        shown.append(item_of(job.id).action)
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "submitted"}, before=two_requests))
    execution.add([1])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert shown == ["tick the CAPTCHA box.", "check it, then press Submit (outlined in orange)."]
    with get_session() as s:
        said = [e.message for e in s.query(ApplyEvent).filter(ApplyEvent.item_id == item_of(1).id)]
    assert any("tick the CAPTCHA box." in m for m in said) and any("press Submit" in m for m in said)
    assert item_of(1).state == APPLIED


def test_a_request_after_its_form_was_settled_changes_nothing(db):
    item_id = put(2, PENDING, action="Check whether it was sent.")
    assert execution._ask_again(item_id, "press Submit") is False
    with get_session() as s:
        assert s.get(ApplyItem, item_id).action == "Check whether it was sent."


# -- cancel and pause against a run -------------------------------------------------------------------

def made(folder):
    (folder / "resume.pdf").write_bytes(b"%PDF-1.4 made-up resume")    # a real file: its checksum is kept
    return runner.Prepared(folder / "resume.pdf", None, "tailored", folder)


def test_cancel_while_its_documents_are_made_means_its_form_is_never_opened(db, monkeypatch):
    cfg, _, folder = db
    pressed = []

    def cancel_meanwhile(cfg, job, applicant, head, *, tailor):
        if job.id == 1 and not pressed:
            pressed.append(execution.cancel(item_of(1).id).state)    # pressed on the page while it tailors
        return made(folder)
    monkeypatch.setattr(runner, "prepare_documents", cancel_meanwhile)
    opened = []
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "submitted", 2: "submitted"}, opened=opened))
    execution.add([1, 2])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert pressed == [CANCELLED] and opened == [2] and states() == {1: CANCELLED, 2: APPLIED}
    execution.retry(item_of(1).id)                               # trying it again is safe: it goes once
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert opened == [2, 1] and states() == {1: APPLIED, 2: APPLIED}


def test_cancel_in_the_moment_before_its_form_opens_means_it_is_never_opened(db, documents, monkeypatch):
    cfg, _, _ = db
    real = execution.pause_asked

    def cancel_when_ready(run_id):                               # asked just after it became ready
        if item_of(1).state == READY:
            execution.cancel(item_of(1).id)
        return real(run_id)
    monkeypatch.setattr(execution, "pause_asked", cancel_when_ready)
    opened = []
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "submitted", 2: "submitted"}, opened=opened))
    execution.add([1, 2])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert opened == [2] and states() == {1: CANCELLED, 2: APPLIED}


def test_cancel_never_interrupts_a_form_that_is_open(db, documents, monkeypatch):
    cfg, _, _ = db
    refused, opened = [], []

    def press_cancel(job):
        for line in (None, "   Waiting for you — check it, then press Submit (outlined in orange)."):
            if line:
                runner.say(line)
            try:
                execution.cancel(item_of(job.id).id)
            except Conflict as exc:
                refused.append((item_of(job.id).state, str(exc)))
        execution.cancel_waiting()                               # "Cancel waiting": only the queued one
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "submitted", 2: "submitted"}, opened=opened,
                                                       before=press_cancel))
    execution.add([1, 2])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert refused == [(RUNNING, CANT_CANCEL[RUNNING]), (WAITING, CANT_CANCEL[WAITING])]
    assert opened == [1] and states() == {1: APPLIED, 2: CANCELLED}


def test_pause_while_its_documents_are_made_stops_before_its_form_opens(db, monkeypatch):
    cfg, _, folder = db
    asked = []

    def pause_meanwhile(cfg, job, applicant, head, *, tailor):
        if not asked:
            asked.append(execution.request_pause())
        return made(folder)
    monkeypatch.setattr(runner, "prepare_documents", pause_meanwhile)
    opened = []
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "submitted"}, opened=opened))
    execution.add([1])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert asked == [True] and opened == [] and item_of(1).state == QUEUED
    assert item_of(1).note == "Paused before its form opened: ready to go when you resume."
    with get_session() as s:
        assert s.query(ApplyRun).one().state == "paused"
    runner.run(cfg, chosen=True, browser_factory=Context)        # Resume
    assert opened == [1] and item_of(1).state == APPLIED


def test_a_job_applied_to_another_way_during_a_run_is_applied_not_skipped(db, monkeypatch):
    cfg, _, folder = db

    def an_email_says_it_went(cfg, job, applicant, head, *, tailor):
        if job.id == 1:
            runner.record_application(1, {"how": "an employer's email"})
        return made(folder)
    monkeypatch.setattr(runner, "prepare_documents", an_email_says_it_went)
    opened = []
    monkeypatch.setattr(runner, "fill_form", fake_form({1: "submitted", 2: "submitted"}, opened=opened))
    execution.add([1, 2])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert opened == [2] and states() == {1: APPLIED, 2: APPLIED}
    assert item_of(1).outcome == "Marked Applied another way (its own page, or your email)."


# -- the page -----------------------------------------------------------------------------------------

def test_the_page_names_every_state_and_offers_only_what_can_be_done(client):
    c, _ = client
    run = execution.start_run(os.getpid())                       # alive: this process
    for n, state in enumerate(STATES):
        put(100 + n, state, run_id=run.id)
    view = c.get("/apply/chosen").get_json()
    shown = {i["state"]: i for i in view["items"]}
    assert {s: i["label"] for s, i in shown.items()} == LABELS
    assert {s for s, i in shown.items() if i["can"]["cancel"]} == {QUEUED, VALIDATING, PREPARING, READY}
    assert {s for s, i in shown.items() if i["can"]["retry"]} == {FAILED, SKIPPED, CANCELLED}
    assert {s for s, i in shown.items() if i["can"]["confirm"]} == {PENDING}     # its form is open in the run
    page = (PROJECT_ROOT / "frontend" / "templates" / "apply.html").read_text(encoding="utf-8")
    assert all(f".st-{s}" in page for s in STATES if s != QUEUED)                # queued: the plain pill
    refused = c.post("/apply/chosen/cancel", json={"item": shown[RUNNING]["id"]})
    assert refused.status_code == 409 and refused.get_json()["error"] == CANT_CANCEL[RUNNING]


# -- older databases, and an import cut short ---------------------------------------------------------

@pytest.fixture
def engine_back():
    before = (session_mod._engine, session_mod._SessionFactory)
    yield
    session_mod._engine, session_mod._SessionFactory = before


def old_database(path, *, apply_tables_sql=()):
    """A database as an older app left it: every table but the queue's, and the made-up jobs."""
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables
                                             if not t.name.startswith("apply_")])
    with Session(engine) as s:
        for n, (company, board, fit, status, open_) in ROWS.items():
            s.add(Job(id=n, source=board, company=company, company_slug=company.lower(), external_id=str(n),
                      title="Platform Engineer", location="Remote", description="x", requirements="",
                      application_url=f"https://example.com/{n}", content_hash=f"h{n}", is_open=open_,
                      ats_match_score=fit, score_basis="full", status=status, posted_at=NOW - timedelta(days=2)))
        s.commit()
    with engine.begin() as c:
        for sql in apply_tables_sql:
            c.execute(text(sql) if isinstance(sql, str) else sql)
    engine.dispose()
    return f"sqlite:///{path}"


def indexes(url):
    engine = create_engine(url)
    with engine.connect() as c:
        found = {name for (name,) in c.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'index'")}
    engine.dispose()
    return found


def jobs_now():
    with get_session() as s:
        return {j.id: (j.company, j.status) for j in s.query(Job)}


def test_a_database_from_before_the_queue_gains_it_and_keeps_every_job(tmp_path, engine_back):
    url = old_database(tmp_path / "old.db")
    init_engine(url)
    assert jobs_now() == {n: (row[0], row[3]) for n, row in ROWS.items()}
    assert {"uq_apply_items_live_job", "uq_apply_runs_one_live"} <= indexes(url)
    assert execution.add([1, 1, 2]) == ([1, 2], [])
    execution.start_run(os.getpid())
    with pytest.raises(IntegrityError):                          # the database itself: one live run
        with get_session() as s:
            s.add(ApplyRun(state="running", pid=1, started_at=NOW.replace(tzinfo=None)))


def test_a_database_from_the_first_queue_build_gets_its_guards_and_loses_no_row(tmp_path, engine_back):
    early_items = ("CREATE TABLE apply_items (id INTEGER PRIMARY KEY, job_id INTEGER, state VARCHAR(24), "
                   "position INTEGER, attempts INTEGER, run_id INTEGER, note TEXT, action TEXT, outcome TEXT, "
                   "added_at DATETIME, updated_at DATETIME, finished_at DATETIME)")     # no "hidden" yet
    when = "'2026-10-24 20:00:00.000000'"
    url = old_database(tmp_path / "early.db", apply_tables_sql=(
        early_items, CreateTable(ApplyRun.__table__), CreateTable(ApplyEvent.__table__),       # no indexes
        f"INSERT INTO apply_items (id, job_id, state, position, attempts, added_at, updated_at) VALUES "
        f"(1, 1, 'queued', 1, 0, {when}, {when}), (2, 1, 'queued', 2, 0, {when}, {when}), "
        f"(3, 2, 'applied', 3, 1, {when}, {when}), (4, 2, 'queued', 4, 0, {when}, {when})",
        f"INSERT INTO apply_runs (id, state, pid, started_at) VALUES "
        f"(1, 'running', 1, {when}), (2, 'running', 2, {when}), (3, 'done', 3, {when})"))
    init_engine(url)
    with get_session() as s:
        items = {i.id: (i.job_id, i.state, i.hidden, i.outcome) for i in s.query(ApplyItem)}
        runs = {r.id: (r.state, r.finished_at is not None) for r in s.query(ApplyRun)}
    assert items == {1: (1, QUEUED, False, None), 2: (1, CANCELLED, False, "Cancelled: on the list twice."),
                     3: (2, APPLIED, False, None), 4: (2, QUEUED, False, None)}
    assert runs == {1: ("interrupted", True), 2: ("running", False), 3: ("done", False)}
    assert {"uq_apply_items_live_job", "uq_apply_runs_one_live"} <= indexes(url)
    assert jobs_now() == {n: (row[0], row[3]) for n, row in ROWS.items()}
    with pytest.raises(IntegrityError):
        with get_session() as s:
            s.add(ApplyItem(job_id=1, state=QUEUED, position=9))
    from data_engineering.db.migrate import ensure_schema
    assert ensure_schema(session_mod._engine) == []                 # done once: the next start adds nothing
    assert execution.recover()["runs"] == 1                         # the run left live: its process is gone


def test_an_import_cut_short_after_the_database_is_never_done_twice(db):
    chosen.PATH.write_text(json.dumps({"selected": [], "queue": [{"job_id": 1}, {"job_id": 2}, {"job_id": 4}]}))
    execution.add([1, 2])                 # the import got this far: in the database, the file not emptied
    execution.cancel(item_of(2).id)       # and one was cancelled before the app started again
    assert chosen.migrate_queue() == 1    # only the one the database never had
    assert states() == {1: QUEUED, 2: CANCELLED, 4: QUEUED}
    assert chosen.load()["queue"] == [] and chosen.migrate_queue() == 0


def test_an_import_cut_short_in_the_database_adds_nothing_and_the_next_one_adds_all(db, monkeypatch):
    chosen.PATH.write_text(json.dumps({"queue": [{"job_id": 1}, {"job_id": 2}]}))
    real = execution._event

    def power_cut_at_the_second(session, message, *, item=None, **fields):
        if item is not None and item.job_id == 2:
            raise OSError("the Mac lost power")
        return real(session, message, item=item, **fields)
    monkeypatch.setattr(execution, "_event", power_cut_at_the_second)
    with pytest.raises(OSError):
        chosen.migrate_queue()
    assert states() == {}                                            # all or nothing
    assert [e["job_id"] for e in chosen.load()["queue"]] == [1, 2]   # and the file still has both
    monkeypatch.setattr(execution, "_event", real)
    assert chosen.migrate_queue() == 2 and states() == {1: QUEUED, 2: QUEUED}
