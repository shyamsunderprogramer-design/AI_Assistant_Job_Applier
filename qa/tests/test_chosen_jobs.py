"""Choosing jobs on the Jobs page: ticked, reviewed, added to Auto-apply, and applied to only on
"Apply to these" (24 Oct 2026: "select exactly the jobs I want, review their eligibility, and
transfer them into Auto Apply safely ... without accidentally submitting applications")."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.apply import chosen, runner
from data_engineering.db import session as session_mod
from data_engineering.db.models import Job
from data_engineering.db.session import get_session, init_engine

NOW = datetime.now(timezone.utc)
MANILA = "Taguig City, National Capital Region (Manila)"
# id: company, board, fit, days old, status, open, place, extra columns
ROWS = {
    1: ("Globex", "greenhouse", 0.95, 2, "Not Applied", True, "Remote", {}),
    2: ("Acme", "greenhouse", 0.55, 3, "Manual Review", True, "Austin, TX", {}),   # a weak fit, chosen anyway
    3: ("Initech", "jobvite", 0.80, 3, "Not Applied", True, "Remote", {}),
    4: ("Umbrella", "greenhouse", 0.90, 200, "Not Applied", True, "Remote", {}),
    5: ("Hooli", "greenhouse", 0.88, 5, "Applied", True, "Remote", {}),
    6: ("Soylent", "greenhouse", 0.85, 5, "Not Applied", False, "Remote", {}),
    7: ("Maxwell", "greenhouse", 0.93, 4, "Not Applied", True, MANILA, {}),
    8: ("Vandelay", "workday", 0.81, 1, "Not Applied", True, "Remote",
        {"clearance": "Top Secret", "clearance_active": True}),
    9: ("Globex", "greenhouse", 0.80, 1, "Not Applied", True, "Remote", {}),      # Globex's second job
    10: ("Wonka", "greenhouse", 0.84, 1, "Not Applied", True, "Remote", {}),
    11: ("Wonka", "greenhouse", 0.70, 9, "Applied", True, "Remote", {"applied_at": NOW - timedelta(hours=2)}),
    12: ("Massive Dynamic", "greenhouse", 0.99, 1, "Not Applied", True, "Remote", {}),  # never chosen
}


class Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class Person:
    max_per_day = 10
    stop_before_submit = False

    def __init__(self):
        self.authorisation = {"us_citizen": False}
        self.answers, self.job, self.job_answers = {}, None, {}


@pytest.fixture
def db(monkeypatch, tmp_path):
    before = (session_mod._engine, session_mod._SessionFactory)
    init_engine("sqlite:///:memory:")
    with get_session() as s:
        for n, (company, board, fit, days, status, open_, place, extra) in ROWS.items():
            s.add(Job(id=n, source=board, company=company, company_slug=company.lower(), external_id=str(n),
                      title="Platform Engineer", location=place, description="x " * 400, requirements="",
                      application_url=f"https://example.com/{n}", content_hash=f"h{n}", is_open=open_,
                      ats_match_score=fit, score_basis="full", status=status,
                      posted_at=NOW - timedelta(days=days), **extra))
    from data_engineering.scraper.filters import JobFilter
    from ml.resume.profile import NON_US_PLACES
    monkeypatch.setattr(runner, "searched_places", lambda cfg: JobFilter(exclude_location_keywords=list(NON_US_PLACES)))
    monkeypatch.setattr(runner, "load", lambda path: Person())
    monkeypatch.setattr(runner, "check_ready", lambda applicant: [])
    from backend.apply import prepare
    monkeypatch.setattr(prepare, "PREPARED", tmp_path / "prepared.json")   # never the person's own
    yield Cfg({"limits.max_applications_per_day": 15})
    session_mod._engine, session_mod._SessionFactory = before


def states(result):
    return {j["id"]: (j["state"], j["reason"]) for j in result["jobs"]}


# -- ticking -------------------------------------------------------------------------------

def test_ticked_jobs_are_kept_once_each_until_unticked_or_cleared(db):
    assert chosen.select([1])["ids"] == [1]                                       # one job
    reply = chosen.select([2, 3, 2, 1, 999])                                      # several, twice, unknown
    assert reply == {"ids": [1, 2, 3], "unknown": [999]}
    assert chosen.selected_ids() == [1, 2, 3]                                    # read back from the file
    assert json.loads(chosen.PATH.read_text())["selected"] == [1, 2, 3]
    assert chosen.unselect([2]) == [1, 3]
    assert chosen.select([4, 5, 6])["ids"] == [1, 3, 4, 5, 6]                    # "select all shown"
    assert chosen.unselect([4, 5, 6]) == [1, 3]                                  # and deselect them again
    assert chosen.clear_selection() == [] and chosen.selected_ids() == []


def test_a_damaged_file_is_read_as_empty_not_as_a_crash(db):
    chosen.PATH.parent.mkdir(parents=True, exist_ok=True)
    chosen.PATH.write_text('{"selected": [1, "2", true, -4, 1], "queue": [{"job_id": 3}, {"job_id": 3}, 7]}')
    data = chosen.load()
    assert data["selected"] == [1] and [e["job_id"] for e in data["queue"]] == [3]


@pytest.mark.parametrize("body", [
    {"add": "5"}, {"add": [1.5]}, {"add": [True]}, {"add": [-3]}, {"add": [0]}, {"add": ["7"]},
    {}, {"add": [1], "remove": [2]}, {"clear": "yes"}, {"remove": None},
])
def test_anything_but_job_numbers_is_refused(db, monkeypatch, body):
    from backend.api import app as webapp
    # Both names: the routes' module holds its own. Unpatched, it loaded the real config and .env
    # into the test run, and ten provider tests after this file failed (24 Oct 2026).
    monkeypatch.setattr(webapp, "cfg", lambda: db)
    monkeypatch.setattr("backend.api.chosen_pages.cfg", lambda: db)
    reply = webapp.app.test_client().post("/selection", json=body)
    assert reply.status_code == 400 and reply.get_json()["ok"] is False
    assert chosen.selected_ids() == []


def test_too_many_job_numbers_at_once_are_refused():
    with pytest.raises(chosen.NotChosen, match="at most"):
        chosen.ids(list(range(1, chosen.MAX_IDS + 2)))


# -- the review ----------------------------------------------------------------------------

def test_the_review_says_for_each_job_whether_it_goes_and_why_not(db):
    result = chosen.check(db, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 999])
    s = states(result)
    assert s[1] == ("ready", "") and s[2] == ("ready", "")             # Acme: chosen at a 55% fit
    # Jobvite has no filler of its own: chosen, it goes all the same, assisted (26 Oct 2026).
    assert s[3] == ("ready", "") and next(j for j in result["jobs"] if j["id"] == 3)["assisted"] == (
        "Assisted: the app opens the employer's form from jobvite and fills what it can; you finish it and press Submit")
    assert s[4] == ("blocked", "posted 200 days ago, over the 4-month limit")
    assert s[5] == ("blocked", "it is marked Applied")
    assert s[6] == ("blocked", "the posting has closed")
    assert s[7] == ("blocked", f"location '{MANILA}' is not in the US")
    assert s[8] == ("blocked", "needs an active Top Secret clearance, which only US citizens hold (from your profile)")
    assert s[9][0] == "later" and s[9][1].startswith("another of your chosen jobs at Globex goes first")
    assert s[10][0] == "later" and s[10][1].startswith("you applied to Wonka in the last 12 hours: it can go after")
    assert s[999] == ("blocked", "this job is no longer in your jobs list")
    # Ready first in the order the run takes them (best fit), then later, then blocked.
    assert [j["id"] for j in result["jobs"]][:5] == [1, 3, 2, 9, 10]
    assert (result["ready"], result["later"], result["blocked"]) == (3, 2, 6)
    assert result["auto_submit"] is True and result["details_missing"] is None


def test_todays_limit_decides_when_a_job_goes_not_whether(db, monkeypatch):
    class OneADay(Person):
        max_per_day = 1
    monkeypatch.setattr(runner, "load", lambda path: OneADay())
    with get_session() as s:                       # nothing sent today: the one a day is still free
        s.get(Job, 11).applied_at = NOW - timedelta(days=3)
    s = states(chosen.check(db, [2, 1]))
    assert s[1] == ("ready", "")                                       # the better fit goes first
    assert s[2] == ("later", "over today's limit (1 left today): it goes on a later run")


def test_unfinished_details_are_said_and_rule_nothing_out(db, monkeypatch):
    from backend.apply.profile import ProfileIncomplete

    def missing(path):
        raise ProfileIncomplete("No applicant profile yet")
    monkeypatch.setattr(runner, "load", missing)
    result = chosen.check(db, [1])
    assert result["details_missing"] == "No applicant profile yet" and states(result)[1] == ("ready", "")


# The review beside the Auto-apply list (25 Oct 2026). Since the list is kept in the database, a
# ticked job can meet one already waiting there: the review said "goes on the next run" of a job
# whose company had a better one waiting, and of one on the list already, even one that may have gone.

def test_the_review_counts_the_jobs_already_waiting_on_the_auto_apply_list(db):
    from backend.apply import execution
    execution.add([1])                                                 # Globex 95% waits on the list
    chosen.select([9, 2])                                              # Globex 80% ticked, and Acme
    s = states(chosen.review(db))
    assert s[9][0] == "later" and s[9][1].startswith("another of your chosen jobs at Globex goes first")
    assert s[2] == ("ready", "")


def test_todays_limit_counts_the_jobs_already_waiting_on_the_list(db, monkeypatch):
    from backend.apply import execution

    class OneADay(Person):
        max_per_day = 1
    monkeypatch.setattr(runner, "load", lambda path: OneADay())
    with get_session() as s:                       # nothing sent today: the one a day is still free
        s.get(Job, 11).applied_at = NOW - timedelta(days=3)
    execution.add([1])                                                 # and it is taken by Globex
    chosen.select([2])
    assert states(chosen.review(db))[2] == ("later", "over today's limit (1 left today): it goes on a later run")


def test_a_ticked_job_on_the_list_already_says_so_and_adding_it_adds_nothing(db):
    from backend.apply import execution
    from data_engineering.db.models import ApplyItem
    execution.add([12, 2])
    with get_session() as s:
        item_id = s.query(ApplyItem.id).filter(ApplyItem.job_id == 12).scalar()
    for state in (execution.VALIDATING, execution.PREPARING, execution.READY, execution.RUNNING, execution.PENDING):
        execution.move(item_id, state)                                 # its form was open: it may have gone
    chosen.select([12, 2, 6])
    result = chosen.review(db)
    assert states(result) == {12: ("listed", "on your Auto-apply list already (pending confirmation)"),
                              2: ("listed", "on your Auto-apply list already (queued)"),
                              6: ("blocked", "the posting has closed")}
    assert [j["id"] for j in result["jobs"]] == [12, 2, 6]
    assert (result["ready"], result["later"], result["listed"], result["blocked"]) == (0, 0, 2, 1)
    moved = chosen.transfer(db)
    assert (moved["added"], moved["already"]) == (0, 2) and chosen.selected_ids() == [6]
    assert sorted(execution.live_job_ids(execution.LIVE)) == [2, 12]   # still once each


# -- adding to Auto-apply --------------------------------------------------------------------

def test_adding_moves_the_jobs_that_can_go_and_leaves_the_rest_ticked_with_the_reason(db):
    from backend.apply import execution
    chosen.select([1, 3, 9, 5, 6])
    moved = chosen.transfer(db)
    assert moved["added"] == 3 and moved["already"] == 0 and moved["queued"] == 3
    assert execution.live_job_ids() == [1, 3, 9]                       # Globex's second job goes later
    assert moved["selected"] == chosen.selected_ids() == [5, 6]        # the two that cannot go
    # Two reasons, as before Jobvite jobs could go (26 Oct 2026): Soylent's posting has closed.
    assert {j["id"]: j["reason"] for j in moved["kept"]} == {5: "it is marked Applied", 6: "the posting has closed"}
    chosen.select([1, 2])                                              # 1 is on the list already
    again = chosen.transfer(db)
    assert again["added"] == 1 and again["already"] == 1 and execution.live_job_ids() == [1, 3, 9, 2]
    assert execution.cancel_waiting() == 4 and execution.live_job_ids() == []


def test_the_run_applies_to_exactly_the_chosen_jobs_never_to_the_apps_own_picks(db):
    queue, left = runner.candidates(db, Person(), 10, chosen_ids=[2, 1, 9, 3])
    sent_today = int(NOW - timedelta(hours=2) >= runner.start_of_today_utc())   # Wonka's, unless before 2 AM
    assert [c.job_id for c in queue] == [1, 3, 2] and left == 10 - sent_today  # not 12, the best fit of all
    assert {c.group_name for c in queue} == {"chosen by you"}
    assert [c.job_id for c in runner.candidates(db, Person(), 10)[0]][:1] == [12]   # Apply to next 10 would


# -- nothing is sent until Apply to these -----------------------------------------------------

@pytest.fixture
def client(db, monkeypatch):
    from backend.api import app as webapp
    monkeypatch.setattr(webapp, "cfg", lambda: db)
    monkeypatch.setattr("backend.api.chosen_pages.cfg", lambda: db)      # its own name for it too
    started = []
    monkeypatch.setattr(webapp, "start_task", lambda task, args: started.append((task, args)) or webapp.jsonify(ok=True))
    monkeypatch.setattr("backend.api.chosen_pages.start_task",
                        lambda task, args: started.append((task, args)) or webapp.jsonify(ok=True))
    monkeypatch.setattr("backend.api.chosen_pages.task_pid", lambda task: None)
    return webapp.app.test_client(), started


def test_ticking_reviewing_and_adding_never_start_a_run(client):
    c, started = client
    assert c.post("/selection", json={"add": [1, 5]}).get_json()["ids"] == [1, 5]
    review = c.get("/selection/review").get_json()
    assert review["ok"] and review["ready"] == 1 and review["blocked"] == 1 and review["queued"] == 0
    assert review["listed"] == 0 and review["later"] == 0
    assert c.post("/selection/transfer").get_json()["added"] == 1
    assert c.get("/selection").get_json()["ids"] == [5]
    listed = c.get("/apply/chosen").get_json()
    assert [(i["job_id"], i["state"]) for i in listed["items"]] == [(1, "queued")] and listed["running"] is False
    assert c.post("/apply/chosen/cancel", json={"item": "x"}).status_code == 400
    assert started == []                                               # nothing opened, nothing sent


def test_apply_to_these_starts_one_run_of_the_chosen_jobs_and_is_refused_when_it_cannot(db, client, monkeypatch):
    c, started = client
    assert c.post("/apply/chosen/start").status_code == 409            # nothing chosen yet
    chosen.select([1])
    chosen.transfer(db)
    monkeypatch.setattr("backend.api.chosen_pages.task_pid", lambda task: 4242 if task == "apply" else None)
    r = c.post("/apply/chosen/start")
    assert r.status_code == 409 and "already" in r.get_json()["error"] and started == []
    monkeypatch.setattr("backend.api.chosen_pages.task_pid", lambda task: None)
    assert c.post("/apply/chosen/start").get_json() == {"ok": True}
    assert started == [("apply-chosen", ["apply", "--chosen"])]


def test_no_two_apply_runs_share_the_browser_whichever_button_starts_them(client, monkeypatch):
    from backend.api import app as webapp
    c, started = client
    monkeypatch.setattr(webapp, "task_pid", lambda task: 4242 if task == "apply-chosen" else None)
    r = c.post("/run/apply-ready")
    assert r.status_code == 409 and "already" in r.get_json()["error"] and started == []


def test_an_applied_job_is_applied_on_the_list_and_a_closed_form_can_be_tried_again(db, monkeypatch, tmp_path):
    from backend.apply import execution
    chosen.select([1, 2])
    chosen.transfer(db)

    class Page:
        def is_closed(self):
            return False

        def close(self):
            pass

    class Context:
        def new_page(self):
            return Page()

    results = {1: "submitted", 2: "skipped"}
    (tmp_path / "r.pdf").write_bytes(b"%PDF-1.4 made-up resume")
    monkeypatch.setattr(runner, "prepare_documents", lambda cfg, job, applicant, head, *, tailor:
                        runner.Prepared(tmp_path / "r.pdf", None, "tailored", tmp_path))
    monkeypatch.setattr(runner, "fill_form", lambda cfg, page, job, applicant, head, ready, **kw:
                        runner.Outcome(job.id, job.company, job.title, results[job.id]))
    monkeypatch.setattr(runner, "say", lambda line: None)
    outcomes = runner.run(db, chosen=True, browser_factory=Context)
    assert [(o.job_id, o.result) for o in outcomes] == [(1, "submitted"), (2, "skipped")]
    view = {i["job_id"]: i for i in execution.view(db)["items"]}
    assert view[1]["state"] == "applied" and view[2]["state"] == "skipped" and view[2]["can"]["retry"]


def test_an_empty_list_opens_no_browser(db, monkeypatch):
    said = []
    monkeypatch.setattr(runner, "say", said.append)
    assert runner.run(db, chosen=True, browser_factory=lambda: pytest.fail("browser opened")) == []
    assert said[-2:] == ["Done: nothing more on your list.", "Done: 0 submitted, 0 skipped, 0 not sent."]
