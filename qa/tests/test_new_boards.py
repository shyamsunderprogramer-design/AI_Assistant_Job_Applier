"""Recruitee, Teamtailor and BambooHR boards — offline, with payloads shaped
like the live ones read in Sep 2026 (bunq, Polestar, Trax Technologies)."""

import pytest

from data_engineering.scraper.bamboohr import BambooHRScraper
from data_engineering.scraper.base import CompanyRef
from data_engineering.scraper.http_client import RateLimited
from data_engineering.scraper.recruitee import RecruiteeScraper
from data_engineering.scraper.runner import SCRAPER_TYPES
from data_engineering.scraper.teamtailor import TeamtailorScraper


class Resp:
    def __init__(self, status=200, body=None, text="", ctype="application/json"):
        self.status_code, self._body, self.text = status, body, text
        self.headers = {"content-type": ctype}

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class Client:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _match(self, url):
        self.calls.append(url)
        for fragment, answer in self.routes.items():
            if fragment in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return Resp(404, {"error": "not found"})

    def get(self, url, **kw):
        return self._match(url)

    def get_json(self, url, **kw):
        return self._match(url).json()


def test_all_three_are_registered_boards():
    assert {"recruitee", "teamtailor", "bamboohr"} <= set(SCRAPER_TYPES)


# -- Recruitee ---------------------------------------------------------------

OFFER = {"id": 2201, "title": "Senior DevOps Engineer", "status": "published",
         "company_name": "bunq", "city": "Amsterdam", "country": "Netherlands", "remote": True,
         "description": "<p>Run Kubernetes</p>", "requirements": "<ul><li>Terraform</li></ul>",
         "careers_url": "https://bunq.recruitee.com/o/devops", "published_at": "2026-09-20 10:00:00 UTC"}


def test_recruitee_reads_offers_with_their_descriptions():
    s = RecruiteeScraper(Client({"bunq.recruitee.com/api/offers": Resp(body={"offers": [OFFER]})}))
    [job] = s.fetch_jobs(CompanyRef("bunq", "bunq", "recruitee"))
    assert job.location == "Remote (Amsterdam, Netherlands)"
    assert "Kubernetes" in job.description and "Terraform" in job.description
    assert job.posted_at.year == 2026


def test_recruitee_skips_unpublished_offers():
    s = RecruiteeScraper(Client({"offers": Resp(body={"offers": [{**OFFER, "status": "closed"}]})}))
    assert s.fetch_jobs(CompanyRef("bunq", "bunq", "recruitee")) == []


def test_recruitee_board_exists_only_for_a_real_board():
    s = RecruiteeScraper(Client({"bunq.recruitee.com": Resp(body={"offers": []})}))
    assert s.board_exists("bunq") and not s.board_exists("nobody")


# -- Teamtailor --------------------------------------------------------------

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel>
<item><title>Platform Engineer</title>
<description>&lt;p&gt;Build the platform&lt;/p&gt;</description>
<pubDate>Wed, 17 Sep 2026 15:42:18 +0100</pubDate>
<link>https://polestar.teamtailor.com/jobs/1-platform-engineer</link>
<remoteStatus>hybrid</remoteStatus><guid>1cece87f</guid><company_name>Polestar</company_name>
<tt:locations><tt:location><tt:name>Gothenburg</tt:name><tt:city>Gothenburg</tt:city><tt:country>Sweden</tt:country></tt:location></tt:locations>
</item></channel></rss>"""


def test_teamtailor_reads_the_rss_feed():
    s = TeamtailorScraper(Client({"polestar.teamtailor.com/jobs.rss": Resp(text=FEED, ctype="application/rss+xml")}))
    [job] = s.fetch_jobs(CompanyRef("Polestar", "polestar", "teamtailor"))
    assert job.title == "Platform Engineer" and job.external_id == "1cece87f"
    assert job.location == "Hybrid (Gothenburg, Sweden)"
    assert job.description == "Build the platform"


def test_teamtailor_board_exists_needs_a_real_feed():
    s = TeamtailorScraper(Client({"polestar.teamtailor.com": Resp(text=FEED, ctype="application/rss+xml")}))
    assert s.board_exists("polestar") and not s.board_exists("nobody")


# -- BambooHR -----------------------------------------------------------------

LISTING = {"meta": {"totalCount": 2}, "result": [
    {"id": "475", "jobOpeningName": "Site Reliability Engineer", "location": {"city": "Austin", "state": "TX"},
     "atsLocation": {"country": None}, "isRemote": None, "locationType": "0"},
    {"id": "547", "jobOpeningName": "Product Manager", "location": {"city": "Cebu City", "state": "Cebu"},
     "atsLocation": {}, "isRemote": True, "locationType": "1"}]}
DETAIL = {"result": {"jobOpening": {
    "jobOpeningName": "Site Reliability Engineer", "description": "<p>Keep it up</p>",
    "datePosted": "2026-09-10", "location": {"city": "Austin", "state": "TX"},
    "jobOpeningShareUrl": "https://traxtech.bamboohr.com/careers/475", "locationType": "0"}}}


def bamboo(routes):
    return BambooHRScraper(Client(routes))


def test_bamboohr_lists_without_descriptions_then_fills_only_survivors():
    s = bamboo({"/careers/list": Resp(body=LISTING), "/careers/475/detail": Resp(body=DETAIL)})
    company = CompanyRef("Trax", "traxtech", "bamboohr")
    stubs = s.stub_jobs(company)
    assert [j.location for j in stubs] == ["Austin, TX", "Remote (Cebu City, Cebu)"]
    assert all(j.description is None for j in stubs)
    [filled] = s.fill_details(company, stubs[:1])
    assert filled.description == "Keep it up" and filled.posted_at.day == 10
    assert not any("/547/" in url for url in s.client.calls)       # never fetched


def test_bamboohr_marketing_redirect_is_not_a_board():
    s = bamboo({"acme.bamboohr.com": Resp(text="<html>", ctype="text/html")})
    assert not s.board_exists("acme")
    s = bamboo({"traxtech.bamboohr.com": Resp(body=LISTING)})
    assert s.board_exists("traxtech")


def test_a_rate_limit_is_never_read_as_no_board():
    for scraper in (RecruiteeScraper, TeamtailorScraper, BambooHRScraper):
        with pytest.raises(RateLimited):
            scraper(Client({"": RateLimited("slow down")})).board_exists("acme")
