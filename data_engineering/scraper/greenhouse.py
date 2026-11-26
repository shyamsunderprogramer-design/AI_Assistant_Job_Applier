"""Greenhouse scraper — public job board API.

    GET https://boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true

Returns every posting with full HTML content in one call, so no per-job
requests are needed. No browser required (see README.md §C4).
"""

from __future__ import annotations

import logging
from datetime import datetime

from data_engineering.scraper.base import CompanyRef, Listing, PortalScraper, RawJob, extract_requirements, html_to_text
from data_engineering.scraper.http_client import FetchError, RateLimited, RobotsDisallowed

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
        except RateLimited:
            raise            # a refusal, not an answer: never cache it
        except (FetchError, RobotsDisallowed):
            return False
        return resp.status_code == 200

    def fetch_jobs(self, company: CompanyRef) -> list[RawJob]:
        payload = self.client.get_json(self._jobs_url(company.slug))
        if not isinstance(payload, dict):
            raise FetchError(f"Unexpected Greenhouse payload for {company.slug}")

        if not isinstance(payload.get("jobs"), list):
            raise FetchError(f"Unexpected Greenhouse payload for {company.slug}")
        entries = payload["jobs"]
        jobs: list[RawJob] = []
        for entry in entries:
            try:
                jobs.append(self._parse_job(company, entry))
            except Exception as exc:  # one malformed posting must not kill the board
                log.warning("Skipping malformed Greenhouse job for %s: %s", company.slug, exc)
        # Every id it named (one it could not parse is still on the board), and whether that is all of them:
        # an entry without an id, or fewer entries than the board's own total, is not the whole board.
        ids = [str(e["id"]) for e in entries if isinstance(e, dict) and e.get("id") is not None]
        total = (payload.get("meta") or {}).get("total") if isinstance(payload.get("meta"), dict) else None
        return Listing(jobs, complete=not (isinstance(total, int) and total != len(entries)), total=total,
                       ids=ids, malformed=len(entries) - len(ids))

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
