"""Posting lifecycle tests — in-memory DB, no network.

The interesting cases here are the ones where closing would be WRONG. A
too-eager closer is worse than none at all: it hides live jobs, and nothing
errors to tell you.
"""

from datetime import datetime, timedelta, timezone

import pytest

from db import session as session_mod
from db.models import Company, Job, utcnow
from db.session import get_session, init_engine
from scraper.base import CompanyRef, RawJob
from scraper.lifecycle import reconcile_board, stale_companies


@pytest.fixture
def db():
    init_engine("sqlite:///:memory:")
    yield
    session_mod._engine = None
    session_mod._SessionFactory = None


def add_job(ext, title="Backend Engineer", status="Not Applied", is_open=True):
    with get_session() as s:
        s.add(Job(
            source="greenhouse", company="Acme", company_slug="acme", external_id=ext,
            title=title, location="Remote", description="jd", requirements="jd",
            application_url=f"https://example.com/{ext}", content_hash=ext,
            status=status, is_open=is_open,
        ))


def get_job(ext):
    with get_session() as s:
        return s.query(Job).filter_by(external_id=ext).one()


# -- the happy path --------------------------------------------------------

def test_job_missing_from_board_is_closed(db):
    add_job("1")
    add_job("2")

    result = reconcile_board("greenhouse", "acme", ["1"])

    assert result.closed == 1 and result.seen == 1
    assert get_job("1").is_open is True
    gone = get_job("2")
    assert gone.is_open is False
    assert gone.closed_at is not None
    assert gone.status == "Closed"


def test_still_listed_job_has_last_seen_advanced(db):
    add_job("1")
    before = get_job("1").last_seen_at
    later = utcnow() + timedelta(hours=1)

    reconcile_board("greenhouse", "acme", ["1"], now=later)

    after = get_job("1").last_seen_at
    assert after > before


# -- the cases where closing would be wrong --------------------------------

def test_empty_board_closes_nothing(db):
    """An empty 200 is indistinguishable from a broken board."""
    add_job("1")
    add_job("2")

    result = reconcile_board("greenhouse", "acme", [])

    assert result.closed == 0
    assert result.skipped_empty is True
    assert get_job("1").is_open is True
    assert get_job("2").is_open is True


def test_empty_board_closes_when_explicitly_allowed(db):
    add_job("1")

    result = reconcile_board("greenhouse", "acme", [], close_on_empty_board=True)

    assert result.closed == 1
    assert get_job("1").is_open is False


def test_another_companys_jobs_are_untouched(db):
    add_job("1")
    with get_session() as s:
        s.add(Job(
            source="lever", company="Other", company_slug="other", external_id="9",
            title="Engineer", location="Remote", description="jd", requirements="jd",
            application_url="https://example.com/9", content_hash="9",
        ))

    reconcile_board("greenhouse", "acme", ["1"])

    with get_session() as s:
        assert s.query(Job).filter_by(company_slug="other").one().is_open is True


def test_user_status_survives_closure(db):
    """"Applied" is the row the user most needs to keep. Never overwrite it."""
    add_job("1", status="Applied")

    reconcile_board("greenhouse", "acme", [])  # empty -> skipped
    reconcile_board("greenhouse", "acme", ["other"], close_on_empty_board=True)

    job = get_job("1")
    assert job.is_open is False          # still closed
    assert job.status == "Applied"       # but the user's decision stands


# -- reopening -------------------------------------------------------------

def test_reappearing_job_reopens_and_clears_closed_status(db):
    add_job("1")
    reconcile_board("greenhouse", "acme", ["other"], close_on_empty_board=True)
    assert get_job("1").status == "Closed"

    result = reconcile_board("greenhouse", "acme", ["1"])

    job = get_job("1")
    assert result.reopened == 1
    assert job.is_open is True
    assert job.closed_at is None
    assert job.status == "Not Applied"


def test_reopen_does_not_resurrect_a_user_status(db):
    add_job("1", status="Rejected", is_open=False)

    reconcile_board("greenhouse", "acme", ["1"])

    assert get_job("1").status == "Rejected"


# -- closure is judged on the raw board, not the filtered subset ------------

def test_closure_uses_every_posting_not_just_filtered_ones(db):
    """A stored job whose title stopped matching the filters is still LISTED.

    The runner passes raw board ids for exactly this reason; passing the
    filtered set instead would close live jobs whenever a filter changed.
    """
    add_job("1", title="Backend Engineer")
    add_job("2", title="Staff Engineer")  # would be excluded by title filters

    reconcile_board("greenhouse", "acme", ["1", "2"])

    assert get_job("2").is_open is True


# -- staleness -------------------------------------------------------------

def test_stale_companies_flags_old_and_never_scraped(db):
    now = datetime(2026, 9, 6, tzinfo=timezone.utc)
    with get_session() as s:
        s.add(Company(name="Fresh", slug="fresh", source="greenhouse",
                      last_scraped_at=datetime(2026, 9, 5)))       # naive, as SQLite returns
        s.add(Company(name="Old", slug="old", source="greenhouse",
                      last_scraped_at=datetime(2026, 8, 1)))
        s.add(Company(name="Never", slug="never", source="lever"))
        s.add(Company(name="Off", slug="off", source="lever",
                      last_scraped_at=datetime(2026, 1, 1), active=False))

    stale = stale_companies(7, now=now)

    slugs = {row.slug for row in stale}
    assert slugs == {"old", "never"}        # fresh is recent, off is inactive
    assert next(r for r in stale if r.slug == "never").days is None


def test_stale_disabled_returns_nothing(db):
    with get_session() as s:
        s.add(Company(name="Never", slug="never", source="lever"))
    assert stale_companies(0) == []


# -- the integration case that matters: a FAILED fetch closes nothing -------

class _BoomScraper:
    """A board that is down, not empty."""

    def fetch_jobs(self, company):
        raise RuntimeError("HTTP 500")


class _OkScraper:
    def __init__(self, jobs):
        self._jobs = jobs

    def fetch_jobs(self, company):
        return self._jobs


class FakeConfig:
    def __init__(self, **overrides):
        self._values = {
            "portals.greenhouse.enabled": True,
            "portals.lever.enabled": False,
            "companies.scrape_discovered": True,
            "limits.max_companies_per_run": 0,
            "limits.deactivate_after_failures": 0,
            "limits.close_on_empty_board": False,
        }
        self._values.update(overrides)

    def get(self, path, default=None):
        return self._values.get(path, default)


def _run_with(monkeypatch, scraper):
    from scraper import runner as runner_mod

    monkeypatch.setattr(runner_mod, "PoliteClient", lambda *a, **k: object())
    monkeypatch.setattr(runner_mod.HttpSettings, "from_config", staticmethod(lambda cfg: None))
    monkeypatch.setattr(runner_mod, "build_scrapers", lambda cfg, client: {"greenhouse": scraper})
    monkeypatch.setattr(runner_mod, "resolve_filter", lambda cfg: _PassThroughFilter())
    return runner_mod.run_scrape(FakeConfig())


class _PassThroughFilter:
    def apply(self, jobs):
        return list(jobs)


def test_failed_fetch_closes_nothing(db, monkeypatch):
    """The whole point. An outage must not look like a mass hiring freeze."""
    with get_session() as s:
        s.add(Company(name="Acme", slug="acme", source="greenhouse"))
    add_job("1")
    add_job("2")

    summary = _run_with(monkeypatch, _BoomScraper())

    assert summary.companies_failed == 1
    assert summary.jobs_closed == 0
    assert get_job("1").is_open is True
    assert get_job("2").is_open is True


def test_successful_fetch_closes_the_vanished_job(db, monkeypatch):
    with get_session() as s:
        s.add(Company(name="Acme", slug="acme", source="greenhouse"))
    add_job("1")
    add_job("2")

    still_listed = [RawJob(
        source="greenhouse", company="Acme", company_slug="acme", external_id="1",
        title="Backend Engineer", location="Remote", description="jd",
        application_url="https://example.com/1",
    )]
    summary = _run_with(monkeypatch, _OkScraper(still_listed))

    assert summary.companies_failed == 0
    assert summary.jobs_closed == 1
    assert get_job("1").is_open is True
    assert get_job("2").is_open is False
