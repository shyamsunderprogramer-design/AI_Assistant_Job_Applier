"""Ashby scraper — public job board API.

    GET https://api.ashbyhq.com/posting-api/job-board/<slug>?includeCompensation=true

Returns every posting with plain-text and HTML descriptions in one call, like
Greenhouse and Lever. Ashby is the third big ATS for startups and scale-ups, and
the two already supported miss it entirely — companies on Ashby were simply
invisible to this tool.

Ashby's payload is richer than the other two: it states explicitly whether a
role is remote, its workplace type, and (opted in) its salary range. That is
worth having, so compensation is appended to the description rather than
discarded.
"""

from __future__ import annotations

import logging
from datetime import datetime

from scraper.base import CompanyRef, PortalScraper, RawJob, extract_requirements, html_to_text
from scraper.http_client import FetchError, RobotsDisallowed

log = logging.getLogger(__name__)

API_ROOT = "https://api.ashbyhq.com/posting-api/job-board"


class AshbyScraper(PortalScraper):
    name = "ashby"

    def board_url(self, slug: str) -> str:
        return f"https://jobs.ashbyhq.com/{slug}"

    def _jobs_url(self, slug: str) -> str:
        return f"{API_ROOT}/{slug}?includeCompensation=true"

    def board_exists(self, slug: str) -> bool:
        try:
            resp = self.client.get(self._jobs_url(slug))
        except (FetchError, RobotsDisallowed):
            return False
        if resp.status_code != 200:
            return False
        try:
            return isinstance(resp.json(), dict) and "jobs" in resp.json()
        except ValueError:
            return False

    def fetch_jobs(self, company: CompanyRef) -> list[RawJob]:
        payload = self.client.get_json(self._jobs_url(company.slug))
        if not isinstance(payload, dict) or "jobs" not in payload:
            raise FetchError(f"Unexpected Ashby payload for {company.slug}")

        jobs: list[RawJob] = []
        for entry in payload.get("jobs") or []:
            # Ashby marks a posting it is not publicly showing. Respect that:
            # it is either filled or not open yet, and applying is pointless.
            if entry.get("isListed") is False:
                continue
            try:
                jobs.append(self._parse_job(company, entry))
            except Exception as exc:  # one malformed posting must not kill the board
                log.warning("Skipping malformed Ashby job for %s: %s", company.slug, exc)
        return jobs

    @staticmethod
    def _parse_job(company: CompanyRef, entry: dict) -> RawJob:
        description = entry.get("descriptionPlain") or html_to_text(entry.get("descriptionHtml"))

        # Salary is the single most useful fact a board can volunteer, and
        # Ashby is the only one of the three that does. Keep it at the top of
        # the description so it survives into the Excel sheet.
        summary = (entry.get("compensation") or {}).get("compensationTierSummary")
        if summary and description:
            description = f"Compensation: {summary}\n\n{description}"

        return RawJob(
            source="ashby",
            company=company.name,
            company_slug=company.slug,
            external_id=str(entry["id"]),
            title=(entry.get("title") or "").strip(),
            location=_location_for(entry),
            description=description,
            requirements=extract_requirements(description),
            application_url=entry.get("applyUrl") or entry.get("jobUrl") or "",
            posted_at=_parse_date(entry.get("publishedAt")),
        )


def _location_for(entry: dict) -> str | None:
    """Compose the location string the filters will see.

    Ashby reports remote-ness separately from the location, so a fully remote
    role can read "New York, NY (HQ)" and be rejected by a "remote" filter that
    should have kept it. Fold the flags in.

    `secondaryLocations` is deliberately NOT included. Location exclusions win
    over inclusions, so a US role that also lists "Remote (Canada)" would be
    dropped by a "canada" exclusion — losing a job the user can actually do.
    """
    parts: list[str] = []

    def add(value: str | None) -> None:
        """Append a part unless an existing one already says it.

        A board whose location is literally "Remote" plus isRemote=True would
        otherwise read "Remote | Remote".
        """
        value = (value or "").strip()
        if not value:
            return
        lowered = value.lower()
        if any(lowered in existing.lower() for existing in parts):
            return
        parts.append(value)

    add(entry.get("location"))
    if entry.get("isRemote"):
        add("Remote")
    add(entry.get("workplaceType"))
    return " | ".join(parts) or None


def _parse_date(value: str | None) -> datetime | None:
    """Ashby returns ISO-8601 with an offset, e.g. 2026-04-07T17:12:35.753+00:00."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
