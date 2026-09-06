"""Parser tests run entirely offline against captured API payload shapes."""

from datetime import timezone

from db.models import Job
from scraper.base import CompanyRef, extract_requirements, html_to_text
from scraper.discovery import candidate_slugs
from scraper.greenhouse import GreenhouseScraper
from scraper.lever import LeverScraper

ACME = CompanyRef(name="Acme", slug="acme", source="greenhouse")


# --- html_to_text ---------------------------------------------------------

def test_html_to_text_strips_tags_and_unescapes():
    raw = "&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"
    text = html_to_text(raw)
    assert "<p>" not in text
    assert "Build & ship" in text
    assert "- Python" in text


def test_html_to_text_handles_none_and_empty():
    assert html_to_text(None) is None
    assert html_to_text("") is None


# --- requirements extraction ---------------------------------------------

def test_extract_requirements_finds_section():
    jd = "About us\nWe are great.\n\nRequirements:\n- 5 years Python\n- SQL"
    reqs = extract_requirements(jd)
    assert "5 years Python" in reqs


def test_extract_requirements_returns_none_when_absent():
    assert extract_requirements("Just a paragraph with no headings at all.") is None


# --- Greenhouse -----------------------------------------------------------

GREENHOUSE_JOB = {
    "id": 4567,
    "title": "Senior Software Engineer",
    "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/4567",
    "location": {"name": "Remote - US"},
    "content": "&lt;p&gt;Requirements:&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;",
    "updated_at": "2026-08-01T12:00:00-04:00",
}


def test_greenhouse_parse_job():
    job = GreenhouseScraper._parse_job(ACME, GREENHOUSE_JOB)
    assert job.source == "greenhouse"
    assert job.external_id == "4567"
    assert job.title == "Senior Software Engineer"
    assert job.location == "Remote - US"
    assert "Python" in job.description
    assert job.application_url.endswith("/4567")
    assert job.posted_at.year == 2026


def test_greenhouse_handles_missing_location():
    entry = {**GREENHOUSE_JOB, "location": {}}
    assert GreenhouseScraper._parse_job(ACME, entry).location is None


# --- Lever ----------------------------------------------------------------

LEVER_POSTING = {
    "id": "abc-123",
    "text": "Backend Engineer",
    "categories": {"location": "New York", "team": "Platform"},
    "hostedUrl": "https://jobs.lever.co/acme/abc-123",
    "descriptionPlain": "We build things.",
    "createdAt": 1735689600000,  # 2025-01-01T00:00:00Z
    "lists": [
        {"text": "Requirements", "content": "&lt;li&gt;Go and Python&lt;/li&gt;"},
    ],
}


def test_lever_parse_job():
    company = CompanyRef(name="Acme", slug="acme", source="lever")
    job = LeverScraper._parse_job(company, LEVER_POSTING)
    assert job.source == "lever"
    assert job.external_id == "abc-123"
    assert job.title == "Backend Engineer"
    assert job.location == "New York"
    assert "We build things." in job.description
    assert "Go and Python" in job.requirements
    assert job.posted_at.astimezone(timezone.utc).year == 2025


def test_lever_falls_back_to_apply_url():
    company = CompanyRef(name="Acme", slug="acme", source="lever")
    entry = {**LEVER_POSTING, "hostedUrl": None, "applyUrl": "https://jobs.lever.co/acme/x/apply"}
    assert LeverScraper._parse_job(company, entry).application_url.endswith("/apply")


# --- dedupe hashing -------------------------------------------------------

def test_content_hash_is_stable_and_change_sensitive():
    a = Job.make_content_hash("Engineer", "Remote", "body")
    b = Job.make_content_hash("Engineer", "Remote", "body")
    c = Job.make_content_hash("Engineer", "Remote", "body edited")
    assert a == b
    assert a != c


# --- slug derivation ------------------------------------------------------

def test_candidate_slugs_strips_suffixes_and_punctuation():
    slugs = candidate_slugs("Ramp Financial, Inc.", strip_suffixes=["inc", "corp"])
    assert "rampfinancial" in slugs
    assert "ramp-financial" in slugs


def test_candidate_slugs_single_word():
    assert candidate_slugs("Stripe") == ["stripe"]


def test_candidate_slugs_handles_ampersand():
    slugs = candidate_slugs("Johnson & Johnson")
    assert "johnsonandjohnson" in slugs


def test_extract_requirements_stops_at_office_boilerplate():
    jd = (
        "Requirements:\n- 4+ years building backend services\n- Strong Python\n"
        "This role will be based in either New York, San Francisco, or Seattle.\n"
        "We are a hybrid environment requiring three days in the office."
    )
    reqs = extract_requirements(jd)
    assert "backend services" in reqs
    assert "New York" not in reqs
    assert "hybrid" not in reqs


def test_extract_requirements_stops_at_eeo_text():
    jd = "Qualifications:\n- Go and Kubernetes\nWe are an equal opportunity employer."
    reqs = extract_requirements(jd)
    assert "Kubernetes" in reqs
    assert "equal opportunity" not in reqs
