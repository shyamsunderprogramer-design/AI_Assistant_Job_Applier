"""Adzuna's search API — offline, with results shaped like the documented response.

https://developer.adzuna.com/docs/search : each result carries id, title,
company.display_name, location.display_name, description (a snippet),
redirect_url, created (ISO time), salary_min/salary_max and
salary_is_predicted ("1" when Adzuna estimated the salary itself).
"""

from datetime import datetime, timedelta, timezone

import pytest

from data_engineering.db import session as session_mod
from data_engineering.db.models import Job
from data_engineering.db.session import get_session, init_engine
from data_engineering.scraper import adzuna
from data_engineering.scraper.adzuna import AdzunaClient, collect, parse_result, retire, store
from data_engineering.scraper.filters import JobFilter


def result(i=1, title="Senior DevOps Engineer", company="Acme Robotics",
           location="Austin, Travis County", predicted="0", **over):
    r = {"id": str(1000 + i), "title": f"<strong>{title}</strong>",
         "company": {"display_name": company},
         "location": {"display_name": location, "area": ["US", "Texas"]},
         "description": "Build Terraform and Kubernetes platforms on AWS&hellip;",
         "redirect_url": f"https://www.adzuna.com/details/{1000 + i}",
         "created": "2026-09-25T10:00:00Z",
         "salary_min": 150000, "salary_max": 180000, "salary_is_predicted": predicted}
    r.update(over)
    return r


class FakeResponse:
    def __init__(self, status=200, results=()):
        self.status_code, self._results = status, list(results)

    def json(self):
        return {"results": self._results, "count": len(self._results)}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.pages.pop(0) if self.pages else FakeResponse(results=[])


def client(pages, **kw):
    return AdzunaClient("id", "key", session=FakeSession(pages), sleep=lambda s: None, **kw)


US_DEVOPS = JobFilter(title_keywords=["devops", "platform engineer"],
                      exclude_location_keywords=["india", "united kingdom"],
                      location_keywords=["us"])


@pytest.fixture
def db():
    init_engine("sqlite:///:memory:")
    yield
    session_mod._engine = None
    session_mod._SessionFactory = None


# -- parsing ---------------------------------------------------------------

def test_a_result_becomes_a_posting():
    job = parse_result(result())
    assert job.raw.source == "adzuna"
    assert job.raw.title == "Senior DevOps Engineer"          # tags stripped
    assert job.raw.company == "Acme Robotics"
    assert job.raw.description.endswith("AWS…")                # entities decoded
    assert job.raw.posted_at == datetime(2026, 9, 25, 10, tzinfo=timezone.utc)


def test_a_stated_salary_is_kept():
    job = parse_result(result(predicted="0"))
    assert (job.salary_min, job.salary_max) == (150000, 180000)


def test_an_estimated_salary_is_not_passed_off_as_the_employers():
    job = parse_result(result(predicted="1"))
    assert job.salary_min is None and job.salary_max is None


def test_a_result_missing_its_company_is_dropped():
    assert parse_result(result(company="")) is None


# -- searching -------------------------------------------------------------

def test_each_title_is_searched_with_the_key_newest_first():
    c = client([FakeResponse(results=[result()])])
    collect(c, ["devops"], US_DEVOPS, pages=1)
    url, params = c.session.calls[0]
    assert url.endswith("/jobs/us/search/1")
    assert params["title_only"] == "devops" and params["sort_by"] == "date"
    assert params["app_id"] == "id" and params["app_key"] == "key"


def test_only_postings_that_pass_the_search_filter_are_kept():
    c = client([FakeResponse(results=[result(1), result(2, title="Barista"),
                                      result(3, location="Bangalore, India")])])
    jobs = collect(c, ["devops"], US_DEVOPS, pages=1)
    assert [j.raw.external_id for j in jobs] == ["1001"]


def test_a_short_page_ends_that_title():
    c = client([FakeResponse(results=[result()])])
    collect(c, ["devops"], US_DEVOPS, pages=3)
    assert len(c.session.calls) == 1


def test_the_call_budget_is_never_exceeded():
    full = lambda: FakeResponse(results=[result(i) for i in range(50)])
    c = client([full() for _ in range(10)], max_calls=3)
    collect(c, ["devops", "platform engineer"], US_DEVOPS, pages=5)
    assert c.calls == 3 and len(c.session.calls) == 3


def test_a_rejected_key_says_so():
    c = client([FakeResponse(status=401)])
    with pytest.raises(PermissionError):
        c.search("devops")


def test_a_rate_limit_stops_the_run_quietly():
    c = client([FakeResponse(status=429)])
    assert c.search("devops") == [] and c.search("sre") == []
    assert len(c.session.calls) == 1


# -- storing ---------------------------------------------------------------

def test_new_postings_are_stored_once(db):
    jobs = [parse_result(result(1)), parse_result(result(2, title="Platform Engineer"))]
    assert store(jobs) == (2, 0, 0)
    assert store(jobs) == (0, 2, 0)
    with get_session() as s:
        assert s.query(Job).filter_by(source="adzuna").count() == 2


def test_a_posting_already_found_on_a_board_is_skipped(db):
    with get_session() as s:
        s.add(Job(source="greenhouse", company="Acme Robotics", company_slug="acme",
                  external_id="9", title="Senior DevOps Engineer", application_url="u",
                  content_hash="h", is_open=True))
    assert store([parse_result(result(1))]) == (0, 0, 1)


def test_postings_not_seen_for_weeks_are_retired(db):
    store([parse_result(result(1))])
    old = datetime.now(timezone.utc) - timedelta(days=30)
    with get_session() as s:
        s.query(Job).filter_by(source="adzuna").update({"last_seen_at": old})
    assert retire(21) == 1
    with get_session() as s:
        assert s.query(Job).filter_by(source="adzuna").one().is_open is False


def test_without_keys_nothing_runs(monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    assert adzuna.run(cfg=None).skipped
