"""Recruitee scraper — public careers API, one call per board.

    GET https://<slug>.recruitee.com/api/offers/   -> {"offers": [...]}

Each offer carries its full description, so there is no second call. Read
live from bunq's board in Sep 2026. A slug that is no one's board answers 404.
robots.txt on *.recruitee.com permits it.
"""

from __future__ import annotations

from datetime import datetime

from data_engineering.scraper.base import CompanyRef, PortalScraper, RawJob, extract_requirements, html_to_text
from data_engineering.scraper.http_client import FetchError, RateLimited, RobotsDisallowed


def _when(value):
    try:
        return datetime.fromisoformat(str(value).replace(" UTC", "+00:00").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


class RecruiteeScraper(PortalScraper):
    name = "recruitee"

    def _api(self, slug: str) -> str:
        return f"https://{slug}.recruitee.com/api/offers/"

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.recruitee.com/"

    def board_exists(self, slug: str) -> bool:
        try:
            resp = self.client.get(self._api(slug))
        except RateLimited:
            raise            # a refusal, not an answer: never cache it
        except (FetchError, RobotsDisallowed):
            return False
        if resp.status_code != 200:
            return False
        try:
            return isinstance(resp.json().get("offers"), list)
        except (ValueError, AttributeError):
            return False

    def fetch_jobs(self, company: CompanyRef) -> list[RawJob]:
        payload = self.client.get_json(self._api(company.slug))
        if not isinstance(payload, dict) or not isinstance(payload.get("offers"), list):
            raise FetchError(f"Unexpected Recruitee payload for {company.slug}")
        jobs = []
        for offer in payload["offers"]:
            if offer.get("status") not in (None, "published"):
                continue
            place = ", ".join(p for p in (offer.get("city"), offer.get("country")) if p)
            if offer.get("remote"):
                place = f"Remote{f' ({place})' if place else ''}"
            description = html_to_text(
                f"{offer.get('description') or ''}\n{offer.get('requirements') or ''}")
            url = offer.get("careers_url") or offer.get("careers_apply_url")
            if not (offer.get("id") and offer.get("title") and url):
                continue
            jobs.append(RawJob(
                source=self.name, company=offer.get("company_name") or company.name,
                company_slug=company.slug, external_id=str(offer["id"]),
                title=offer["title"].strip(), location=place or None,
                description=description, application_url=url,
                posted_at=_when(offer.get("published_at") or offer.get("created_at")),
                requirements=extract_requirements(description),
            ))
        return jobs
