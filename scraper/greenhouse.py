"""Greenhouse scraper — public job board API.

    GET https://boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true

Returns every posting with full HTML content in one call, so no per-job
requests are needed. No browser required (see README.md §C4).
"""

from __future__ import annotations

import logging
from datetime import datetime

from scraper.base import CompanyRef, PortalScraper, RawJob, extract_requirements, html_to_text
from scraper.http_client import FetchError, RobotsDisallowed

log = logging.getLogger(__name__)

API_ROOT = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseScraper(PortalScraper):
    name = "greenhouse"

    def board_url(self, slug: str) -> str:
        return f"https://job-boards.greenhouse.io/{slug}"

    def _jobs_url(self, slug: str) -> str:
        return f"{API_ROOT}/{slug}/jobs?content=true"

    def board_exists(self, slug: str) -> bool:
        try:
            resp = self.client.get(f"{API_ROOT}/{slug}")
        except (FetchError, RobotsDisallowed):
            return False
        return resp.status_code == 200

    def fetch_jobs(self, company: CompanyRef) -> list[RawJob]:
        payload = self.client.get_json(self._jobs_url(company.slug))
        if not isinstance(payload, dict):
            raise FetchError(f"Unexpected Greenhouse payload for {company.slug}")

        jobs: list[RawJob] = []
        for entry in payload.get("jobs", []) or []:
            try:
                jobs.append(self._parse_job(company, entry))
            except Exception as exc:  # one malformed posting must not kill the board
                log.warning("Skipping malformed Greenhouse job for %s: %s", company.slug, exc)
        return jobs

    @staticmethod
    def _parse_job(company: CompanyRef, entry: dict) -> RawJob:
        description = html_to_text(entry.get("content"))
        location = (entry.get("location") or {}).get("name")

        return RawJob(
            source="greenhouse",
            company=company.name,
            company_slug=company.slug,
            external_id=str(entry["id"]),
            title=(entry.get("title") or "").strip(),
            location=location,
            description=description,
            requirements=extract_requirements(description),
            application_url=entry.get("absolute_url") or "",
            posted_at=_parse_date(entry.get("first_published") or entry.get("updated_at")),
        )


def _parse_date(value: str | None) -> datetime | None:
    """Greenhouse returns ISO-8601, sometimes with a trailing Z."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
