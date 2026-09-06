"""Lever scraper — public postings API.

    GET https://api.lever.co/v0/postings/<slug>?mode=json

Returns a JSON array of postings, each with plain-text and HTML descriptions
plus structured `lists` sections (often the requirements).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from scraper.base import CompanyRef, PortalScraper, RawJob, extract_requirements, html_to_text
from scraper.http_client import FetchError, RobotsDisallowed

log = logging.getLogger(__name__)

API_ROOT = "https://api.lever.co/v0/postings"


class LeverScraper(PortalScraper):
    name = "lever"

    def board_url(self, slug: str) -> str:
        return f"https://jobs.lever.co/{slug}"

    def _postings_url(self, slug: str) -> str:
        return f"{API_ROOT}/{slug}?mode=json"

    def board_exists(self, slug: str) -> bool:
        try:
            resp = self.client.get(self._postings_url(slug))
        except (FetchError, RobotsDisallowed):
            return False
        if resp.status_code != 200:
            return False
        try:
            return isinstance(resp.json(), list)
        except ValueError:
            return False

    def fetch_jobs(self, company: CompanyRef) -> list[RawJob]:
        payload = self.client.get_json(self._postings_url(company.slug))
        if not isinstance(payload, list):
            raise FetchError(f"Unexpected Lever payload for {company.slug}")

        jobs: list[RawJob] = []
        for entry in payload:
            try:
                jobs.append(self._parse_job(company, entry))
            except Exception as exc:  # one malformed posting must not kill the board
                log.warning("Skipping malformed Lever job for %s: %s", company.slug, exc)
        return jobs

    @staticmethod
    def _parse_job(company: CompanyRef, entry: dict) -> RawJob:
        categories = entry.get("categories") or {}
        description = entry.get("descriptionPlain") or html_to_text(entry.get("description"))

        # Lever splits the body into titled `lists` (e.g. "Requirements").
        sections: list[str] = []
        for section in entry.get("lists") or []:
            heading = (section.get("text") or "").strip()
            body = html_to_text(section.get("content")) or ""
            if heading or body:
                sections.append(f"{heading}\n{body}".strip())

        full_description = "\n\n".join(part for part in [description, *sections] if part) or None

        return RawJob(
            source="lever",
            company=company.name,
            company_slug=company.slug,
            external_id=str(entry["id"]),
            title=(entry.get("text") or "").strip(),
            location=categories.get("location"),
            description=full_description,
            requirements=_requirements_from_lists(entry) or extract_requirements(full_description),
            application_url=entry.get("hostedUrl") or entry.get("applyUrl") or "",
            posted_at=_parse_epoch_ms(entry.get("createdAt")),
        )


def _requirements_from_lists(entry: dict) -> str | None:
    """Prefer Lever's own structured section when its heading looks like reqs."""
    wanted = ("requirement", "qualification", "you have", "looking for", "skills")
    for section in entry.get("lists") or []:
        heading = (section.get("text") or "").lower()
        if any(word in heading for word in wanted):
            return html_to_text(section.get("content"))
    return None


def _parse_epoch_ms(value) -> datetime | None:
    """Lever timestamps are epoch milliseconds."""
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
