"""Workday scraper tests — offline, no network.

Two of Workday's quirks cost postings silently rather than raising, and both
are pinned here: `total` arrives on the first page only, and a location is
often a count rather than a place.
"""

import pytest

from scraper.base import CompanyRef
from scraper.workday import (
    WorkdayScraper,
    _location,
    make_slug,
    parse_slug,
    slug_from_url,
)


# -- the three-part slug ----------------------------------------------------

def test_a_slug_carries_tenant_datacenter_and_site():
    assert parse_slug("nvidia:wd5:NVIDIAExternalCareerSite") == (
        "nvidia", "wd5", "NVIDIAExternalCareerSite")


def test_a_one_part_slug_is_not_a_workday_board():
    # Greenhouse-style slugs must not be mistaken for Workday ones.
    assert parse_slug("nvidia") is None
    assert parse_slug("") is None


def test_a_slug_without_a_datacenter_is_rejected():
    assert parse_slug("nvidia:careers:Site") is None


def test_the_datacenter_is_normalised():
    assert parse_slug("nvidia:WD5:Site")[1] == "wd5"


def test_round_trip():
    slug = make_slug("bpinternational", "wd3", "bpCareers")
    assert parse_slug(slug) == ("bpinternational", "wd3", "bpCareers")


# -- recovering a board from a URL, which is the only way to find one --------

def test_a_tenant_is_recovered_from_a_job_url():
    assert slug_from_url(
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA/Engineer_JR1"
    ) == "nvidia:wd5:NVIDIAExternalCareerSite"


def test_a_locale_segment_is_not_a_site_name():
    # ".../en-US/Careers" names the locale first; taking it as the site would
    # build a URL that answers nothing.
    assert slug_from_url("https://acme.wd1.myworkdayjobs.com/en-US/AcmeCareers") == \
        "acme:wd1:AcmeCareers"


def test_a_non_workday_url_yields_nothing():
    assert slug_from_url("https://boards.greenhouse.io/acme") is None
    assert slug_from_url("") is None
    assert slug_from_url(None) is None


def test_a_board_url_is_rebuilt_from_the_slug():
    s = WorkdayScraper(client=None)
    assert s.board_url("nvidia:wd5:Site") == "https://nvidia.wd5.myworkdayjobs.com/Site"
    assert s.board_url("not-a-workday-slug") == ""


# -- "6 Locations" is a count, not a place ----------------------------------

def test_a_location_count_is_treated_as_unknown():
    """This one silently dropped every multi-site posting.

    No location filter can match "6 Locations", so a posting spanning several
    offices was rejected before anyone saw it — including a Senior SRE role.
    An unknown location is kept for review; a wrong one is not.
    """
    assert _location("6 Locations") is None
    assert _location("1 Location") is None
    assert _location("   3 locations  ") is None


def test_a_real_place_survives():
    assert _location("US-CA-Santa Clara") == "US-CA-Santa Clara"
    assert _location("Remote, United States") == "Remote, United States"


def test_an_empty_location_is_unknown():
    assert _location("") is None
    assert _location(None) is None


# -- pagination: `total` is sent once, then zero ----------------------------

class FakeClient:
    """Serves pages the way Workday does, including the total-once quirk."""

    def __init__(self, count: int, page_size: int = 20):
        self.count = count
        self.page_size = page_size
        self.pages_served = 0
        self.details_served = 0

    def post(self, url, json=None, **kw):
        offset = json["offset"]
        limit = json["limit"]
        items = [
            {"title": f"Engineer {i}", "locationsText": "3 Locations",
             "externalPath": f"/job/x/Engineer-{i}_JR{i}"}
            for i in range(offset, min(offset + limit, self.count))
        ]
        self.pages_served += 1
        # The quirk: a total on the first page, zero on every page after.
        total = self.count if offset == 0 else 0
        return _Resp({"total": total, "jobPostings": items})

    def get(self, url, **kw):
        self.details_served += 1
        return _Resp({"jobPostingInfo": {
            "jobDescription": "<p>Run things</p>", "jobReqId": "JR9",
            "startDate": "2026-09-01", "location": "US-CA-Santa Clara",
            "externalUrl": "https://example.com/apply",
        }})


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def company() -> CompanyRef:
    return CompanyRef(name="NVIDIA", slug="nvidia:wd5:Site", source="workday")


def test_pagination_does_not_stop_at_the_first_page():
    """`total` is 0 after page one; trusting it per page stopped at 40 of 2000."""
    client = FakeClient(count=250)
    stubs = WorkdayScraper(client).stub_jobs(company())

    assert len(stubs) == 250


def test_pagination_stops_once_everything_is_collected():
    client = FakeClient(count=40)
    WorkdayScraper(client).stub_jobs(company())

    # 2 pages of results, then it knows it is done — no endless walk.
    assert client.pages_served <= 3


def test_a_short_board_is_read_completely():
    stubs = WorkdayScraper(FakeClient(count=7)).stub_jobs(company())
    assert len(stubs) == 7


# -- descriptions cost a request each, so only filtered rows get one --------

def test_stubs_carry_no_description():
    stubs = WorkdayScraper(FakeClient(count=5)).stub_jobs(company())

    assert all(s.description is None for s in stubs)
    assert all(s.title for s in stubs)


def test_details_are_fetched_only_for_what_survives_filtering():
    client = FakeClient(count=100)
    scraper = WorkdayScraper(client)
    stubs = scraper.stub_jobs(company())

    scraper.fill_details(company(), stubs[:3])      # as if a filter kept 3

    assert client.details_served == 3               # not 100


def test_a_detail_fills_description_location_and_the_requisition_id():
    client = FakeClient(count=1)
    scraper = WorkdayScraper(client)
    filled = scraper.fill_details(company(), scraper.stub_jobs(company()))

    job = filled[0]
    assert "Run things" in job.description
    assert job.location == "US-CA-Santa Clara"      # the list said "3 Locations"
    assert job.external_id == "JR9"                 # stable across re-postings
    assert job.application_url == "https://example.com/apply"


def test_a_bad_slug_raises_rather_than_scraping_nothing():
    from scraper.http_client import FetchError

    with pytest.raises(FetchError):
        WorkdayScraper(FakeClient(count=1)).stub_jobs(
            CompanyRef(name="X", slug="just-a-name", source="workday"))
