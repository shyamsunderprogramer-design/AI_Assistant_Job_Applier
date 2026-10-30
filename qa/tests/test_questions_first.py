"""Auto-apply Phase 3, step 3 (26 Oct 2026): questions first. When a chosen job's documents are made
ready its form's questions are read from the board (prepare.open_questions) and kept with its answers;
one with required questions nothing answers yet waits -- no new state, it stays queued -- while the run
goes on with the rest; the Questions page asks them once and the answers reach its form; a sensitive
question is never answered by a guess; and the answers given are kept. Made-up jobs, a made-up
Greenhouse board answering from this test, a temporary database: no browser, model or network."""

import json
import os
import subprocess
import sys
import threading

import pytest

from backend.apply import chosen, execution, prepare, runner
from backend.apply.execution import APPLIED, QUEUED, VALIDATING
from backend.config.loader import PROJECT_ROOT
from data_engineering.db.models import ApplyEvent, ApplyRun
from data_engineering.db.session import get_session
from qa.tests.test_execution_queue import Context, client, db, item_of  # noqa: F401
from qa.tests.test_prepared_documents import Browser, dana, desk  # noqa: F401

WHY = "Why do you want to work at Globex?"
CITIZEN = "Are you a US citizen?"
# Each made-up job's form, as Greenhouse's board API gives it: the filler's own fields, the profile's,
# a sensitive one, an essay and an optional one.
FORMS = {
    1: [{"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
        {"label": "Resume/CV", "required": True, "fields": [{"name": "resume", "type": "input_file"}]},
        {"label": WHY, "required": True, "fields": [{"name": "question_1", "type": "textarea"}]},
        {"label": "Are you legally authorized to work in the United States?", "required": True,
         "fields": [{"name": "question_2", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
        {"label": CITIZEN, "required": True,
         "fields": [{"name": "question_3", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
        {"label": "Twitter handle", "required": False, "fields": [{"name": "question_4", "type": "input_text"}]}],
    6: [{"label": "LinkedIn Profile", "required": False, "fields": [{"name": "question_5", "type": "input_text"}]}],
}


class Board:
    """Greenhouse's board API, made up: each job's form questions, and how many times it was asked."""

    def __init__(self):
        self.asked = []

    def get(self, url, timeout=None, **kw):
        job_id = int(url.split("/jobs/")[1].split("?")[0])
        self.asked.append(job_id)
        questions = FORMS.get(job_id, [])
        return type("Reply", (), {"json": lambda self: {"questions": questions}})()


@pytest.fixture
def board(db, desk, monkeypatch):
    made = Board()
    monkeypatch.setattr(prepare.requests, "get", made.get)
    from data_engineering.db.models import Job
    with get_session() as s:                         # the made-up jobs' board names, for its address
        for job in s.query(Job):
            job.company_slug, job.external_id = "example", str(job.id)
    monkeypatch.setattr(runner, "load", lambda path: person())
    return made


def person(**authorisation):
    who = dana(**authorisation)
    who.links = {"linkedin": "https://linkedin.com/in/dana-example"}
    return who


def steps(job_id):
    with get_session() as s:
        return [e.message for e in s.query(ApplyEvent).filter(ApplyEvent.job_id == job_id).order_by(ApplyEvent.id)]


def last_run():
    with get_session() as s:
        return s.query(ApplyRun).order_by(ApplyRun.id.desc()).first().note


def filled(went):
    """A form filler that never opens a page: what the person's answers say to this job's questions."""
    def fill_form(cfg, page, job, applicant, head, ready, *, auto, wait_s):
        went[job.id] = {label: applicant.answer_for(label) for label in (WHY, CITIZEN, "Twitter handle")}
        runner.record_application(job.id, {"company": job.company, "title": job.title, "how": "you"})
        return runner.Outcome(job.id, job.company, job.title, "submitted", how="you")
    return fill_form


# -- read ahead, and set aside ---------------------------------------------------------------------

def test_each_forms_questions_are_read_when_it_is_made_ready_and_one_left_to_answer_waits(db, desk, board, monkeypatch):
    cfg, _, _ = db
    went = {}
    monkeypatch.setattr(runner, "fill_form", filled(went))
    execution.add([1, 6])
    assert runner.prepare_chosen(cfg) == 2 and sorted(board.asked) == [1, 6]
    entry = prepare.load()["1"]
    assert entry["state"] == prepare.CHOSEN and entry not in prepare.pending()          # never on the Ready list
    assert [q["label"] for q in entry["questions"]] == [WHY, CITIZEN, "Twitter handle"]  # the profile's, answered
    assert "6" not in prepare.load()                                                     # nothing for the person
    assert "Its form asks 3 questions nothing answers yet: 2 required, for you on the Questions page" in steps(1)
    assert item_of(1).note == "Made ready (tailored): exactly these files go with its form"
    shown = {i["job_id"]: i for i in execution.view(cfg)["items"]}
    assert (shown[1]["turn"], shown[1]["turn_reason"]) == (
        "answers", "2 required questions on its form (Questions page)")
    assert shown[6]["turn"] == "ready" and execution.view(cfg)["next_run"] == 1
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert list(went) == [6] and item_of(6).state == APPLIED            # the rest go on
    assert item_of(1).state == QUEUED and item_of(1).attempts == 1      # no new state; never taken
    assert last_run() == "Nothing more can go now: 1 wait for your answers to their forms' questions (Questions page)."


def test_the_questions_page_asks_them_once_and_the_answers_reach_its_form(db, desk, board, client, monkeypatch):
    cfg, _, _ = db
    c, _ = client
    monkeypatch.setattr("backend.api.app.applicant_path", lambda: PROJECT_ROOT / "made-up.yaml")
    monkeypatch.setattr("backend.apply.profile.load", lambda path: person())
    went = {}
    monkeypatch.setattr(runner, "fill_form", filled(went))
    execution.add([1, 6])
    runner.prepare_chosen(cfg)
    page = c.get("/apply/questions.json").get_json()
    job = next(j for j in page["jobs"] if j["job_id"] == 1)
    assert {q["label"] for q in job["questions"]} == {WHY, CITIZEN, "Twitter handle"} and job["required"] == 2
    saved = c.post("/apply/questions", json={"answers": [
        {"label": WHY, "value": "Its robots run the warehouses I have kept running.", "job_ids": [1]},
        {"label": CITIZEN, "value": "No", "job_ids": [1]}]}).get_json()
    assert saved == {"ok": True, "saved": 2}
    assert steps(1)[-1] == item_of(1).note == ("You answered 2 of its form's questions on the Questions page; "
                                               "nothing required is left: it goes when its turn comes")
    assert execution.view(cfg)["next_run"] == 2
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert went[1] == {WHY: "Its robots run the warehouses I have kept running.", CITIZEN: "No", "Twitter handle": None}
    assert item_of(1).state == APPLIED
    kept = prepare.load()["1"]
    assert kept["answers"] == {WHY: "Its robots run the warehouses I have kept running.", CITIZEN: "No"}


def test_a_sensitive_question_is_never_answered_by_a_guess(db, desk, board, monkeypatch):
    cfg, _, _ = db
    went = {}
    monkeypatch.setattr(runner, "fill_form", filled(went))
    execution.add([1])
    runner.prepare_chosen(cfg)
    prepare.save_answers(1, {WHY: "Its robots run the warehouses I have kept running."})
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert went == {} and item_of(1).state == QUEUED                    # citizenship unanswered: not sent
    assert [q["label"] for q in runner.questions_waiting(execution._job(1), person())] == [CITIZEN]
    # Stated on the profile, it is answered -- by the person's own words, not a guess.
    monkeypatch.setattr(runner, "load", lambda path: person(us_citizen=False))
    assert runner.questions_waiting(execution._job(1), person(us_citizen=False)) == []
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert went[1][CITIZEN] == "No" and item_of(1).state == APPLIED


def test_questions_on_a_form_made_ready_on_the_way_set_it_aside_and_the_run_goes_on(db, desk, board, monkeypatch):
    cfg, _, _ = db
    went = {}
    monkeypatch.setattr(runner, "fill_form", filled(went))
    execution.add([1, 6])
    runner.run(cfg, chosen=True, browser_factory=Context)               # no Prepare these first
    assert list(went) == [6] and item_of(1).state == QUEUED
    assert item_of(1).note.startswith("Waits for your answers: 2 required questions on its form, on the Questions page")
    prepare.save_answers(1, {WHY: "Its robots run the warehouses I have kept running.", CITIZEN: "No"})
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert item_of(1).state == APPLIED and desk.calls.count(1) == 1     # its documents were not made again


def test_a_question_left_unanswered_after_the_job_was_picked_holds_it_before_anything_is_made(db, desk, board, monkeypatch):
    """The run picks only jobs with their questions answered; were an answer taken back between the pick
    and the job's own check (the Questions page, mid-run), the check holds it -- nothing made, no form."""
    cfg, _, _ = db
    went = {}
    monkeypatch.setattr(runner, "fill_form", filled(went))
    prepare.keep_questions(execution._job(1), [{"label": WHY, "required": True, "options": []}])
    execution.add([1])
    item = item_of(1)
    run = execution.start_run(os.getpid())
    execution.move(item.id, VALIDATING, run_id=run.id)                 # picked while it was answered
    shown = execution._one(cfg, person(), run.id, item.id, runner.searched_places(cfg),
                           {"context": None, "close": lambda: None, "factory": Context}, tailor=False, wait_s=1)
    assert shown is None and item_of(1).state == QUEUED and went == {} and desk.calls == []
    assert item_of(1).note.startswith("Waits for your answers: 1 required question on its form")


def test_prepare_these_makes_its_documents_ready_while_its_answers_are_still_awaited(db, desk, board):
    cfg, _, _ = db
    prepare.keep_questions(execution._job(1), [{"label": WHY, "required": True, "options": []}])   # asked before
    execution.add([1])
    assert runner.prepare_chosen(cfg) == 1 and desk.calls == [1]                 # made ready all the same
    assert item_of(1).state == QUEUED and execution.view(cfg)["items"][0]["turn"] == "answers"


def test_a_form_that_cannot_be_read_ahead_goes_on_as_before(db, desk, board, monkeypatch):
    cfg, _, _ = db
    went = {}
    monkeypatch.setattr(runner, "fill_form", filled(went))

    def broken(url, timeout=None, **kw):
        raise ValueError("the board sent something that is not JSON")
    monkeypatch.setattr(prepare.requests, "get", broken)
    monkeypatch.setattr(prepare, "open_questions", lambda job, applicant: (_ for _ in ()).throw(KeyError("fields")))
    execution.add([1])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert item_of(1).state == APPLIED and 1 in went
    # Kept as not read, never as "asks nothing" (29 Oct 2026): it goes on, with Submit the person's.
    assert "Its form's questions could not be read ahead (KeyError): it goes on, and you press Submit on it" in steps(1)
    assert prepare.questions_unread(1) == "KeyError"


# -- kept ------------------------------------------------------------------------------------------

def test_the_answers_given_and_the_drafts_are_kept_when_the_form_is_read_again(db, desk, board):
    cfg, _, _ = db
    execution.add([1])
    runner.prepare_chosen(cfg)
    prepare.save_answers(1, {WHY: "Its robots run the warehouses I have kept running."})
    with prepare.changing():
        data = prepare.load()
        data["1"]["drafts"] = {"Twitter handle": {"text": "@dana_example"}}
        prepare._save(data)
    FORMS[1].append({"label": "Earliest start date?", "required": True, "fields": [{"name": "question_9", "type": "input_text"}]})
    try:
        prepare.keep_questions(execution._job(1), prepare.open_questions(execution._job(1), person()))
    finally:
        FORMS[1].pop()
    entry = prepare.load()["1"]
    assert entry["answers"] == {WHY: "Its robots run the warehouses I have kept running."}
    assert entry["drafts"] == {"Twitter handle": {"text": "@dana_example"}} and entry["state"] == prepare.CHOSEN
    assert [q["label"] for q in runner.questions_waiting(execution._job(1), person())] == [CITIZEN, "Earliest start date?"]


def test_a_job_on_the_ready_list_keeps_its_place_there(db, desk, board):
    with prepare.changing():
        prepare._save({"1": {"job_id": 1, "company": "Globex", "title": "Platform Engineer", "state": "ready",
                             "questions": [], "answers": {"Pronouns": "she/her"}}})
    prepare.keep_questions(execution._job(1), [{"label": WHY, "required": True, "options": []}])
    entry = prepare.load()["1"]
    assert entry["state"] == "ready" and entry["answers"] == {"Pronouns": "she/her"} and entry in prepare.pending()


def test_the_jobs_page_review_says_a_ticked_job_waits_for_answers(db, desk, board):
    cfg, _, _ = db
    prepare.keep_questions(execution._job(1), [{"label": WHY, "required": True, "options": []}])
    result = chosen.check(cfg, [1, 6])
    rows = {r["id"]: r for r in result["jobs"]}
    assert (rows[1]["state"], rows[1]["reason"]) == ("answers", "1 required question on its form (Questions page)")
    assert rows[6]["state"] == "ready" and result["answers"] == 1


# -- two writers at once ---------------------------------------------------------------------------

def test_answers_saved_while_a_run_keeps_questions_are_never_lost(db, desk, board):
    """The Questions page saving answers while a run keeps another job's questions: each reads the whole
    file and writes it back. Every answer and every job's questions must be there at the end, and a
    reader never sees half a file."""
    from data_engineering.db.models import Job
    jobs = {}
    with get_session() as s:
        for job in s.query(Job):
            s.expunge(job)
            jobs[job.id] = job
    for job_id in (1, 2, 3):
        prepare.keep_questions(jobs[job_id], [{"label": f"Q{job_id}", "required": True, "options": []}])
    errors = []

    def answering():
        for n in range(60):
            prepare.save_answers(1 + n % 3, {f"answer {n}": f"value {n}"})

    def keeping():
        for n in range(60):
            prepare.keep_questions(jobs[4 + n % 3], [{"label": f"K{n}", "required": False, "options": []}])

    def reading():
        for _ in range(300):
            try:
                json.loads(prepare.PREPARED.read_text())
            except ValueError as exc:
                errors.append(exc)
    threads = [threading.Thread(target=t) for t in (answering, keeping, reading)]
    for t in threads:
        t.start()
    # And a second process, as the run is: it keeps a job's questions over and over meanwhile.
    child = subprocess.run([sys.executable, "-c", f"""
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from pathlib import Path
from types import SimpleNamespace
from backend.apply import prepare
prepare.PREPARED = Path({str(prepare.PREPARED)!r})
job = SimpleNamespace(id=5, company="Hooli", title="Platform Engineer", source="workday", ats_match_score=0.75)
for n in range(60):
    prepare.keep_questions(job, [{{"label": f"P{{n}}", "required": False, "options": []}}])
"""], capture_output=True, text=True, timeout=120)
    for t in threads:
        t.join()
    assert child.returncode == 0, child.stderr[-2000:]
    data = prepare.load()
    given = {k for e in data.values() for k in (e.get("answers") or {})}
    assert given == {f"answer {n}" for n in range(60)} and not errors
    assert set(data) == {"1", "2", "3", "4", "5", "6"}


# -- not read is not "none asked" (29 Oct 2026) -----------------------------------------------------

@pytest.mark.parametrize("trouble", [
    lambda url, timeout=None, **kw: (_ for _ in ()).throw(prepare.requests.ConnectionError("no answer")),
    lambda url, timeout=None, **kw: type("Reply", (), {"json": lambda self: (_ for _ in ()).throw(ValueError("not JSON"))})(),
    lambda url, timeout=None, **kw: type("Reply", (), {"json": lambda self: ["not", "a", "form"]})(),
])
def test_a_greenhouse_board_that_does_not_answer_is_not_read_as_no_questions(db, desk, board, monkeypatch, trouble):
    monkeypatch.setattr(prepare.requests, "get", trouble)
    with pytest.raises(prepare.QuestionsUnread):
        prepare.open_questions(execution._job(6), person())


def test_a_greenhouse_form_that_asks_nothing_is_read_as_none(db, desk, board, monkeypatch):
    monkeypatch.setattr(prepare.requests, "get",
                        lambda url, timeout=None, **kw: type("Reply", (), {"json": lambda self: {"questions": []}})())
    assert prepare.open_questions(execution._job(6), person()) == []
    job = execution._job(6)
    job.external_id = ""
    with pytest.raises(prepare.QuestionsUnread, match="board address is incomplete"):
        prepare.open_questions(job, person())


def test_a_jazzhr_page_that_does_not_answer_is_not_read_as_no_questions(monkeypatch):
    from types import SimpleNamespace

    def no_answer(url, headers=None, timeout=None):
        raise prepare.requests.Timeout("no answer")
    monkeypatch.setattr(prepare.requests, "get", no_answer)
    job = SimpleNamespace(id=1, source="jazzhr", application_url="https://example.applytojob.com/apply/x")
    with pytest.raises(prepare.QuestionsUnread, match="JazzHR page did not answer"):
        prepare.open_questions(job, person())
    with pytest.raises(prepare.QuestionsUnread, match="address is missing"):
        prepare.open_questions(SimpleNamespace(id=1, source="jazzhr", application_url=""), person())


def test_a_job_whose_questions_could_not_be_read_is_never_sent_by_the_app_itself(db, desk, board, monkeypatch):
    """Eligible, on Greenhouse's own board, the person's Submit-by-itself on: still theirs to send while its
    form's questions are unread; once the board answers, the app may send it."""
    cfg, _, _ = db
    from qa.tests.test_prepared_documents import dana
    me = dana(stop_before_submit=False)
    me.links = {"linkedin": "https://linkedin.com/in/dana-example"}
    monkeypatch.setattr(runner, "load", lambda path: me)
    went = []

    def fill_form(cfg, page, job, applicant, head, ready, *, auto, wait_s):
        went.append((job.id, auto))
        return runner.Outcome(job.id, job.company, job.title, "skipped")
    monkeypatch.setattr(runner, "fill_form", fill_form)
    answers = board.get
    desk.letters, desk.checked = False, True                              # its documents ready to go by themselves
    monkeypatch.setattr(prepare.requests, "get", lambda url, timeout=None, **kw: (_ for _ in ()).throw(
        prepare.requests.ConnectionError("no answer")))
    execution.add([6])
    runner.run(cfg, chosen=True, browser_factory=Context)
    assert went == [(6, False)] and prepare.questions_unread(6).startswith("Greenhouse's board did not answer")
    assert any(s.startswith("You press Submit on this one: its form's questions could not be read ahead")
               for s in steps(6))

    monkeypatch.setattr(prepare.requests, "get", answers)                 # the board answers again
    execution.retry(item_of(6).id)
    runner.run(cfg, chosen=True, browser_factory=Context)                 # read again before its form
    assert went[-1] == (6, True) and prepare.questions_unread(6) is None
