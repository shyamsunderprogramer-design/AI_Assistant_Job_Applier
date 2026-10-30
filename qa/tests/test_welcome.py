"""The Welcome page: a new person's first steps in order, and the search corrected without YAML."""

import pytest
import yaml

from backend.api import app as webapp
from backend.api import morning_pages, welcome_pages
from backend.config.loader import DEFAULT_CONFIG_PATH, Config
from data_engineering.db import session as session_mod
from data_engineering.db.session import init_engine


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A copy just installed: no resume, no search, no details, nothing fetched."""
    before = (session_mod._engine, session_mod._SessionFactory)
    init_engine("sqlite:///:memory:")
    data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text()) or {}
    (tmp_path / "resume").mkdir()
    data.setdefault("resume", {})["base_path"] = str(tmp_path / "resume")
    data.setdefault("filters", {})["profile_path"] = str(tmp_path / "search_profile.yaml")
    monkeypatch.setattr(webapp, "_cfg", Config(data))
    monkeypatch.setattr(webapp, "env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(webapp, "applicant_path", lambda: tmp_path / "applicant.yaml")
    for module in (webapp, morning_pages):
        monkeypatch.setattr(module, "DAILY_LOG", tmp_path / "daily_run.log")
    monkeypatch.setattr(morning_pages, "AUTO_OFF", tmp_path / "daily_auto_off")
    monkeypatch.setattr(morning_pages, "pipeline_pids", lambda: [])
    monkeypatch.setattr(morning_pages, "task_pid", lambda task: None)   # nor a fetch running on this Mac
    yield tmp_path, webapp.app.test_client()
    session_mod._engine, session_mod._SessionFactory = before


def with_resume(root):
    (root / "resume" / "base_resume.txt").write_text(
        "JANE DOE\nRegistered Nurse\nEXPERIENCE\nRegistered Nurse, Mercy Hospital, 2018-2026\n"
        "- Cared for 6 patients a shift on a medical-surgical unit\n")
    from ml.resume.profile import SearchProfile, save_profile
    save_profile(SearchProfile(titles=["registered nurse", "staff nurse"], exclude_titles=["director"],
                               locations=["chicago", "remote"], seniority="mid", years_experience=8,
                               families=["nursing"], derived_from="families"),
                 root / "search_profile.yaml")


def test_a_new_person_sees_the_first_step_and_nothing_can_run_yet(fresh):
    root, client = fresh
    page = client.get("/welcome").get_data(as_text=True)
    assert "Welcome to Job Applier" in page and 'id="w-upload"' in page
    assert 'class="step-tag"' not in page                              # nothing done yet
    assert "Upload your resume first." in page
    start = page[page.index('id="w-start"'):]
    assert "disabled" in start[:start.index(">")]                      # Find jobs waits for the resume
    assert 'id="start-here"' in client.get("/").get_data(as_text=True)  # the Jobs page points here
    refused = client.post("/run/morning/start")                        # and Start refuses, saying why
    assert refused.status_code == 409 and "no resume yet" in refused.get_json()["error"]


def test_with_a_resume_the_search_is_shown_and_can_be_corrected(fresh):
    root, client = fresh
    with_resume(root)
    page = client.get("/welcome").get_data(as_text=True)
    assert "registered nurse, staff nurse" in page and "chicago, remote" in page
    assert 'id="start-here"' not in client.get("/").get_data(as_text=True)
    start = page[page.index('id="w-start"'):]
    assert "disabled" not in start[:start.index(">")]

    reply = client.post("/search/save", json={
        "titles": "Registered Nurse\n  charge nurse \n\nregistered nurse\nICU nurse",
        "locations": "Chicago\nEvanston", "exclude_titles": "director\nmanager"}).get_json()
    assert reply["ok"] and reply["search"]["titles"] == ["Registered Nurse", "charge nurse", "ICU nurse"]
    saved = yaml.safe_load((root / "search_profile.yaml").read_text())
    assert saved["title_keywords"] == ["Registered Nurse", "charge nurse", "ICU nurse"]
    assert saved["location_keywords"] == ["Chicago", "Evanston"]
    assert saved["exclude_title_keywords"] == ["director", "manager"]
    assert saved["derived_from"] == "edited" and saved["families"] == ["nursing"]   # the rest is kept
    assert "charge nurse" in client.get("/welcome").get_data(as_text=True)

    assert client.post("/search/save", json={"titles": "  \n"}).status_code == 400   # never an empty search


def test_no_search_to_correct_before_a_resume(fresh):
    _, client = fresh
    assert client.post("/search/save", json={"titles": "nurse"}).status_code == 409


def test_terms_are_tidied_and_bounded():
    assert welcome_pages._terms("a\n a \nA\n\n b  c ") == ["a", "b c"]
    assert len(welcome_pages._terms("\n".join(f"t{i}" for i in range(500)))) == welcome_pages.MAX_TERMS
    assert welcome_pages._terms(["x" * 500]) == ["x" * welcome_pages.MAX_TERM_LEN]


def test_the_best_match_is_said_even_when_none_is_strong(fresh):
    """A one-page resume covers less of a posting than a long one: "0 strong matches" alone read
    as if nothing had been found."""
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session
    root, client = fresh
    with_resume(root)
    with get_session() as s:
        for i, score in enumerate((0.31, 0.47)):
            s.add(Job(source="lever", company="Mercy", company_slug="mercy", external_id=str(i), content_hash=str(i),
                      title="Registered Nurse", description="Patient care.", application_url="https://x",
                      is_open=True, ats_match_score=score))
    page = client.get("/welcome").get_data(as_text=True)
    assert "0 strong matches (60% or more) so far; your best match is 47%." in page
    assert client.get("/welcome/state").get_json()["jobs"] == {"open": 2, "strong": 0, "best": 47}
