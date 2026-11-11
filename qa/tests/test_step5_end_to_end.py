"""Step 5 of the Morning shortlist plan (7 Nov 2026): end to end, from the morning to the person's Submit.

One made-up morning, through the app's own pages and runs, in the order the person meets them:

   1. the morning picks its jobs, each with why;              6. Apply to these goes to exactly the
   2. eligibility shown, "Sponsorship unknown" labelled;         entries waiting at the press;
   3. Prepare pressed, documents made only then;              7. an employer's site: a check or a
   4. each file's fingerprint kept, and approved by the          Lever form goes to the person's own
      person before any form opens;                              browser;
   5. a form's required question waits for the person's       8. Submit pressed by the person only;
      answer, never guessed;                                  9. one record per application, history kept;
                                                             10. never sent by the app, with the person's
                                                                 setting at "send by itself" and no hold.

Made-up jobs in a database file of the test's own; the writer, the boards and the forms stood in for;
the runs called as their processes would be, with the arguments the page wrote down. No browser opens,
nothing reaches the network, and nothing is sent."""

import json

import pytest

from backend.apply import anyform, execution, greenhouse, prepare, runner, shortlist
from backend.apply.execution import APPLIED, PENDING, QUEUED
from data_engineering.db.models import ApplyArtifact, ApplyEvent
from data_engineering.db.session import get_session
from qa.tests.test_morning_picks_page import ON, client, jobs_db, now, picks_now  # noqa: F401
from qa.tests.test_morning_picks_prepare import writer  # noqa: F401
from qa.tests.test_morning_shortlist import Person

WHY = "Why do you want to work at Acme?"
LEVER = "https://jobs.lever.co/initech/2/apply"
WONKA = "https://wonka.example/jobs/4411-platform-engineer"


class Page:
    def is_closed(self):
        return False


class Context:
    def new_page(self):
        return Page()


@pytest.fixture
def started(monkeypatch):
    """The page's presses: the task each would start, with its arguments -- not started."""
    from backend.api import app as webapp
    pressed = []
    for module in ("backend.api.shortlist_pages", "backend.api.chosen_pages"):
        monkeypatch.setattr(f"{module}.start_task",
                            lambda task, args: pressed.append((task, args)) or webapp.jsonify(ok=True))
        monkeypatch.setattr(f"{module}.task_pid", lambda task: None)
    return pressed


def items_in(args):
    return [int(i) for i in args[args.index("--items") + 1].split(",")]


@pytest.fixture
def forms(monkeypatch):
    """The forms: Greenhouse's, a Lever form and an employer's own site, stood in for. On each the person
    presses Submit (or would, in their own browser); what the app was let to do is written down."""
    seen = {"greenhouse_submit": {}, "filled": []}

    def gh_fill(page, job_id, url, applicant, resume=None, letter=None, *, submit=False, evidence=None, **kw):
        seen["greenhouse_submit"][job_id] = submit
        seen["filled"].append(job_id)
        return greenhouse.FillResult(job_id=job_id, url=url, submitted=submit)
    monkeypatch.setattr(greenhouse, "fill", gh_fill)
    monkeypatch.setattr(greenhouse, "await_submission", lambda page, timeout_s, **kw: "submitted")
    monkeypatch.setattr(greenhouse, "challenge_shown", lambda page: False)
    monkeypatch.setattr(greenhouse, "watch_submission", lambda page, timeout_s, **kw: ("submitted", {}))  # the person's press

    def reach(page, url, **kw):
        if url == WONKA:                                   # the employer's site checks the browser: it never clears
            return anyform.Reached(page, None, url, "the site's browser check did not clear", "check")
        return anyform.Reached(page, "form", LEVER)
    monkeypatch.setattr(anyform, "reach", reach)

    def any_fill(page, job_id, url, applicant, *a, **kw):
        seen["filled"].append(job_id)
        return greenhouse.FillResult(job_id=job_id, url=url, filled={"name": "Dana"})
    monkeypatch.setattr(anyform, "fill", any_fill)
    opened = []
    monkeypatch.setattr(runner, "open_own_browser", lambda url: opened.append(url) or True)
    seen["own_browser"] = opened
    return seen


def stage(c, job_id):
    return next(p["stage"]["label"] for p in picks_now(c)["picks"] if p["job_id"] == job_id)


def item(c, job_id):
    return next(p["entry"] for p in picks_now(c)["picks"] if p["job_id"] == job_id)


def messages(item_id):
    with get_session() as s:
        return [e.message for e in s.query(ApplyEvent).filter(ApplyEvent.item_id == item_id).order_by(ApplyEvent.id)]


def test_a_morning_from_its_picks_to_the_persons_submit(client, writer, started, forms, monkeypatch):
    c, holder = client
    monkeypatch.setattr(Person, "stop_before_submit", False)           # the person's setting: send by itself
    assert not execution.auto_submit_held()                            # and no hold (the test's own file)

    # 1. the morning's picks, each with why
    report = shortlist.pick(ON, now=now())
    picks = [p["job_id"] for p in report["picked"]]
    assert picks == [1, 2, 4, 11, 12] and all(p["reasons"] for p in report["picked"])
    assert writer.made == [] and forms["filled"] == []                 # the morning makes and opens nothing

    # 2. eligibility, labelled
    first = picks_now(c)["picks"][0]
    assert shortlist.SPONSORSHIP_UNKNOWN in first["reasons"]
    assert first["entry"]["decision"]["decision"] == "ELIGIBLE_WITH_WARNINGS"
    assert {stage(c, j) for j in picks} == {"Selected"}

    # 3. Prepare, pressed: documents made only now, for the entries pressed for; one form asks a question
    def questions(job, applicant):
        return [{"label": WHY, "required": True, "options": []}] if job.id == 11 else []
    monkeypatch.setattr(prepare, "open_questions", questions)
    assert c.post("/apply/picks/prepare", json={}).get_json() == {"ok": True}
    (task, args), = started
    assert task == "prepare-chosen" and len(items_in(args)) == 5
    runner.prepare_chosen(ON, only=items_in(args))                      # what the started process does
    assert sorted(writer.made) == picks

    # 4. each file's fingerprint kept; nothing opens before the person's word on it
    with get_session() as s:
        docs = s.query(ApplyArtifact).filter(ApplyArtifact.state == "ready").all()
        fingerprints = {d.item_id: (d.id, d.sha256) for d in docs}
    assert len(fingerprints) == 5 and all(len(sha) == 64 for _, sha in fingerprints.values())
    assert stage(c, 1) == "Needs document approval" and stage(c, 11) == "Needs answers"
    for item_id, (doc, _) in fingerprints.items():
        r = c.post("/apply/chosen/document", json={"item": item_id, "document": doc, "decision": "approved"})
        assert r.get_json()["ok"] is True
    assert stage(c, 1) == "Ready for review"

    # 6. Apply to these: the entries waiting at the press, written down by the page
    assert c.post("/apply/chosen/start", json={}).get_json() == {"ok": True}
    task, args = started[-1]
    pressed = items_in(args)
    assert task == "apply-chosen" and len(pressed) == 5
    execution.add([13])                                                 # added after the press (the top-up, say)
    runner.run(ON, chosen=True, browser_factory=Context, only=pressed)

    # 8. Submit: the person's, on every form; 10. never the app's
    assert forms["greenhouse_submit"] == {1: False, 12: False}
    assert item(c, 1)["state"] == item(c, 12)["state"] == APPLIED
    with get_session() as s:
        from data_engineering.db.models import ApplyItem
        outcomes = {i.job_id: (i.state, i.outcome or "", i.attempts) for i in s.query(ApplyItem)}
    assert outcomes[1][1].startswith("Applied ✓ (you submitted it") and outcomes[12][1].startswith("Applied ✓ (you submitted it")
    assert not any("sent automatically" in o for _, o, _ in outcomes.values())

    # 7. the employer's site: Lever's form and a check that never clears go to the person's own browser
    assert outcomes[2][0] == outcomes[4][0] == PENDING
    assert stage(c, 2) == stage(c, 4) == "Ready for manual submission"
    assert forms["own_browser"] == [LEVER, WONKA]
    assert item(c, 4)["action"].startswith(runner.CHECK_HANDOFF)

    # 5. the question: never guessed -- the job waits, its form unopened, until the person answers
    assert outcomes[11][0] == QUEUED and 11 not in forms["filled"]
    # 6. the job added after the press was not gone to
    assert outcomes[13] == (QUEUED, "", 0)

    monkeypatch.setattr("backend.api.app.applicant_path", lambda: "made-up.yaml")
    monkeypatch.setattr("backend.apply.profile.load", lambda path: Person())
    saved = c.post("/apply/questions", json={"answers": [
        {"label": WHY, "value": "Its platform is the one I have kept running.", "job_ids": [11]}]}).get_json()
    assert saved["ok"] is True
    assert c.post("/apply/chosen/start", json={}).get_json() == {"ok": True}
    runner.run(ON, chosen=True, browser_factory=Context, only=items_in(started[-1][1]))
    assert forms["greenhouse_submit"][11] is False and item(c, 11)["state"] == APPLIED

    # 9. one record per application; each pick's history whole, in order
    sent = [json.loads(line)["job_id"] for line in runner.AUDIT_LOG.read_text().splitlines()]
    assert sorted(sent) == sorted(set(sent)) and set(sent) >= {1, 11, 12} and not set(sent) & {2, 4}
    said = messages(item(c, 1)["id"])
    assert said[0] == "Added to your chosen jobs" and said[1].startswith("Picked this morning:")
    assert any(m.startswith("Documents ready") or "made ready" in m.lower() for m in said)
    assert any(m.startswith("Documents ready: resume resume.pdf (checksum ") for m in said)      # its fingerprint
    assert any(m.startswith("You approved its resume resume.pdf (checksum ") for m in said)      # the person's word
    assert any(m.startswith("Checked before the form opens: resume.pdf (checksum ") for m in said)
    assert any(m.startswith("You press Submit on this one") for m in said)
    assert said[-1].startswith("Applied ✓ (you submitted it")
