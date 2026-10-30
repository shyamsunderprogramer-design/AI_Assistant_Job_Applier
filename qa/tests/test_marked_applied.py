"""A job marked applied by hand -- its status on the job page, the assistant, a stage on the Applied page --
gets a history line saying exactly that, in the same transaction (29 Oct 2026). Until then each could leave
a job Applied with no history at all. Nothing is invented: "marked", never a submission the app saw; no
documents, no run; the job's applied_at (when an application went out) is left as it was. A job with a
line already gets no second one. A temporary database and made-up jobs."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.apply import runner, tracker
from data_engineering.db import session as session_mod
from data_engineering.db.models import Job
from data_engineering.db.session import get_session, init_engine

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db(tmp_path, monkeypatch):
    before = (session_mod._engine, session_mod._SessionFactory)
    init_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    with get_session() as s:
        for n, company in ((1, "Example Robotics"), (2, "Sample Analytics"), (3, "Demo Logistics")):
            s.add(Job(id=n, source="greenhouse", company=company, company_slug=company.lower(), external_id=str(n),
                      title="Platform Engineer", location="Remote", description="x " * 40, requirements="",
                      application_url=f"https://example.com/{n}", content_hash=f"h{n}", is_open=True,
                      ats_match_score=0.9, score_basis="full", status="Not Applied", posted_at=NOW - timedelta(days=2)))
    monkeypatch.setattr(tracker, "STAGES_FILE", tmp_path / "stages.json")
    yield tmp_path
    session_mod._engine, session_mod._SessionFactory = before


def lines():
    return [json.loads(l) for l in runner.AUDIT_LOG.read_text().splitlines()] if runner.AUDIT_LOG.exists() else []


def job(job_id):
    with get_session() as s:
        found = s.get(Job, job_id)
        s.expunge(found)
        return found


@pytest.fixture
def client(db, monkeypatch):
    from backend.api import app as webapp
    monkeypatch.setattr(webapp, "cfg", lambda: {})
    return webapp.app.test_client()


def test_a_status_set_to_applied_by_hand_gets_one_marked_line_and_no_invented_date(client):
    assert client.post("/job/1/status", json={"status": "Applied"}).get_json()["ok"]
    assert [(l["job_id"], l["how"], l["stage"], l["marked_on"]) for l in lines()] == [
        (1, "marked", "Applied", "its status on the job page")]
    assert job(1).status == "Applied" and job(1).applied_at is None       # when it went out: not known
    assert lines()[0].get("history_event_id") and "resume" not in lines()[0]
    client.post("/job/1/status", json={"status": "Interviewing"})          # a later stage: no second line
    assert len(lines()) == 1 and job(1).status == "Interviewing"


def test_a_job_with_its_record_already_gets_no_marked_line(client):
    runner.record_application(2, {"company": "Sample Analytics", "title": "Platform Engineer", "how": "you"})
    client.post("/job/2/status", json={"status": "Applied"})
    assert [l["how"] for l in lines()] == ["you"]


def test_a_status_that_is_not_an_application_adds_no_line(client):
    client.post("/job/3/status", json={"status": "Skipped"})
    assert lines() == [] and job(3).status == "Skipped"


def test_the_assistant_and_a_stage_on_the_applied_page_say_where_they_were_marked(db):
    from backend.assistant import tools
    assert tools.run_direct({"kind": "status", "params": {"job_id": 1, "status": "Applied"}}) == "Marked Applied."
    tracker.add_stage(3, "recruiter_screen", on=NOW.date().isoformat())
    assert {l["job_id"]: l["marked_on"] for l in lines()} == {1: "the assistant", 3: "a stage on the Applied page"}
    assert tracker.application(1)["record"]["how"] == "marked"
