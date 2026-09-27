"""Breezy HR scraper — each board's public JSON, descriptions included.

    GET https://<slug>.breezy.hr/json?verbose=true

`verbose=true` adds the full description, so one call covers the board. Read
live from Netrix Global's board in Sep 2026. A slug that is no one's board
answers 404 ("Career portal not found").
"""

from __future__ import annotations

from datetime import datetime

from data_engineering.scraper.base import CompanyRef, PortalScraper, RawJob, extract_requirements, html_to_text
from data_engineering.scraper.http_client import FetchError, RateLimited, RobotsDisallowed


def _place(loc: dict) -> str | None:
    loc = loc or {}
    city, country = loc.get("city") or "", (loc.get("country") or {}).get("name") or ""
    state = (loc.get("state") or {}).get("name") if isinstance(loc.get("state"), dict) else loc.get("state")
    place = ", ".join(p for p in (city, state, country) if p) or loc.get("name") or ""
    if loc.get("is_remote"):
        place = f"Remote{f' ({place})' if place else ''}"
    return place or None


class BreezyScraper(PortalScraper):
    name = "breezy"

    def _api(self, slug: str) -> str:
        return f"https://{slug}.breezy.hr/json"

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.breezy.hr/"

    def board_exists(self, slug: str) -> bool:
        try:
            resp = self.client.get(self._api(slug))
        except RateLimited:
            raise            # a refusal, not an answer: never cache it
        except (FetchError, RobotsDisallowed):
            return False
        if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
            return False
        try:
            return isinstance(resp.json(), list)
        except ValueError:
            return False

    def fetch_jobs(self, company: CompanyRef) -> list[RawJob]:
        payload = self.client.get_json(self._api(company.slug), params={"verbose": "true"})
        if not isinstance(payload, list):
            raise FetchError(f"Unexpected Breezy payload for {company.slug}")
        jobs = []
        for entry in payload:
            job_id, title, url = entry.get("id"), (entry.get("name") or "").strip(), entry.get("url")
            if not (job_id and title and url):
                continue
            description = html_to_text(entry.get("description") or "")
            try:
                posted = datetime.fromisoformat(str(entry.get("published_date")).replace("Z", "+00:00"))
            except ValueError:
                posted = None
            jobs.append(RawJob(
                source=self.name, company=(entry.get("company") or {}).get("name") or company.name,
                company_slug=company.slug, external_id=str(job_id), title=title,
                location=_place(entry.get("location")), description=description or None,
                application_url=url, posted_at=posted,
                requirements=extract_requirements(description) if description else None,
            ))
        return jobs
