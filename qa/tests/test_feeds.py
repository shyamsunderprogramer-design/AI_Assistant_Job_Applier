"""The job feeds — offline. Payloads are shaped like the live responses read on
26 Sep 2026 (public feeds) or the providers' documented formats (keyed ones)."""

from datetime import datetime, timezone

import pytest

from data_engineering.db import session as session_mod
from data_engineering.db.models import Job
from data_engineering.db.session import get_session, init_engine
from data_engineering.scraper import feeds
from data_engineering.scraper.feeds import (careerjet, findwork, himalayas, hn_hiring, jobicy,
                                            jooble, remoteok, remotive, themuse, usajobs)
from data_engineering.scraper.feeds.common import PacedSession, store, when
from data_engineering.scraper.filters import JobFilter


class Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class FakeHTTP:
    """Answers by URL fragment; records every call."""

    def __init__(self, routes):
        self.routes, self.calls, self.seen = routes, 0, []

    def _answer(self, url, params):
        self.calls += 1
        self.seen.append((url, params))
        for fragment, payload in self.routes.items():
            if fragment in url:
                return payload(params) if callable(payload) else payload
        return {}

    def get_json(self, url, params=None, headers=None):
        return self._answer(url, params)

    def post_json(self, url, payload, headers=None):
        return self._answer(url, payload)


@pytest.fixture
def db():
    init_engine("sqlite:///:memory:")
    yield
    session_mod._engine = None
    session_mod._SessionFactory = None


# -- dates in every shape the feeds use -------------------------------------

@pytest.mark.parametrize("value", ["2026-09-21T12:55:11", "2026-09-21T12:55:11Z",
                                   "2026-09-21T12:55:11+00:00", "2026-09-21 12:55:11",
                                   "1790000111", "Mon, 21 Sep 2026 12:55:11 GMT"])
def test_every_date_shape_parses(value):
    parsed = when(value)
    assert parsed is not None and parsed.tzinfo is not None


# -- each feed's parser -----------------------------------------------------

def test_remotive():
    http = FakeHTTP({"remotive.com": {"jobs": [{
        "id": 2091144, "url": "https://remotive.com/remote-jobs/devops/sre-1", "title": "SRE",
        "company_name": "Acme", "publication_date": "2026-09-21T12:55:11",
        "candidate_required_location": "USA", "description": "<p>Run things</p>"}]}})
    [job] = remotive.fetch(http, [], Cfg({"feeds.remotive.categories": ["devops"]}))
    assert (job.raw.title, job.raw.location) == ("SRE", "Remote (USA)")
    assert job.raw.description == "Run things"


def test_remoteok_skips_its_legal_notice_and_keeps_pay():
    http = FakeHTTP({"remoteok.com": [
        {"last_updated": 1, "legal": "..."},
        {"id": "1137062", "position": "DevOps Engineer", "company": "Acme",
         "date": "2026-08-22T00:00:12+00:00", "location": "", "salary_min": 150000,
         "salary_max": 0, "url": "https://remoteOK.com/remote-jobs/x", "description": "d"}]})
    [job] = remoteok.fetch(http, [], Cfg({"feeds.remoteok.tags": ["devops"]}))
    assert job.raw.location == "Remote (anywhere)"
    assert (job.salary_min, job.salary_max) == (150000, None)


def test_himalayas_reads_unix_dates_and_country_lists():
    http = FakeHTTP({"himalayas.app": {"jobs": [{
        "guid": "https://himalayas.app/companies/a/jobs/b", "title": "Platform Engineer",
        "companyName": "Interrahealth", "pubDate": "1790394375",
        "locationRestrictions": ["United States"], "minSalary": 150000, "maxSalary": 180000,
        "currency": "USD", "applicationLink": "https://himalayas.app/companies/a/jobs/b",
        "description": "x"}]}})
    [job] = himalayas.fetch(http, [], Cfg({"feeds.himalayas.pages": 3}))
    assert job.raw.location == "Remote (United States)"
    assert job.raw.posted_at.year == 2026 and job.salary_min == 150000
    assert http.calls == 1                        # a short page ends the paging


def test_jobicy_searches_per_title_in_the_us():
    http = FakeHTTP({"jobicy.com": {"jobs": [{
        "id": 153779, "jobTitle": "DevOps Engineer", "companyName": "Oddball",
        "pubDate": "2026-09-21T09:35:21+00:00", "jobGeo": "USA", "salaryMin": 110000,
        "salaryMax": 140000, "url": "https://jobicy.com/jobs/153779", "jobDescription": "x"}]}})
    jobs = jobicy.fetch(http, ["devops", "sre"], Cfg())
    assert [p["tag"] for _, p in http.seen] == ["devops", "sre"]
    assert all(p["geo"] == "usa" for _, p in http.seen)
    assert jobs[0].salary_max == 140000


def test_themuse_joins_locations_and_stops_at_the_last_page():
    http = FakeHTTP({"themuse.com": {"page_count": 1, "results": [{
        "id": 22167029, "name": "Site Reliability Engineer", "company": {"name": "SpaceX"},
        "locations": [{"name": "Lockhart, TX"}, {"name": "Remote"}],
        "publication_date": "2026-09-16T20:34:24Z",
        "refs": {"landing_page": "https://www.themuse.com/jobs/spacex/sre"}, "contents": "x"}]}})
    [job] = themuse.fetch(http, [], Cfg({"feeds.themuse.pages": 5}))
    assert job.raw.location == "Lockhart, TX; Remote" and http.calls == 1


def test_hn_header_line_is_split_into_company_title_place():
    assert hn_hiring.split_header(
        "Acme Robotics | Senior DevOps Engineer | Austin, TX or REMOTE (US) | $180k<p>More") == \
        ("Acme Robotics", "Senior DevOps Engineer", "Austin, TX or REMOTE (US)")
    assert hn_hiring.split_header("We are hiring lots of people, email me") is None


def test_hn_reads_the_newest_who_is_hiring_thread():
    http = FakeHTTP({
        "search_by_date": {"hits": [
            {"title": "Ask HN: Who wants to be hired? (September 2026)", "objectID": "2"},
            {"title": "Ask HN: Who is hiring? (September 2026)", "objectID": "1"}]},
        "items/1": {"children": [
            {"id": 11, "text": "Acme | SRE | Remote (US)<p>Apply at acme.com", "created_at": "2026-09-01T15:00:00Z"},
            {"id": 12, "text": None},
            {"id": 13, "text": "Just saying hi"}]}})
    [job] = hn_hiring.fetch(http, [], Cfg())
    assert job.raw.application_url == "https://news.ycombinator.com/item?id=11"


def test_usajobs_keeps_annual_pay_only():
    item = lambda code: {"MatchedObjectId": f"8{code}", "MatchedObjectDescriptor": {
        "PositionTitle": "IT Specialist (Cloud/DevOps)", "OrganizationName": "Defense Health Agency",
        "PositionURI": "https://www.usajobs.gov/job/1", "ApplyURI": ["https://www.usajobs.gov/job/1/apply"],
        "PositionLocationDisplay": "Falls Church, Virginia",
        "PublicationStartDate": "2026-09-25T00:00:00.0000",
        "PositionRemuneration": [{"MinimumRange": "120000.0", "MaximumRange": "160000.0",
                                  "RateIntervalCode": code}],
        "UserArea": {"Details": {"JobSummary": "Cloud work"}}, "QualificationSummary": "GS-13"}}
    http = FakeHTTP({"usajobs.gov": {"SearchResult": {"SearchResultItems": [item("PA"), item("PH")]}}})
    yearly, hourly = usajobs.fetch(http, ["devops"], Cfg())
    assert yearly.salary_min == 120000 and hourly.salary_min is None
    assert yearly.raw.application_url.endswith("/apply")


def test_jooble_posts_its_search():
    http = FakeHTTP({"jooble.org": {"jobs": [{
        "id": -123, "title": "DevOps Engineer", "company": "Acme", "link": "https://jooble.org/desc/-123",
        "location": "Austin, TX", "snippet": "Terraform...", "updated": "2026-09-25T00:00:00.0000000"}]}})
    [job] = jooble.fetch(http, ["devops"], Cfg())
    assert http.seen[0][1]["keywords"] == "devops" and job.raw.external_id == "-123"


def test_careerjet_and_findwork_parse():
    cj = FakeHTTP({"careerjet": {"jobs": [{
        "title": "Cloud Engineer", "company": "Acme", "locations": "Denver, CO",
        "date": "Thu, 25 Sep 2026 00:00:00 GMT", "url": "https://jobviewtrack.com/x",
        "description": "x", "salary_min": 140000, "salary_max": 170000, "salary_type": "Y"}]}})
    [a] = careerjet.fetch(cj, ["cloud engineer"], Cfg())
    assert a.salary_max == 170000
    fw = FakeHTTP({"findwork.dev": {"results": [{
        "id": 9, "role": "SRE", "company_name": "Acme", "location": "NYC", "remote": True,
        "url": "https://findwork.dev/9", "text": "x", "date_posted": "2026-09-25T00:00:00Z"}]}})
    [b] = findwork.fetch(fw, ["sre"], Cfg())
    assert b.raw.location == "Remote (NYC)"


# -- the runner ---------------------------------------------------------------

def test_keyed_feeds_wait_for_their_keys(monkeypatch):
    monkeypatch.delenv("JOOBLE_API_KEY", raising=False)
    assert feeds.missing_keys(jooble) == ["JOOBLE_API_KEY"]
    assert feeds.missing_keys(remotive) == []


def test_one_feed_failing_does_not_stop_the_rest(db, monkeypatch):
    monkeypatch.setattr("data_engineering.scraper.filters.resolve_filter",
                        lambda cfg: JobFilter(title_keywords=["sre"]))

    def broken(http, titles, cfg):
        raise ConnectionError("down")

    monkeypatch.setattr(remotive, "fetch", broken)
    monkeypatch.setattr(remoteok, "fetch", lambda http, titles, cfg: [
        feeds.common.FeedJob(feeds.common.posting("remoteok", job_id=1, title="SRE",
                                                  company="Acme", url="u"))])
    out = feeds.run_feeds(Cfg(), only=["remotive", "remoteok"], polite_client=object())
    by = {s.name: s for s in out}
    assert by["remotive"].error.startswith("ConnectionError")
    assert by["remoteok"].new == 1


def test_the_same_job_from_two_feeds_is_stored_once(db):
    mk = lambda src, i: feeds.common.FeedJob(feeds.common.posting(
        src, job_id=i, title="Senior SRE", company="Acme Inc", url=f"u{i}"))
    assert store([mk("remoteok", 1)]) == (1, 0, 0)
    assert store([mk("jobicy", 2)]) == (0, 0, 1)
    with get_session() as s:
        assert s.query(Job).count() == 1


def test_a_keyed_api_is_paced_and_capped():
    class Resp:
        status_code = 200
        def json(self): return {}
        def raise_for_status(self): pass

    class Session:
        headers = {}
        calls = 0
        def get(self, *a, **k):
            Session.calls += 1
            return Resp()

    slept = []
    paced = PacedSession(interval=3, max_calls=2, session=Session(), sleep=slept.append,
                         clock=lambda: 100.0)
    for _ in range(4):
        paced.get_json("https://data.usajobs.gov/api/search")
    assert Session.calls == 2 and slept == [3.0]


def test_usajobs_accepts_an_it_specialist_whose_text_names_the_work():
    from data_engineering.scraper.feeds.common import FeedJob, posting
    f = JobFilter(title_keywords=["devops", "cloud engineer"])
    mk = lambda title, text: FeedJob(posting("usajobs", job_id=title, title=title,
                                             company="Agency", url="u", description=text))
    assert usajobs.accept(mk("IT Specialist (SYSADMIN)", "Build DevOps pipelines on AWS"), f)
    assert not usajobs.accept(mk("IT Specialist (CUSTSPT)", "Help desk tickets"), f)
    assert not usajobs.accept(mk("Meteorological Technician", "DevOps"), f)
    assert usajobs.accept(mk("Cloud Engineer", "anything"), f)
