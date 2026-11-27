"""The Nov 2026 discovery corrections (the cloud fetch's code), offline: closures on complete listings, Workday resuming its descriptions,
network outages, quotas, the indexed merge and its log retention, the one coverage report, the morning that ranks
before it recovers, and no routine top-up. No test reads a real job site or the real database."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from data_engineering.db import session as session_mod
from data_engineering.db.models import Company, Job, ScrapeLog
from data_engineering.db.session import get_session, init_engine
from data_engineering.scraper import runner
from data_engineering.scraper.base import CompanyRef, Listing, PortalScraper, RawJob
from data_engineering.scraper.http_client import NetworkError, RateLimited

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def db():
    init_engine("sqlite:///:memory:")
    yield
    session_mod._engine = None
    session_mod._SessionFactory = None


class Cfg:
    user_agent = "test-agent"

    def __init__(self, **over):
        self.d = {"http.respect_robots": False, "http.min_delay_seconds": 0.001, "http.jitter_seconds": 0.0,
                  "http.max_retries": 0, "limits.scrape_workers": 1, **over}

    def get(self, key, default=None):
        return self.d.get(key, default)


class Wanted:
    """The search: titles with "Engineer" are wanted; descriptions are not checked."""

    def matches_stub(self, job):
        return "Engineer" in job.title

    def apply(self, jobs):
        return [j for j in jobs if "Engineer" in j.title]

    def reject_reason(self, job):
        return None


def stored(source, slug, ext, title="Data Engineer", status="Not Applied", url=None, description="old text"):
    with get_session() as s:
        if not s.query(Company).filter_by(source=source, slug=slug).one_or_none():
            s.add(Company(name=slug, slug=slug, source=source, origin="seed", active=True))
        s.add(Job(source=source, company=slug, company_slug=slug, external_id=ext, title=title, location="Remote",
                  description=description, requirements=None, application_url=url or f"https://x/{ext}",
                  content_hash=f"h-{ext}", status=status, is_open=True))


def job(source, slug, ext):
    with get_session() as s:
        row = s.query(Job).filter_by(source=source, company_slug=slug, external_id=ext).one()
        s.expunge(row)
        return row


# -- a two-call board with durable listing ids (BambooHR, JazzHR, Jobvite, Workable) -----------------------------

class TwoCall(PortalScraper):
    name = "bamboohr"
    durable_listing_ids = True
    LISTING: list = []           # (id, title)
    COMPLETE = True
    FAIL_DETAIL: set = set()
    RAISE = None
    asked: list = []

    def board_url(self, slug):
        return f"https://{slug}.bamboohr.com/careers"

    def board_exists(self, slug):
        return True

    def stub_jobs(self, company):
        if self.RAISE:
            raise self.RAISE
        return Listing([RawJob(source=self.name, company=company.name, company_slug=company.slug, external_id=i,
                               title=t, location="Remote", description=None, application_url=f"https://x/{i}")
                        for i, t in self.LISTING], complete=self.COMPLETE)

    def fill_details(self, company, jobs):
        for j in jobs:
            type(self).asked.append(j.external_id)
            if j.external_id in self.FAIL_DETAIL:
                j.detail_missing = True
            else:
                j.description = f"description of {j.external_id}"
        return jobs

    def fetch_jobs(self, company):
        return self.fill_details(company, self.stub_jobs(company))


@pytest.fixture
def two_call(monkeypatch, db):
    TwoCall.LISTING, TwoCall.COMPLETE, TwoCall.FAIL_DETAIL, TwoCall.RAISE, TwoCall.asked = [], True, set(), None, []
    monkeypatch.setattr(runner, "SCRAPER_TYPES", {"bamboohr": TwoCall})
    monkeypatch.setattr(runner, "resolve_filter", lambda cfg: Wanted())
    return TwoCall


def read(source="bamboohr", slug="acme", **cfg):
    return runner.scrape_companies(Cfg(**cfg), [CompanyRef(name=slug, slug=slug, source=source)])


def test_a_complete_listing_closes_what_it_no_longer_names_even_when_its_description_was_skipped(two_call):
    stored("bamboohr", "acme", "1")                         # still listed, wanted
    stored("bamboohr", "acme", "2", title="Chef")           # still listed, NOT wanted: never described
    stored("bamboohr", "acme", "3")                         # taken down
    two_call.LISTING = [("1", "Data Engineer"), ("2", "Chef"), ("4", "Platform Engineer")]
    summary = read()
    assert job("bamboohr", "acme", "3").is_open is False                 # the legitimate closure
    assert job("bamboohr", "acme", "2").is_open is True                  # skipped as irrelevant: still on the board
    assert job("bamboohr", "acme", "1").is_open is True
    assert sorted(two_call.asked) == ["1", "4"]                          # only the wanted ones cost a description
    assert summary.outcomes[("bamboohr", "acme")] == ("complete", "")    # skipping irrelevant ones is not partial
    d = summary.details[("bamboohr", "acme")]
    assert d == {"listing": "complete", "listed": 3, "relevant": 2, "described": 2, "pending": 0, "skipped": 1}


def test_a_failed_description_closes_nothing_listed_and_keeps_the_stored_text(two_call):
    stored("bamboohr", "acme", "1", description="the text we had")
    stored("bamboohr", "acme", "3")
    two_call.LISTING = [("1", "Data Engineer")]
    two_call.FAIL_DETAIL = {"1"}
    summary = read()
    kept = job("bamboohr", "acme", "1")
    assert kept.is_open is True and kept.description == "the text we had"   # not wiped by an empty one
    assert job("bamboohr", "acme", "3").is_open is False                    # gone from the listing: closed
    assert summary.outcomes[("bamboohr", "acme")] == ("partial", "")        # a relevant description is pending
    assert summary.details[("bamboohr", "acme")]["pending"] == 1


def test_an_incomplete_listing_an_empty_one_a_failure_or_a_refusal_closes_nothing(two_call):
    stored("bamboohr", "acme", "1")
    stored("bamboohr", "acme", "3")
    two_call.LISTING, two_call.COMPLETE = [("1", "Data Engineer")], False      # stopped at a page cap
    s = read()
    assert job("bamboohr", "acme", "3").is_open is True and s.outcomes[("bamboohr", "acme")][0] == "partial"
    assert s.details[("bamboohr", "acme")]["listing"] == "incomplete"
    two_call.LISTING, two_call.COMPLETE = [], True                              # an empty 200
    read()
    assert job("bamboohr", "acme", "1").is_open is True and job("bamboohr", "acme", "3").is_open is True
    for error in (ValueError("not a board"), RateLimited("429"), NetworkError("no answer")):
        two_call.RAISE = error
        s = read()
        assert job("bamboohr", "acme", "1").is_open is True and job("bamboohr", "acme", "3").is_open is True
        assert s.outcomes[("bamboohr", "acme")][0] in ("failed", "rate_limited", "not_attempted")


def test_a_closed_posting_keeps_the_persons_own_status(two_call):
    stored("bamboohr", "acme", "9", status="Applied")
    stored("bamboohr", "acme", "8", status="Interviewing")
    two_call.LISTING = [("1", "Data Engineer")]
    read()
    for ext, status in (("9", "Applied"), ("8", "Interviewing")):
        gone = job("bamboohr", "acme", ext)
        assert gone.is_open is False and gone.status == status


def test_nothing_is_closed_on_age_alone(two_call):
    stored("bamboohr", "acme", "1")
    with get_session() as s:
        s.query(Job).update({Job.last_seen_at: datetime(2026, 1, 1)})
    two_call.LISTING = [("1", "Data Engineer")]
    read()
    assert job("bamboohr", "acme", "1").is_open is True


# -- Workday: resume where the last read stopped ---------------------------------------------------------------

class FakeWorkday:
    """A tenant with N relevant postings; details answered from the path; some fail."""

    def __init__(self, n, fail=()):
        self.n, self.fail, self.details = n, set(fail), []

    def post(self, url, json=None, headers=None):
        offset, limit = json["offset"], json["limit"]
        rows = [{"externalPath": f"/job/Remote/Site-Reliability-Engineer_R{i}", "title": "Site Reliability Engineer",
                 "locationsText": "Remote"} for i in range(offset, min(self.n, offset + limit))]
        return SimpleNamespace(json=lambda: {"total": self.n if offset == 0 else 0, "jobPostings": rows})

    def get(self, url, headers=None):
        path = url[url.index("/job/"):]
        self.details.append(path)
        if path in self.fail:
            from data_engineering.scraper.http_client import FetchError
            raise FetchError("HTTP 500")
        rid = path.rsplit("_", 1)[1]
        return SimpleNamespace(json=lambda: {"jobPostingInfo": {
            "jobDescription": "<p>Run things.</p>", "jobReqId": rid, "location": "Remote",
            "externalUrl": f"https://acme.wd5.myworkdayjobs.com/External{path}"}})


def test_workday_resumes_its_missing_descriptions_instead_of_the_same_first_sixty(monkeypatch, db):
    from data_engineering.scraper.filters import JobFilter
    from data_engineering.scraper.workday import WorkdayScraper
    slug = "acme:wd5:External"
    client = FakeWorkday(75, fail={"/job/Remote/Site-Reliability-Engineer_R3"})
    monkeypatch.setattr(runner, "SCRAPER_TYPES", {"workday": lambda c: WorkdayScraper(client)})
    monkeypatch.setattr(runner, "resolve_filter", lambda cfg: JobFilter(title_keywords=["site reliability"]))
    stored("workday", slug, "R_OLD", title="Site Reliability Engineer",
           url="https://acme.wd5.myworkdayjobs.com/External/job/Remote/Gone_R_OLD")
    first = read("workday", slug)
    d = first.details[("workday", slug)]
    assert d["relevant"] == 75 and d["described"] == 59 and d["pending"] == 16    # 60 asked, one failed
    assert first.outcomes[("workday", slug)] == ("partial", "")
    with get_session() as s:
        assert s.query(Job).filter_by(source="workday").count() == 60               # 59 new + the old one
        assert not s.query(Job).filter(Job.external_id.like("/job/%")).count()     # a failed one is not stored by path
    asked_first = list(client.details)
    client.details.clear()
    client.fail.clear()
    second = read("workday", slug)
    d = second.details[("workday", slug)]
    assert d["pending"] == 0 and second.outcomes[("workday", slug)] == ("complete", "")
    # The 16 never described were asked first -- not the same first sixty again.
    assert set(client.details[:16]) == {p for p in (f"/job/Remote/Site-Reliability-Engineer_R{i}" for i in range(75))
                                        if p not in asked_first or p.endswith("_R3")}
    # Workday never closes on its listing: the posting gone from it stays open.
    assert job("workday", slug, "R_OLD").is_open is True


def test_a_workday_listing_past_its_page_cap_is_incomplete(monkeypatch, db):
    from data_engineering.scraper import workday
    monkeypatch.setattr(workday, "MAX_PAGES", 2)
    listing = workday.WorkdayScraper(FakeWorkday(75)).stub_jobs(CompanyRef("Acme", "acme:wd5:External", "workday"))
    assert len(listing) == 40 and listing.complete is False and listing.total == 75


# -- network outages ---------------------------------------------------------------------------------------------

class Silent(TwoCall):
    pass


def test_no_internet_stops_the_run_within_a_few_boards_and_retires_nothing(monkeypatch, db):
    monkeypatch.setattr(runner, "SCRAPER_TYPES", {"bamboohr": Silent})
    monkeypatch.setattr(runner, "resolve_filter", lambda cfg: Wanted())
    Silent.RAISE, Silent.LISTING = NetworkError("Failed to resolve"), []
    with get_session() as s:
        for i in range(30):
            s.add(Company(name=f"b{i}", slug=f"b{i}", source="bamboohr", origin="seed", active=True,
                          consecutive_failures=4))
    probes = []
    watch = runner.NetworkWatch(probe=lambda: probes.append(1) or False, streak=5)
    t = time.monotonic()
    s = runner.scrape_companies(Cfg(**{"limits.deactivate_after_failures": 5}),
                                [CompanyRef(f"b{i}", f"b{i}", "bamboohr") for i in range(30)], watch=watch)
    assert time.monotonic() - t < 60 and probes == [1] and s.network_down
    assert {v for v in s.outcomes.values()} == {("not_attempted", "NetworkDown")}   # none of them "failed"
    with get_session() as sess:
        rows = sess.query(Company).all()
        assert all(r.active for r in rows) and {r.consecutive_failures for r in rows} == {4}   # no strike, no retiring


def test_one_silent_host_with_the_internet_up_fails_that_board_but_never_retires_it(two_call):
    with get_session() as s:
        s.add(Company(name="acme", slug="acme", source="bamboohr", origin="seed", active=True, consecutive_failures=4))
    two_call.RAISE = NetworkError("Connection refused")
    s = runner.scrape_companies(Cfg(**{"limits.deactivate_after_failures": 5}),
                                [CompanyRef("acme", "acme", "bamboohr")],
                                watch=runner.NetworkWatch(probe=lambda: True, streak=1))
    assert s.outcomes[("bamboohr", "acme")] == ("failed", "NetworkError") and not s.network_down
    with get_session() as sess:
        row = sess.query(Company).one()
        assert row.active and row.consecutive_failures == 4


def test_a_board_that_says_it_is_gone_is_still_retired(two_call):
    with get_session() as s:
        s.add(Company(name="acme", slug="acme", source="bamboohr", origin="seed", active=True, consecutive_failures=4))
    from data_engineering.scraper.http_client import FetchError
    two_call.RAISE = FetchError("HTTP 404 for https://acme.bamboohr.com/careers/list")
    read(**{"limits.deactivate_after_failures": 5})
    with get_session() as sess:
        assert sess.query(Company).one().active is False


def test_network_errors_are_told_apart_from_answers(monkeypatch):
    from data_engineering.scraper.http_client import FetchError, HttpSettings, PoliteClient

    def no_dns(self, method, url, timeout=None, **kw):
        raise requests.ConnectionError("Failed to resolve 'x' (NameResolutionError)")
    monkeypatch.setattr(requests.Session, "request", no_dns)
    client = PoliteClient(HttpSettings(user_agent="t", respect_robots=False, min_delay_seconds=0, jitter_seconds=0,
                                       max_retries=0))
    with pytest.raises(NetworkError):
        client.get("https://acme.bamboohr.com/careers/list")
    monkeypatch.setattr(requests.Session, "request",
                        lambda self, m, u, timeout=None, **kw: SimpleNamespace(status_code=503, headers={}))
    with pytest.raises(FetchError) as caught:
        client.get("https://acme.bamboohr.com/careers/list")
    assert not isinstance(caught.value, NetworkError)


# -- quotas -------------------------------------------------------------------------------------------------------

def test_a_quota_takes_the_least_recently_read_first_so_every_board_gets_its_turn(db):
    from devops.discovery_worker import rotation
    now = datetime(2026, 11, 24, 12, 0)
    with get_session() as s:
        for i in range(6):
            s.add(Company(name=f"w{i}", slug=f"w{i}", source="workable", origin="seed", active=True,
                          last_scraped_at=None if i == 5 else now - timedelta(hours=i)))
    refs = [CompanyRef(f"w{i}", f"w{i}", "workable") for i in range(6)]
    first, later = rotation(refs, 3)
    assert [r.slug for r in first] == ["w5", "w4", "w3"] and len(later) == 3        # never read, then oldest
    with get_session() as s:
        for r in first:
            s.query(Company).filter_by(slug=r.slug).update({Company.last_scraped_at: now + timedelta(hours=1)})
    second, _ = rotation(refs, 3)
    assert {r.slug for r in second} == {"w0", "w1", "w2"}                          # their turn next
    assert rotation(refs, 0) == (refs, [])


# -- the planner -----------------------------------------------------------------------------------------------------

def test_the_plan_puts_every_board_in_exactly_one_lane_with_the_approved_splits():
    from devops import discovery_plan as dp
    lanes = dp.load_lanes()
    rows = [{"source": src, "slug": f"{src}{i}", "name": "", "board_url": "", "origin": "", "est_s": 2.0}
            for src, n in (("greenhouse", 30), ("lever", 10), ("ashby", 10), ("workday", 7), ("workable", 400))
            for i in range(n)]
    plan = dp.make_plan(rows, lanes)
    by = {}
    for p in plan["partitions"]:
        by.setdefault(p["provider"], []).append(p)
    assert len(by["greenhouse"]) == 9 and len(by["lever"]) == 4 and len(by["ashby"]) == 4   # 26 Nov 2026
    assert all(p["workers"] == 1 for site in ("greenhouse", "lever", "ashby") for p in by[site])   # one at a time
    # Every site in the cloud since 25 Nov 2026: Workday too, nothing on a Mac lane.
    assert len(by["workday"]) == 2 and plan["mac_lanes"] == {}
    assert by["workable"][0]["quota"] == 150
    assert plan["boards"] == len(rows)
    bad = json.loads(json.dumps(plan))
    bad["partitions"][0]["boards"].append(bad["partitions"][1]["boards"][0])
    with pytest.raises(ValueError):
        dp.check_assignment(rows, bad, lanes)


# -- the merge: index, equivalence, retention -----------------------------------------------------------------------

def _db(path: Path, boards, run="old", log_days=40, rows_per_day=2):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT, slug TEXT, source TEXT, board_url TEXT,
            origin TEXT, active BOOLEAN, consecutive_failures INTEGER DEFAULT 0, last_scraped_at DATETIME,
            UNIQUE (source, slug));
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, source TEXT, company TEXT, company_slug TEXT, external_id TEXT,
            title TEXT, is_open BOOLEAN, closed_at DATETIME, last_seen_at DATETIME,
            UNIQUE (source, company_slug, external_id));
        CREATE TABLE scrape_log (id INTEGER PRIMARY KEY, run_id TEXT, source TEXT, company_slug TEXT, ok BOOLEAN,
            jobs_seen INTEGER, duration_seconds FLOAT, created_at DATETIME);
        CREATE INDEX ix_scrape_log_source ON scrape_log (source);
    """)
    now = datetime(2026, 11, 24, 12)
    for source, slug in boards:
        con.execute("INSERT INTO companies (name, slug, source, active, last_scraped_at) VALUES (?,?,?,1,?)",
                    (slug, slug, source, str(now - timedelta(days=3))))
        con.execute("INSERT INTO jobs (source, company, company_slug, external_id, title, is_open, last_seen_at) "
                    "VALUES (?,?,?,?,?,1,?)", (source, slug, slug, f"{slug}-1", "Engineer", str(now - timedelta(days=3))))
        for d in range(log_days):
            for k in range(rows_per_day):
                con.execute("INSERT INTO scrape_log (run_id, source, company_slug, ok, jobs_seen, duration_seconds, "
                            "created_at) VALUES (?,?,?,1,1,?,?)", (f"{run}{d}", source, slug, 1.5,
                                                                   str(now - timedelta(days=log_days - d, hours=k))))
    con.commit()
    con.close()
    return path


def _part(tmp: Path, manifest, part, boards, run="new"):
    from devops import discovery_worker as dw
    path = tmp / f"part-{part['id']}.db"
    _db(path, boards, run=run, log_days=1, rows_per_day=1)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE boards (source TEXT, slug TEXT, status TEXT, error_type TEXT, listing TEXT, listed "
                "INTEGER, relevant INTEGER, described INTEGER, pending INTEGER, skipped INTEGER, PRIMARY KEY (source, slug))")
    for source, slug in boards:
        con.execute("INSERT INTO boards VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (source, slug, "partial" if slug.endswith("0") else "complete", "",
                     "complete", 10, 4, 3 if slug.endswith("0") else 4, 1 if slug.endswith("0") else 0, 6))
    con.execute("UPDATE companies SET last_scraped_at = ?", (str(datetime(2026, 11, 24, 13)),))
    con.execute("UPDATE jobs SET last_seen_at = ?", (str(datetime(2026, 11, 24, 13)),))
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    for k, v in (("plan_id", manifest["plan_id"]), ("partition", part["id"])):
        con.execute("INSERT INTO meta VALUES (?, ?)", (k, json.dumps(v)))
    con.commit()
    con.close()
    return path


def _manifest(boards, mac=None):
    part = {"id": "greenhouse-1of1", "provider": "greenhouse", "run": True,
            "boards": [{"source": s, "slug": b} for s, b in boards]}
    return {"plan_id": "p1", "partitions": [part], "mac_lanes": mac or {}}, part


def test_the_indexed_merge_gives_the_same_database_and_uses_its_index(tmp_path):
    from devops import discovery_merge as dm
    boards = [("greenhouse", f"g{i}") for i in range(20)]
    manifest, part = _manifest(boards)
    results = {}
    for name, with_index in (("old", False), ("new", True)):
        base = _db(tmp_path / f"{name}.db", boards)
        parts = {part["id"]: _part(tmp_path / name, manifest, part, boards) if (tmp_path / name).mkdir() is None
                 else None}
        if not with_index:
            real = dm.LOG_INDEX
            dm.LOG_INDEX = "SELECT 1"
        try:
            cov = dm.merge(base, manifest, parts)
        finally:
            if not with_index:
                dm.LOG_INDEX = real
        con = sqlite3.connect(base)
        results[name] = (cov["partitions"][0]["merged"],
                         con.execute("SELECT source, company_slug, external_id, is_open, last_seen_at FROM jobs "
                                     "ORDER BY 1, 2, 3").fetchall(),
                         con.execute("SELECT run_id, source, company_slug FROM scrape_log ORDER BY 1, 2, 3").fetchall())
        if with_index:
            plan = " ".join(str(r) for r in con.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM scrape_log m WHERE m.run_id = 'x' AND m.source = 'greenhouse' "
                "AND m.company_slug = 'g1'"))
            assert "ix_scrape_log_run_board" in plan
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        con.close()
    assert results["old"] == results["new"]                 # the same merge, row for row
    # Index creation is idempotent: merging again creates nothing new and changes nothing.
    again = dm.merge(tmp_path / "new.db", manifest, {part["id"]: tmp_path / "new" / f"part-{part['id']}.db"})
    assert again["partitions"][0]["merged"]["logged"] == 0


def test_log_retention_keeps_thirty_days_and_each_boards_newest_row_and_touches_no_posting(tmp_path):
    from devops import discovery_merge as dm
    boards = [("greenhouse", "g1"), ("greenhouse", "quiet")]
    base = _db(tmp_path / "base.db", boards, log_days=60)
    con = sqlite3.connect(base)
    con.execute("DELETE FROM scrape_log WHERE company_slug = 'quiet' AND id != "
                "(SELECT MIN(id) FROM scrape_log WHERE company_slug = 'quiet')")   # read once, two months ago
    con.commit()
    jobs_before = con.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    newest_quiet = con.execute("SELECT MAX(id) FROM scrape_log WHERE company_slug = 'quiet'").fetchone()[0]
    con.close()
    manifest, part = _manifest([("greenhouse", "g1")])
    (tmp_path / "p").mkdir()
    cov = dm.merge(base, manifest, {part["id"]: _part(tmp_path / "p", manifest, part, [("greenhouse", "g1")])},
                   now=datetime(2026, 11, 24, 14, tzinfo=timezone.utc), prune_log_days=30)
    con = sqlite3.connect(base)
    oldest = con.execute("SELECT MIN(created_at) FROM scrape_log WHERE company_slug = 'g1'").fetchone()[0]
    assert oldest >= str(datetime(2026, 10, 25))
    assert con.execute("SELECT COUNT(*) FROM scrape_log WHERE id = ?", (newest_quiet,)).fetchone()[0] == 1
    assert con.execute("SELECT * FROM jobs WHERE company_slug = 'quiet'").fetchall() == \
        [j for j in jobs_before if j[3] == "quiet"]
    assert cov["pruned_log_rows"] > 0 and "vacuum_s" in cov["timings"]
    con.close()


def test_without_the_flag_nothing_is_pruned(tmp_path):
    from devops import discovery_merge as dm
    base = _db(tmp_path / "base.db", [("greenhouse", "g1")], log_days=60)
    before = sqlite3.connect(base).execute("SELECT COUNT(*) FROM scrape_log").fetchone()[0]
    manifest, part = _manifest([("greenhouse", "g1")])
    (tmp_path / "p").mkdir()
    dm.merge(base, manifest, {part["id"]: _part(tmp_path / "p", manifest, part, [("greenhouse", "g1")])})
    assert sqlite3.connect(base).execute("SELECT COUNT(*) FROM scrape_log").fetchone()[0] == before + 1


# -- one coverage report ------------------------------------------------------------------------------------------------

def test_the_board_counts_are_exclusive_and_add_up_and_postings_are_counted_apart():
    from devops import discovery_merge as dm
    outcomes = [["s", "a", "complete", "", "p"], ["s", "b", "partial", "", "p"], ["s", "c", "failed", "HTTPError", "p"],
                ["s", "d", "rate_limited", "RateLimited", "p"], ["s", "e", "not_attempted", "ProviderQuota", "p"],
                ["s", "f", "not_attempted", "NetworkDown", "p"], ["s", "g", "timed_out", "TimeBudget", "p"],
                ["s", "h", "not_attempted", "PartitionFailed", "p"]]
    details = [["s", "a", "complete", 10, 2, 2, 0, 8], ["s", "b", "incomplete", 1000, 70, 60, 10, 930]]
    cov = {"outcomes": outcomes, "details": details, "partitions": [{"status": "PARTIAL"}],
           "mac_lanes": {"workday": 2000}}
    t = dm.totals(cov)
    assert t["reconciles"] and t["boards_planned_cloud"] == 8 and t["boards_planned_mac"] == 2000
    assert (t["boards_scanned_in_full"], t["boards_read_in_part"], t["boards_failed"], t["boards_rate_limited"],
            t["boards_deferred_quota"], t["boards_deferred_network"], t["boards_timed_out"],
            t["boards_not_attempted"]) == (1, 1, 1, 1, 1, 1, 1, 1)
    assert t["boards_listing_incomplete"] == 1 and t["boards_descriptions_pending"] == 1
    assert (t["postings_listed"], t["postings_relevant"], t["postings_described"],
            t["postings_descriptions_pending"], t["postings_skipped_irrelevant"]) == (1010, 72, 62, 10, 938)
    text = dm.summary({**cov, "status": "PARTIAL", "complete_refresh": False, "providers": {}, "partitions": [],
                       "totals": t})
    assert "Skipped as irrelevant (on purpose) | 938" in text and "Planned on the Mac lanes | 2,000" in text
    assert "a" not in [line.split("|")[1].strip() for line in text.splitlines() if line.startswith("| a ")]


# -- the verified result: new fields, and the old format still read --------------------------------------------------

def test_the_result_carries_mac_lanes_and_pending_boards_and_an_old_result_still_verifies(tmp_path):
    from devops import discovery_result as dr
    db = tmp_path / "jobs.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE companies (source TEXT, slug TEXT, last_scraped_at TEXT); "
                      "CREATE TABLE jobs (id INTEGER);"
                      "INSERT INTO companies VALUES ('greenhouse', 'a', '2026-11-24 02:00:00'), "
                      "('greenhouse', 'b', '2026-11-24 02:00:00');")
    con.commit()
    con.close()
    manifest = {"plan_id": "", "mac_lanes": {"workday": 3},
                "partitions": [{"id": "greenhouse-1of1", "provider": "greenhouse", "run": True,
                                "boards": [{"source": "greenhouse", "slug": "a"}, {"source": "greenhouse", "slug": "b"}]}]}
    manifest["plan_id"] = dr.plan_id_of([{"id": p["id"], "boards": [[b["source"], b["slug"]] for b in p["boards"]]}
                                         for p in manifest["partitions"]])
    coverage = {"status": "PARTIAL", "partitions": [{"id": "greenhouse-1of1", "status": "PARTIAL", "reason": ""}]}
    outcomes = [["greenhouse", "a", "complete", "", "greenhouse-1of1"], ["greenhouse", "b", "partial", "", "greenhouse-1of1"]]
    details = [["greenhouse", "a", "complete", 3, 1, 1, 0, 2], ["greenhouse", "b", "incomplete", 1000, 80, 60, 20, 920]]
    result = dr.build(db, manifest, outcomes, coverage, "pw", started="2026-11-24T01:00:00Z", details=details)
    assert result["details"] == [details[1]]                      # only what the Mac must act on
    got = dr.verify(result, db, "pw")
    assert got["mac_lanes"] == {"workday": 3} and got["pending"][("greenhouse", "b")]["pending"] == 20
    tampered = json.loads(json.dumps(result))
    tampered["details"][0][6] = 0
    with pytest.raises(dr.ResultError):
        dr.verify(tampered, db, "pw")                             # signed with the rest
    old = {k: v for k, v in result.items() if k not in ("details", "mac_lanes", "feeds", "signature")}
    old["signature"] = dr.sign(old, "pw")
    got = dr.verify(old, db, "pw")                                 # a result from before Nov 2026 still reads
    assert got["pending"] == {} and got["mac_lanes"] == {}
