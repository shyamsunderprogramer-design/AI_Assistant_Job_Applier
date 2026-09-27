"""BambooHR scraper — the careers page's own JSON, two calls like Workable.

    GET https://<slug>.bamboohr.com/careers/list          -> {"result": [...]}
    GET https://<slug>.bamboohr.com/careers/<id>/detail   -> {"result": {"jobOpening": ...}}

Both answer JSON only when asked for it (Accept: application/json), as the
careers page's own script does; a slug that is no customer's redirects to
bamboohr.com's marketing site instead. The listing has no description, so the
detail is fetched only for postings that survive the title filter. Read live
from Trax Technologies' board in Sep 2026.
"""

from __future__ import annotations

from datetime import datetime, timezone

from data_engineering.scraper.base import CompanyRef, PortalScraper, RawJob, extract_requirements, html_to_text
from data_engineering.scraper.http_client import FetchError, RateLimited, RobotsDisallowed

JSON = {"Accept": "application/json"}


def _place(entry: dict) -> str | None:
    loc = entry.get("location") or {}
    ats = entry.get("atsLocation") or {}
    city = (loc.get("city") or ats.get("city") or "").strip()
    state = (loc.get("state") or ats.get("state") or ats.get("province") or "").strip()
    country = (ats.get("country") or "").strip()
    place = ", ".join(p for p in (city, state, country) if p)
    if entry.get("isRemote") or str(entry.get("locationType")) == "1":
        place = f"Remote{f' ({place})' if place else ''}"
    elif str(entry.get("locationType")) == "2":
        place = f"Hybrid{f' ({place})' if place else ''}"
    return place or None


class BambooHRScraper(PortalScraper):
    name = "bamboohr"

    def _list(self, slug: str) -> str:
        return f"https://{slug}.bamboohr.com/careers/list"

    def _detail(self, slug: str, job_id: str) -> str:
        return f"https://{slug}.bamboohr.com/careers/{job_id}/detail"

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.bamboohr.com/careers"

    def board_exists(self, slug: str) -> bool:
        try:
            resp = self.client.get(self._list(slug), headers=JSON)
        except RateLimited:
            raise            # a refusal, not an answer: never cache it
        except (FetchError, RobotsDisallowed):
            return False
        if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
            return False      # a non-customer lands on the marketing site
        try:
            return isinstance(resp.json().get("result"), list)
        except (ValueError, AttributeError):
            return False

    def _to_job(self, company: CompanyRef, entry: dict, description=None) -> RawJob | None:
        job_id, title = str(entry.get("id") or "").strip(), (entry.get("jobOpeningName") or "").strip()
        if not (job_id and title):
            return None
        text = html_to_text(description) if description else None
        posted = entry.get("datePosted")
        try:
            posted = datetime.fromisoformat(posted).replace(tzinfo=timezone.utc) if posted else None
        except ValueError:
            posted = None
        return RawJob(
            source=self.name, company=company.name, company_slug=company.slug,
            external_id=job_id, title=title, location=_place(entry), description=text,
            application_url=entry.get("jobOpeningShareUrl") or f"{self.board_url(company.slug)}/{job_id}",
            posted_at=posted, requirements=extract_requirements(text) if text else None,
        )

    def stub_jobs(self, company: CompanyRef) -> list[RawJob]:
        payload = self.client.get_json(self._list(company.slug), headers=JSON)
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
            raise FetchError(f"Unexpected BambooHR payload for {company.slug}")
        return [j for j in (self._to_job(company, e) for e in payload["result"]) if j]

    def fill_details(self, company: CompanyRef, jobs: list[RawJob]) -> list[RawJob]:
        filled = []
        for job in jobs:
            try:
                detail = self.client.get_json(self._detail(company.slug, job.external_id), headers=JSON)
                opening = (detail.get("result") or {}).get("jobOpening") or {}
                job = self._to_job(company, {**opening, "id": job.external_id},
                                   opening.get("description")) or job
            except (FetchError, RobotsDisallowed, AttributeError) as exc:
                # One unreachable posting must not lose the rest of the board.
                self.log_detail_miss(company, job, exc)
            filled.append(job)
        return filled

    def log_detail_miss(self, company, job, exc) -> None:
        import logging
        logging.getLogger(__name__).warning("bamboohr/%s: %s has no detail (%s)",
                                            company.slug, job.external_id, exc)

    def fetch_jobs(self, company: CompanyRef) -> list[RawJob]:
        return self.fill_details(company, self.stub_jobs(company))
