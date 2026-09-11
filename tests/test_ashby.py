"""Ashby scraper tests — offline, against captured payload shapes.

Ashby reports remote-ness separately from the location string, which is the
detail most likely to be got wrong: a fully remote role reads "New York, NY
(HQ)" and a "remote" filter would throw it away.
"""

import pytest

from scraper.ashby import AshbyScraper, _location_for, _parse_date
from scraper.base import CompanyRef

COMPANY = CompanyRef(name="Ramp", slug="ramp", source="ashby")


def entry(**overrides):
    """One posting, shaped like the live API returns it."""
    base = {
        "id": "34413f8d-26bf-4bbc-8ade-eb309a0e2245",
        "title": "  Security Engineer, Cloud  ",
        "location": "New York, NY (HQ)",
        "isRemote": True,
        "workplaceType": "Hybrid",
        "isListed": True,
        "publishedAt": "2026-04-07T17:12:35.753+00:00",
        "jobUrl": "https://jobs.ashbyhq.com/ramp/34413f8d",
        "applyUrl": "https://jobs.ashbyhq.com/ramp/34413f8d/application",
        "descriptionPlain": "Requirements:\n- Terraform\n- Kubernetes",
        "descriptionHtml": "<p>Requirements:</p><ul><li>Terraform</li></ul>",
        "compensation": {"compensationTierSummary": "$211.4K – $290.6K • Offers Equity"},
        "secondaryLocations": [{"location": "Remote (Canada)"}],
    }
    base.update(overrides)
    return base


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def get_json(self, url, **kwargs):
        self.urls.append(url)
        return self.payload


def scraper_for(payload):
    return AshbyScraper(FakeClient(payload))


# -- parsing ---------------------------------------------------------------

def test_parses_a_posting():
    jobs = scraper_for({"jobs": [entry()]}).fetch_jobs(COMPANY)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "ashby"
    assert job.company == "Ramp"
    assert job.title == "Security Engineer, Cloud"  # stripped
    assert job.external_id == "34413f8d-26bf-4bbc-8ade-eb309a0e2245"
    assert job.application_url.endswith("/application")


def test_requirements_are_extracted():
    job = scraper_for({"jobs": [entry()]}).fetch_jobs(COMPANY)[0]
    assert job.requirements and "Terraform" in job.requirements


def test_falls_back_to_html_when_plain_text_is_absent():
    job = scraper_for({"jobs": [entry(descriptionPlain=None)]}).fetch_jobs(COMPANY)[0]
    assert job.description and "Terraform" in job.description


def test_compensation_is_kept():
    """Ashby is the only one of the three ATSs that volunteers salary."""
    job = scraper_for({"jobs": [entry()]}).fetch_jobs(COMPANY)[0]
    assert job.description.startswith("Compensation: $211.4K")


def test_no_compensation_section_when_absent():
    job = scraper_for({"jobs": [entry(compensation=None)]}).fetch_jobs(COMPANY)[0]
    assert not job.description.startswith("Compensation:")


# -- the location trap -----------------------------------------------------

def test_remote_flag_is_folded_into_the_location():
    """A remote role reading "New York, NY (HQ)" would fail a remote filter."""
    assert "Remote" in _location_for(entry())


def test_workplace_type_is_included():
    assert "Hybrid" in _location_for(entry())


def test_onsite_role_is_not_labelled_remote():
    assert "Remote" not in _location_for(entry(isRemote=False, workplaceType="Onsite"))


def test_secondary_locations_are_excluded_from_the_filter_string():
    """A US role also open to "Remote (Canada)" must not be killed by a
    "canada" location exclusion — exclusions win, so it would lose the job."""
    assert "Canada" not in _location_for(entry())


def test_location_is_none_when_nothing_is_known():
    assert _location_for({"location": "", "isRemote": False, "workplaceType": ""}) is None


def test_workplace_type_is_not_duplicated():
    location = _location_for(entry(location="Remote", isRemote=True, workplaceType="Remote"))
    assert location.lower().count("remote") == 1


# -- listing state ---------------------------------------------------------

def test_unlisted_postings_are_skipped():
    """Ashby marks a posting it is not publicly showing — filled, or not open."""
    payload = {"jobs": [entry(id="a"), entry(id="b", isListed=False)]}
    jobs = scraper_for(payload).fetch_jobs(COMPANY)
    assert [j.external_id for j in jobs] == ["a"]


def test_missing_islisted_is_treated_as_listed():
    payload = {"jobs": [entry(**{"isListed": None})]}
    assert len(scraper_for(payload).fetch_jobs(COMPANY)) == 1


# -- resilience ------------------------------------------------------------

def test_one_malformed_posting_does_not_kill_the_board():
    payload = {"jobs": [entry(id="ok"), {"no_id": True}, entry(id="ok2")]}
    jobs = scraper_for(payload).fetch_jobs(COMPANY)
    assert [j.external_id for j in jobs] == ["ok", "ok2"]


def test_unexpected_payload_raises():
    from scraper.http_client import FetchError

    with pytest.raises(FetchError):
        scraper_for([]).fetch_jobs(COMPANY)


def test_empty_board_returns_no_jobs():
    assert scraper_for({"jobs": []}).fetch_jobs(COMPANY) == []


# -- urls and dates --------------------------------------------------------

def test_board_url():
    assert AshbyScraper(None).board_url("ramp") == "https://jobs.ashbyhq.com/ramp"


def test_compensation_is_requested():
    s = scraper_for({"jobs": []})
    s.fetch_jobs(COMPANY)
    assert "includeCompensation=true" in s.client.urls[0]


def test_parses_iso_date_with_offset():
    assert _parse_date("2026-04-07T17:12:35.753+00:00").year == 2026


def test_bad_date_is_none_not_an_error():
    assert _parse_date("not a date") is None
    assert _parse_date(None) is None
