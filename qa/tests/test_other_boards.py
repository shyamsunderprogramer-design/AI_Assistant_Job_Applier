"""Auto-apply Phase 3, step 4 (26 Oct 2026): other boards, assisted. A chosen job on a board with no
filler of its own (Jobvite, Lever, ...) goes on a run: the app follows the posting to the employer's
form and fills what it can, and the person sends it -- never the app, even when Submit by itself is on
and the posting leads to a Greenhouse form; a Lever form is handed to the person's own browser; a form
that cannot be reached says why. Every other run keeps its rule: Apply to next 10 still leaves such a
board to the job's own page. Made-up jobs, a fake form and a fake follower: no browser or network."""

import pytest

from backend.apply import anyform, chosen, execution, greenhouse, runner
from backend.apply.execution import APPLIED, FAILED, PENDING
from data_engineering.db.models import Job
from data_engineering.db.session import get_session
from qa.tests.test_execution_queue import client, db, item_of  # noqa: F401
from qa.tests.test_prepared_documents import Form, dana, desk  # noqa: F401

BOARD_WORDS = ("a run fills Greenhouse, Workday and JazzHR forms, and this one is on jobvite: "
               "press Auto-apply on its page")


class Browser:
    """The app's browser: a made-up form for each job, kept to see what each one was given and pressed."""
    forms: list = []

    def new_page(self):
        Browser.forms.append(Form())
        return Browser.forms[-1]


@pytest.fixture
def follow(db, desk, monkeypatch):
    """The posting followed to its form, made up: where each job's posting leads (anyform.reach)."""
    leads = {}

    def reach(page, url, *, say=print, check_s=180, hops=5):
        job_id = int(url.rstrip("/").split("/")[-1])
        kind, there, error = leads.get(job_id, ("form", url, None))
        return anyform.Reached(page, kind, there, error)
    monkeypatch.setattr(anyform, "reach", reach)
    monkeypatch.setattr(greenhouse, "watch_submission", lambda page, timeout_s, **kw: ("submitted", {}))
    monkeypatch.setattr(greenhouse, "await_submission", lambda page, timeout_s, **kw: "submitted")
    opened = []
    monkeypatch.setattr(runner, "open_own_browser", lambda url: opened.append(url) or True)
    # Each made-up board answers that its form asks nothing beyond the profile. Until 29 Oct 2026 the blocked
    # network did, silently: a board that did not answer read as one that asks nothing.
    from backend.apply import prepare
    monkeypatch.setattr(prepare, "open_questions", lambda job, applicant: [])
    Browser.forms = []
    return leads, opened


def on_board(job_id, board, url=None):
    with get_session() as s:
        job = s.get(Job, job_id)
        job.source = board
        job.application_url = url or job.application_url


# -- assisted ----------------------------------------------------------------------------------------

def test_another_boards_form_is_filled_and_left_to_the_person_even_when_it_leads_to_greenhouse(db, desk, follow, monkeypatch):
    """Submit by itself on, and both jobs Eligible: Greenhouse's own job is sent by the app; the
    Jobvite job, whose posting leads to a Greenhouse form, is filled and waits for the person."""
    cfg, _, _ = db
    leads, _ = follow
    leads[3] = ("greenhouse", "https://boards.greenhouse.io/initech/jobs/3", None)
    monkeypatch.setattr(runner, "load", lambda path: dana(stop_before_submit=False))
    desk.letters, desk.checked = False, True              # documents ready to go by themselves (29 Oct 2026)
    execution.add([3, 6])
    runner.run(cfg, chosen=True, browser_factory=Browser)
    pressed = {f.files["input#resume"].split("_Resume_")[1].split(".")[0]: f.clicked for f in Browser.forms}
    assert pressed == {"Initech": False, "Massive_Dynamic": True}      # the app pressed Submit on one only
    assert item_of(3).state == item_of(6).state == APPLIED
    assert item_of(3).outcome.startswith("Applied ✓ (you submitted it") and item_of(6).outcome.startswith(
        "Applied ✓ (sent automatically")
    steps3 = execution.events_after(0, item_id=item_of(3).id)
    assert any(e["message"] == runner.assisted(execution._job(3)) for e in steps3)
    assert any(e["message"] == "It leads to a Greenhouse form." for e in steps3)


def test_lever_is_filled_then_handed_to_the_persons_own_browser(db, desk, follow, monkeypatch):
    cfg, _, _ = db
    leads, opened = follow
    on_board(3, "lever", "https://jobs.lever.co/initech/3")
    leads[3] = ("form", "https://jobs.lever.co/initech/3/apply", None)

    def filled(page, job_id, url, applicant, resume_path=None, letter_path=None, **kw):
        result = greenhouse.FillResult(job_id=job_id, url=url, filled={"name": "Dana Example"})
        result.attachments.append(f"resume={resume_path.name}")
        return result
    monkeypatch.setattr(anyform, "fill", filled)
    execution.add([3])
    assert runner.assisted(execution._job(3)).startswith("Assisted: the app fills its form, then you send it from your own browser")
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert item_of(3).state == PENDING and "your own Chrome" in item_of(3).action
    assert opened == ["https://jobs.lever.co/initech/3/apply"]          # once the app's window is closed
    assert anyform.take_unconfirmed(3)["handoff"] is True               # the job page lists every answer
    execution.confirm(item_of(3).id, went=True)
    assert item_of(3).state == APPLIED


def test_a_form_that_cannot_be_reached_says_why(db, desk, follow):
    cfg, _, _ = db
    leads, _ = follow
    leads[3] = (None, "https://example.com/3", "could not open the posting (net::ERR_NAME_NOT_RESOLVED)")
    execution.add([3])
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert item_of(3).state == FAILED
    assert item_of(3).outcome == ("Not sent: could not open the posting (net::ERR_NAME_NOT_RESOLVED). "
                                  "Retry puts it back in the queue.")


@pytest.mark.parametrize("blocked_during_watch", [False, True])
def test_signin_barrier_hands_off_original_posting_without_recording_applied(
        db, desk, follow, monkeypatch, blocked_during_watch):
    cfg, _, _ = db
    leads, opened = follow
    posting = "https://posting.example/3"
    on_board(3, "jobicy", posting)
    leads[3] = ("form" if blocked_during_watch else "signin-handoff",
                "https://login.example/oauth?code=never-keep-this", None)
    monkeypatch.setattr(anyform, "sign_in_barrier", lambda page: "Sign-in was blocked in the app's browser.")

    def fill(page, job_id, url, *args, **kwargs):
        assert blocked_during_watch, "A recognized sign-in page must never be filled"
        return greenhouse.FillResult(job_id=job_id, url=url)

    monkeypatch.setattr(anyform, "fill", fill)
    monkeypatch.setattr(anyform, "show_submit", lambda *a, **kw: False)
    monkeypatch.setattr(anyform, "watch", lambda page, *a, **kw: ("signin-handoff", {}, page))
    execution.add([3])
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert item_of(3).state == PENDING
    assert opened == [posting]
    kept = anyform.unconfirmed(3)
    assert kept["url"] == posting and kept["handoff"] is True
    assert "never-keep-this" not in str(kept)
    assert not runner.AUDIT_LOG.exists()


def test_the_list_and_the_review_say_which_jobs_are_assisted(db, desk, client):
    cfg, _, _ = db
    c, _ = client
    execution.add([3, 1])
    items = {i["job_id"]: i for i in c.get("/apply/chosen").get_json()["items"]}
    assert items[3]["assisted"].startswith("Assisted: the app opens the employer's form from jobvite")
    assert items[1]["assisted"] == "" and items[3]["turn"] == "ready"
    rows = {r["id"]: r for r in chosen.check(cfg, [3, 5])["jobs"]}
    assert rows[3]["state"] == "ready" and rows[3]["assisted"]
    assert rows[5]["state"] == "ready" and rows[5]["assisted"] == ""          # Workday: its own filler


# -- every other run keeps its rule -----------------------------------------------------------------

def test_apply_to_next_10_still_leaves_another_board_to_the_jobs_own_page(db):
    cfg, _, _ = db
    job = execution._job(3)
    person = runner.load(None)
    assert runner.blocked_reason(cfg, job, person) == BOARD_WORDS                 # its saved-job note
    assert runner.blocked_reason(cfg, job, person, any_board=True) is None        # chosen: assisted
    queue, _ = runner.candidates(cfg, person, 10)                                 # its own picks
    assert 3 not in [c.job_id for c in queue] and {c.job_id for c in queue} <= {1, 2, 4, 5, 6}


def test_greenhouse_workday_and_jazzhr_go_as_they_always_have(db, desk, follow, monkeypatch):
    """Their own fillers, never the follower: Workday and JazzHR wait for the person whatever the
    setting; Greenhouse's own job may be sent by the app (when Eligible)."""
    cfg, _, _ = db
    went = []

    def fill_form(cfg, page, job, applicant, head, ready, *, auto, wait_s):
        went.append((job.source, auto))
        runner.record_application(job.id, {"company": job.company, "title": job.title, "how": "you"})
        return runner.Outcome(job.id, job.company, job.title, "submitted", how="you")
    monkeypatch.setattr(runner, "fill_form", fill_form)
    monkeypatch.setattr(runner, "load", lambda path: dana(stop_before_submit=False))
    desk.letters, desk.checked = False, True              # documents ready to go by themselves (29 Oct 2026)
    execution.add([2, 5, 6, 3])
    runner.run(cfg, chosen=True, browser_factory=Browser)
    assert sorted(went) == [("greenhouse", True), ("jazzhr", False), ("jobvite", False), ("workday", False)]
