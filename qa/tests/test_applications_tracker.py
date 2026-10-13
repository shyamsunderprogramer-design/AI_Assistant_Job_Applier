"""Every application and its stages: the posting, the day, what went, and what happened next."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.apply import tracker
from data_engineering.db import session as session_mod
from data_engineering.db.models import Job
from data_engineering.db.session import get_session, init_engine


@pytest.fixture
def db(monkeypatch, tmp_path):
    init_engine("sqlite:///:memory:")
    monkeypatch.setattr(tracker, "STAGES_FILE", tmp_path / "stages.json")
    monkeypatch.setattr(tracker, "RUNS_DIR", tmp_path / "runs")
    from backend.apply import runner
    monkeypatch.setattr(runner, "AUDIT_LOG", tmp_path / "applications.jsonl")
    evidence = tmp_path / "runs" / "7-x"
    evidence.mkdir(parents=True)
    (evidence / "resume.pdf").write_bytes(b"%PDF")
    applied = datetime.now(timezone.utc) - timedelta(days=9)
    with get_session() as s:
        s.add(Job(id=7, source="jazzhr", company="Acme", company_slug="acme", external_id="x", content_hash="h1",
                  title="DevOps Engineer", status="Applied", applied_at=applied, is_open=True,
                  description="Run CI/CD.", application_url="https://a/apply/x"))
        s.add(Job(id=8, source="greenhouse", company="Globex", company_slug="globex", external_id="y",
                  content_hash="h2", title="SRE", status="Not Applied", is_open=True, application_url="https://b"))
    (tmp_path / "applications.jsonl").write_text(json.dumps({
        "at": applied.isoformat(), "job_id": 7, "url": "https://a/apply/x", "how": "you", "resume_source": "tailored",
        "filled": {"resumator-email-value": "j@x.com", "Are you a US citizen?": "No"}, "evidence": str(evidence)}) + "\n")
    yield tmp_path
    session_mod._engine = None
    session_mod._SessionFactory = None


def test_applications_are_listed_with_where_they_stand(db):
    items = tracker.applications()
    assert [a["job_id"] for a in items] == [7]
    assert items[0]["stage"] == "Applied" and items[0]["days"] == 9 and items[0]["through_app"]


def test_one_application_holds_the_posting_what_went_and_a_follow_up_nudge(db):
    a = tracker.application(7)
    assert a["description"] == "Run CI/CD." and a["record"]["files"] == ["resume.pdf"]
    assert a["record"]["answers"]["Are you a US citizen?"] == "No"
    assert a["follow_up"] is True                     # nine days, no reply recorded


def test_stages_move_the_jobs_status_and_can_be_taken_back(db):
    today = datetime.now().date().isoformat()
    a = tracker.add_stage(7, "recruiter_screen", today, "Talked to Sam")
    assert a["current"] == "Recruiter screen" and a["status"] == "Interviewing" and not a["follow_up"]
    a = tracker.add_stage(7, "offer", today)
    assert a["status"] == "Offer"
    a = tracker.remove_stage(7, 1)
    assert a["status"] == "Interviewing" and [s["stage"] for s in a["stages"]] == ["recruiter_screen"]
    with get_session() as s:
        assert s.get(Job, 7).status == "Interviewing"


def test_a_stage_cannot_be_in_the_future_or_before_applying(db):
    with pytest.raises(ValueError):
        tracker.add_stage(7, "heard_back", "2999-01-01")
    with pytest.raises(ValueError):
        tracker.add_stage(7, "heard_back", "2000-01-01")
    with pytest.raises(ValueError):
        tracker.add_stage(7, "teleported")


def test_files_come_only_from_the_applications_own_folder(db):
    assert tracker.evidence_file(7, "resume.pdf").name == "resume.pdf"
    assert tracker.evidence_file(7, "../../etc/passwd") is None and tracker.evidence_file(8, "resume.pdf") is None


def test_the_pages_load(db, monkeypatch):
    from backend.api import app as webapp
    c = webapp.app.test_client()
    assert b"DevOps Engineer" in c.get("/applications").data
    page = c.get("/applications/7")
    assert page.status_code == 200 and b"What happened next" in page.data
    r = c.post("/applications/7/stage", json={"stage": "heard_back"}).get_json()
    assert r["ok"] and r["application"]["current"] == "Heard back"
    assert c.get("/applications/7/file/resume.pdf").status_code == 200
    assert c.get("/applications/999").status_code == 404
