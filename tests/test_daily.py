"""Daily-loop tests — in-memory DB, no network.

The behaviour worth protecting is resilience: this runs unattended behind cron,
so a stage that fails must not take the digest with it. A scrape can die and
yesterday's ranking is still worth reading.
"""

from datetime import timedelta

import pytest

from daily import DailyReport, render_digest, run_daily
from db import session as session_mod
from db.models import Company, Job, utcnow
from db.session import get_session, init_engine

BASE_RESUME = """JANE DOE
GA, USA
SKILLS
Python, Terraform, Kubernetes, AWS
EXPERIENCE
- Built Terraform modules and Kubernetes platforms on AWS
"""


class FakeConfig:
    def __init__(self, resume_path, **overrides):
        self._values = {
            "resume.base_path": str(resume_path),
            "resume.min_score": 0.25,
            "resume.output_dir": "resume/output",
            "excel.path": None,           # set per-test to a tmp file
            "limits.stale_company_warn_days": 0,
            "filters.use_derived_profile": False,
        }
        self._values.update(overrides)

    def get(self, path, default=None):
        value = self._values.get(path, default)
        return default if value is None and path == "excel.path" else value


@pytest.fixture
def env(tmp_path):
    init_engine("sqlite:///:memory:")
    resume = tmp_path / "base_resume.txt"
    resume.write_text(BASE_RESUME, encoding="utf-8")
    yield FakeConfig(resume, **{"excel.path": str(tmp_path / "tracker.xlsx")})
    session_mod._engine = None
    session_mod._SessionFactory = None


def add_job(ext="1", title="Site Reliability Engineer", score=None, found_at=None, is_open=True):
    with get_session() as s:
        job = Job(
            source="greenhouse", company="Acme", company_slug="acme", external_id=ext,
            title=title, location="Remote - US",
            description="Requirements:\n- Terraform\n- Kubernetes\n- AWS",
            requirements="Terraform, Kubernetes, AWS",
            application_url=f"https://example.com/{ext}", content_hash=ext,
            ats_match_score=score, is_open=is_open,
        )
        if found_at:
            job.found_at = found_at
        s.add(job)


# -- the loop runs end to end ----------------------------------------------

def test_daily_scores_and_exports(env):
    add_job("1")
    report = run_daily(env, skip_scrape=True)

    assert report.ok
    assert report.scored == 1
    assert [s.name for s in report.stages] == ["score", "export", "digest"]


def test_scrape_stage_is_included_when_not_skipped(env, monkeypatch):
    import daily as daily_mod

    class FakeSummary:
        companies_attempted = 3; companies_failed = 0; jobs_seen = 120
        jobs_kept = 4; jobs_new = 2; jobs_updated = 0; jobs_closed = 1; jobs_reopened = 0

    monkeypatch.setattr("scraper.runner.run_scrape", lambda cfg: FakeSummary())
    monkeypatch.setattr("scraper.runner.sync_seed_companies", lambda cfg: 0)

    report = run_daily(env)
    assert report.companies == 3 and report.jobs_new == 2 and report.jobs_closed == 1


# -- resilience: a failing stage must not abort the run --------------------

def test_a_failing_stage_does_not_stop_the_others(env, monkeypatch):
    """The whole point of running unattended: partial results still reach you."""
    monkeypatch.setattr(
        "scraper.runner.sync_seed_companies",
        lambda cfg: (_ for _ in ()).throw(RuntimeError("network down")),
    )
    add_job("1")

    report = run_daily(env)

    assert not report.ok
    assert [s.name for s in report.failed_stages] == ["scrape"]
    # ...and the rest still ran
    assert {"score", "export", "digest"} <= {s.name for s in report.stages}


def test_a_failed_stage_records_why(env, monkeypatch):
    monkeypatch.setattr(
        "scraper.runner.sync_seed_companies",
        lambda cfg: (_ for _ in ()).throw(ValueError("bad config")),
    )
    report = run_daily(env)
    assert "ValueError: bad config" in report.failed_stages[0].error


# -- exit codes, so cron can act on them -----------------------------------

def test_exit_code_zero_when_everything_worked(env):
    add_job("1")
    assert run_daily(env, skip_scrape=True).exit_code() == 0


def test_exit_code_one_when_a_stage_failed(env, monkeypatch):
    monkeypatch.setattr(
        "scraper.runner.sync_seed_companies",
        lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert run_daily(env).exit_code() == 1


def test_exit_code_two_when_nothing_ran():
    assert DailyReport().exit_code() == 2


# -- the digest is the product ---------------------------------------------

def test_digest_leads_with_what_changed(env):
    add_job("1", title="Senior SRE")
    report = run_daily(env, skip_scrape=True)
    digest = render_digest(report, env)
    assert "NEW since yesterday" in digest
    assert "Senior SRE" in digest


def test_digest_says_so_when_nothing_is_new(env):
    """A quiet morning must read as quiet, not as an empty page."""
    old = utcnow() - timedelta(days=30)
    add_job("1", found_at=old)
    report = run_daily(env, skip_scrape=True)
    assert "No new postings" in render_digest(report, env)


def test_digest_shows_apply_links_for_strong_matches_only(env):
    add_job("1", title="Site Reliability Engineer")   # will score well
    report = run_daily(env, skip_scrape=True)
    digest = render_digest(report, env)
    assert "https://example.com/1" in digest


def test_digest_lists_closed_jobs(env):
    add_job("1")
    with get_session() as s:
        job = s.query(Job).one()
        job.is_open = False
        job.closed_at = utcnow()

    report = run_daily(env, skip_scrape=True)
    assert "CLOSED" in render_digest(report, env)


def test_digest_reports_failed_stages(env, monkeypatch):
    monkeypatch.setattr(
        "scraper.runner.sync_seed_companies",
        lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    digest = render_digest(run_daily(env), env)
    assert "STAGES THAT FAILED" in digest
    assert "the rest still ran" in digest


def test_digest_warns_about_stale_companies(env):
    env._values["limits.stale_company_warn_days"] = 1
    with get_session() as s:
        s.add(Company(name="Neglected", slug="neglected", source="greenhouse"))
    add_job("1")

    digest = render_digest(run_daily(env, skip_scrape=True), env)
    assert "not scraped recently" in digest


def test_since_hours_controls_what_counts_as_new(env):
    add_job("1", found_at=utcnow() - timedelta(hours=48))

    recent = run_daily(env, skip_scrape=True, since_hours=24)
    assert recent.new_jobs == []

    session_mod._engine  # keep the same DB
    wider = run_daily(env, skip_scrape=True, since_hours=72)
    assert len(wider.new_jobs) == 1


def test_closed_jobs_are_not_listed_as_new(env):
    add_job("1", is_open=False)
    report = run_daily(env, skip_scrape=True)
    assert report.new_jobs == []


def test_digest_is_plain_text_and_never_empty(env):
    digest = render_digest(run_daily(env, skip_scrape=True), env)
    assert digest.strip() and "JOB DIGEST" in digest
