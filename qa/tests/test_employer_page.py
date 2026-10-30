"""The company's own page for a job, added on the job's page (25 Oct 2026: "i want to add career website
or job posting page"). This job's own posting on the company's site is applied on, instead of the job
site the posting came through; a careers site is kept as a link to look from and never taken for the
form. Neither a fetch of the postings nor the morning's cloud merge overwrites them. A temporary
database and made-up jobs: no page is opened, nothing is sent."""

import sqlite3

import pytest
import yaml

from backend.api import app as webapp
from backend.apply import runner
from backend.config.loader import DEFAULT_CONFIG_PATH, Config
from backend.core import joblinks
from data_engineering.db import session as session_mod
from data_engineering.db.models import Job
from data_engineering.db.session import get_session, init_engine
from data_engineering.scraper.base import RawJob

ADZUNA = "https://www.adzuna.com/details/5512"
ORACLE = "https://careers.oracle.com/jobs/#en/sites/jobsearch/job/348662"     # this job's own posting
ORACLE_SITE = "https://careers.oracle.com/jobs/#en/sites/jobsearch"           # the careers site's search


@pytest.fixture
def c(monkeypatch, tmp_path):
    before = (session_mod._engine, session_mod._SessionFactory)
    init_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    monkeypatch.setattr(webapp, "_cfg", Config(yaml.safe_load(DEFAULT_CONFIG_PATH.read_text()) or {}))
    with get_session() as s:
        s.add(Job(id=7, source="adzuna", company="Oracle", company_slug="oracle", external_id="5512",
                  content_hash="h7", title="Principal Site Reliability Engineer", description="We run the cloud.",
                  application_url=ADZUNA))
        s.add(Job(id=8, source="greenhouse", company="Globex", company_slug="globex", external_id="8",
                  content_hash="h8", title="Platform Engineer", description="We build.",
                  application_url="https://boards.greenhouse.io/globex/jobs/8"))
    yield webapp.app.test_client()
    session_mod._engine, session_mod._SessionFactory = before


def saved(job_id):
    with get_session() as s:
        job = s.get(Job, job_id)
        return job.employer_url, job.employer_url_kind, job.application_url


# -- the job's page: add, change, take off ----------------------------------------------------------

def test_this_jobs_own_posting_is_applied_on(c):
    page = c.get("/job/7").get_data(as_text=True)
    assert 'id="ep-edit"' in page and "not added" in page and "instead of adzuna" in page
    assert c.post("/job/7/employer-page", json={"url": ORACLE}).get_json() == \
        {"ok": True, "url": ORACLE, "kind": "posting", "apply": ORACLE}
    page = c.get("/job/7").get_data(as_text=True)
    assert "careers.oracle.com" in page and "(this job's posting, added by you)" in page
    assert f'href="{ORACLE}" target="_blank" rel="noopener">Open the application form' in page
    assert "Open Oracle&rsquo;s page" in page
    with get_session() as s:
        assert webapp.job_row(s.get(Job, 7))["url"] == ORACLE          # the Jobs page links there too
    assert saved(7) == (ORACLE, "posting", ADZUNA)                     # the job site's link kept beside it


def test_a_careers_site_is_kept_to_look_from_and_never_taken_for_the_form(c):
    assert c.post("/job/7/employer-page", json={"url": ORACLE_SITE}).get_json() == \
        {"ok": True, "url": ORACLE_SITE, "kind": "careers", "apply": ADZUNA}
    page = c.get("/job/7").get_data(as_text=True)
    assert "(careers site, added by you: Auto-apply uses the adzuna link)" in page
    assert f'href="{ADZUNA}" target="_blank" rel="noopener">Open the application form' in page
    assert "Open the Adzuna form" in page
    # The person knows better: it opens this very job.
    assert c.post("/job/7/employer-page", json={"url": ORACLE_SITE, "kind": "posting"}).get_json()["apply"] \
        == ORACLE_SITE
    assert c.post("/job/7/employer-page", json={"url": ORACLE, "kind": "careers"}).get_json()["apply"] == ADZUNA


def test_a_link_pasted_without_https_is_completed_and_one_can_take_it_off(c):
    assert c.post("/job/7/employer-page", json={"url": "  careers.oracle.com/jobs/348662 "}).get_json()["url"] \
        == "https://careers.oracle.com/jobs/348662"
    assert c.post("/job/7/employer-page", json={"url": ""}).get_json() == \
        {"ok": True, "url": "", "kind": "", "apply": ADZUNA}
    assert saved(7) == (None, None, ADZUNA)


@pytest.mark.parametrize("body", [
    {}, {"url": None}, {"url": 5}, [ORACLE], {"url": ORACLE, "kind": "homepage"},
    {"url": "javascript:alert(1)"}, {"url": "data:text/html,<b>hi</b>"}, {"url": "file:///etc/passwd"},
    {"url": "ftp://files.example.com/job"}, {"url": "mailto:hr@example.com"}, {"url": "not a link"},
    {"url": "https://exa mple.com/job"}, {"url": "https://" + "a" * 1000 + ".com"},
    {"url": "https://user:secret@careers.example.com/jobs/1"}, {"url": "http://localhost:8770/job/7"},
    {"url": "http://127.0.0.1/jobs/1"}, {"url": "http://192.168.1.5/jobs/1"}, {"url": "https://[::1]/jobs/1"},
    {"url": "https://intranet/jobs/1"}, {"url": "https://careers.example.com:99999/jobs/1"}])
def test_anything_but_a_safe_web_link_is_refused_and_nothing_changes(c, body):
    c.post("/job/7/employer-page", json={"url": ORACLE})
    refused = c.post("/job/7/employer-page", json=body)
    assert refused.status_code == 400 and refused.get_json()["error"]
    assert saved(7) == (ORACLE, "posting", ADZUNA)


def test_a_job_that_is_not_there_is_said(c):
    assert c.post("/job/999/employer-page", json={"url": ORACLE}).status_code == 404


@pytest.mark.parametrize("url", [
    ORACLE, "https://boards.greenhouse.io/globex/jobs/4567890", "https://www.globex.com/careers?gh_jid=4567890",
    "https://jobs.lever.co/globex/5f2c1a3e-1234-4cde-9abc-0123456789ab",
    "https://globex.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Site-Reliability-Engineer_R12345",
    "https://apply.workable.com/globex/j/1A2B3C4D5E/", "https://globex.taleo.net/careersection/2/jobdetail.ftl?job=12345",
    "https://jobs.smartrecruiters.com/Globex/743999912345678-site-reliability-engineer",
    "https://careers.example.com/jobs/512"])
def test_a_link_to_one_job_reads_as_its_posting(url):
    assert joblinks.kind(url) == joblinks.POSTING


@pytest.mark.parametrize("url", [
    "https://careers.oracle.com", "https://www.oracle.com/careers/", ORACLE_SITE,
    "https://careers.oracle.com/jobs/#en/sites/jobsearch/requisitions?keyword=SRE",
    "https://boards.greenhouse.io/globex", "https://globex.wd5.myworkdayjobs.com/en-US/External",
    "https://www.globex.com/careers/search?q=engineer&location=Austin", "https://www.globex.com/careers/2024",
    "https://www.globex.com/jobs/2024"])
def test_a_careers_site_or_a_search_reads_as_the_careers_site(url):
    assert joblinks.kind(url) == joblinks.CAREERS


# -- kept: a fetch, the morning's cloud merge, a restart, an older database ------------------------

def test_fetching_the_postings_again_keeps_it(c):
    from data_engineering.scraper.runner import _persist
    c.post("/job/7/employer-page", json={"url": ORACLE})
    _persist([RawJob(source="adzuna", company="Oracle", company_slug="oracle", external_id="5512",
                     title="Principal Site Reliability Engineer", location="United States",
                     description="We run the cloud, and more.", application_url=ADZUNA + "?v=2")])
    assert saved(7) == (ORACLE, "posting", ADZUNA + "?v=2")             # the board's link moves on, ours stays


def test_the_mornings_cloud_merge_keeps_it(tmp_path):
    from devops import merge_cloud
    from qa.tests.test_cloud_results import make_db
    local = make_db(tmp_path / "local.db", [("edited", "Not Applied", 1, "2026-10-19 07:00:00", None)])
    cloud = make_db(tmp_path / "cloud.db", [("edited", "Not Applied", 1, "2026-10-20 06:40:00", None)])
    with sqlite3.connect(local) as db:
        db.execute("UPDATE jobs SET employer_url = ?, employer_url_kind = 'posting'", (ORACLE,))
    with sqlite3.connect(cloud) as db:                                    # the employer edited the posting
        db.execute("UPDATE jobs SET content_hash = 'edited-2', application_url = 'https://x/2', "
                   "title = 'SRE (Remote)', employer_url = NULL, employer_url_kind = NULL")
    assert merge_cloud.merge(cloud, local)["edited"] == 1
    with sqlite3.connect(local) as db:
        assert db.execute("SELECT title, application_url, employer_url, employer_url_kind FROM jobs").fetchone() \
            == ("SRE (Remote)", "https://x/2", ORACLE, "posting")


def test_it_is_there_after_the_app_starts_again(c, tmp_path):
    c.post("/job/7/employer-page", json={"url": ORACLE})
    init_engine(f"sqlite:///{tmp_path / 'jobs.db'}")                      # the app, or the Mac, starts again
    assert saved(7) == (ORACLE, "posting", ADZUNA)
    assert 'id="ep-kind-now">(this job\'s posting, added by you)' in c.get("/job/7").get_data(as_text=True)


def test_an_older_database_gains_the_two_columns_and_keeps_every_job(tmp_path):
    from qa.tests.test_cloud_results import make_db
    path = make_db(tmp_path / "old.db", [("a", "Applied", 1, "2026-10-19 07:00:00", None),
                                         ("b", "Not Applied", 1, "2026-10-20 07:00:00", None)])
    with sqlite3.connect(path) as db:                                     # as it was before 25 Oct 2026
        db.execute("ALTER TABLE jobs DROP COLUMN employer_url_kind")
        db.execute("ALTER TABLE jobs DROP COLUMN employer_url")
        rows_before = db.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    before = (session_mod._engine, session_mod._SessionFactory)
    try:
        init_engine(f"sqlite:///{path}")                                  # the app starts on it
    finally:
        session_mod._engine, session_mod._SessionFactory = before
    with sqlite3.connect(path) as db:
        columns = [r[1] for r in db.execute("PRAGMA table_info(jobs)")]
        rows_after = db.execute(f"SELECT {', '.join(columns[:-2])} FROM jobs ORDER BY id").fetchall()
        added = db.execute("SELECT employer_url, employer_url_kind FROM jobs").fetchall()
    assert columns[-2:] == ["employer_url", "employer_url_kind"]          # added at the end, nothing else moved
    assert rows_after == rows_before and added == [(None, None), (None, None)]


# -- Auto-apply ------------------------------------------------------------------------------------

def opened_by_auto_apply(monkeypatch, tmp_path):
    from backend.apply import anyform
    opened = []

    def reach(page, url, *, say, check_s):
        opened.append(url)
        return anyform.Reached(page=page, url=url, kind="", error="stopped here by the test")
    monkeypatch.setattr(anyform, "reach", reach)
    monkeypatch.setattr(runner, "say", lambda line: None)
    with get_session() as s:
        job = s.get(Job, 7)
        s.expunge(job)
    outcome = runner.Outcome(job.id, job.company, job.title, "not-filled")
    runner._apply_anywhere(webapp._cfg, object(), job, None, "[Oracle]", outcome, tmp_path / "r.pdf", None,
                           "tailored", tmp_path, 60, auto=False)
    assert outcome.detail == "stopped here by the test"
    return opened


def test_auto_apply_opens_this_jobs_own_posting(c, monkeypatch, tmp_path):
    c.post("/job/7/employer-page", json={"url": ORACLE})
    assert opened_by_auto_apply(monkeypatch, tmp_path) == [ORACLE]


def test_auto_apply_never_takes_a_careers_site_for_the_form(c, monkeypatch, tmp_path):
    c.post("/job/7/employer-page", json={"url": ORACLE_SITE})
    assert opened_by_auto_apply(monkeypatch, tmp_path) == [ADZUNA]


def test_a_greenhouse_job_keeps_its_own_form_for_auto_apply(c):
    c.post("/job/8/employer-page", json={"url": "https://careers.globex.com/jobs/8"})
    page = c.get("/job/8").get_data(as_text=True)
    assert "Auto-apply still fills the Greenhouse form" in page
