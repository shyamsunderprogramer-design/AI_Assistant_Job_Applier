"""Workable scraper — public job board API.

    POST https://apply.workable.com/api/v1/accounts/<slug>/jobs   -> the list
    GET  https://apply.workable.com/api/v1/accounts/<slug>/jobs/<shortcode>

Workable is the fourth big ATS and the first one added since the original
three. It matters because Greenhouse, Lever and Ashby are a tech-sector
habit: measured against this project's own data, 43% of the companies whose
boards we can read are software, IT, internet or marketing firms, and
manufacturers and distributors are almost absent. Workable's thirty thousand
customers skew smaller and much broader, which is the population this tool
could not see at all.

`robots.txt` permits it explicitly, which is why this one is here and
SmartRecruiters is not:

    User-agent: *
    Content-Signal: search=yes, ai-input=yes, ai-train=no
    Disallow:

An empty Disallow is "allow everything", and the content signal names search
and AI input as permitted uses. SmartRecruiters' API, by contrast, is
`Disallow: /` for everyone except LinkedInBot, so it stays unscraped.

Like Workday, the listing carries no description, so this is a two-call
scraper: `stub_jobs` returns what the list gives -- enough to filter on title
and location -- and `fill_details` fetches a description only for the
postings that survived. A board of four hundred jobs then costs four hundred
requests only if four hundred of them match.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from scraper.base import CompanyRef, PortalScraper, RawJob, extract_requirements, html_to_text
from scraper.http_client import FetchError, RobotsDisallowed

log = logging.getLogger(__name__)

API_ROOT = "https://apply.workable.com/api/v1/accounts"

# The board's own search form sends this. Empty filters mean "everything";
# the endpoint rejects a bare {} body.
LIST_BODY = {"query": "", "location": [], "department": [], "worktype": []}

# Workable pages with an opaque token and fixes the page size itself -- a
# "limit" in the body is rejected outright ({"limit":"Not allowed"}), and a
# token in the query string is ignored silently, which is the worse failure:
# it returns page one again and a naive loop never terminates.
MAX_PAGES = 200          # 2,000 postings; a guard, not an expectation


class WorkableScraper(PortalScraper):
    name = "workable"

    def board_url(self, slug: str) -> str:
        return f"https://apply.workable.com/{slug}/"

    def _list_url(self, slug: str) -> str:
        return f"{API_ROOT}/{slug}/jobs"

    def _detail_url(self, slug: str, shortcode: str) -> str:
        return f"{API_ROOT}/{slug}/jobs/{shortcode}"

    def board_exists(self, slug: str) -> bool:
        """Does this slug name a real Workable board?

        A board with no open roles still answers 200 with `results: []`, and
        that is a real board -- it will have postings again. Only a 404 means
        the slug belongs to nobody.
        """
        try:
            resp = self.client.post(self._list_url(slug), json=LIST_BODY)
        except (FetchError, RobotsDisallowed):
            return False
        if resp.status_code != 200:
            return False
        try:
            payload = resp.json()
        except ValueError:
            return False
        return isinstance(payload, dict) and "results" in payload

    # -- listing ------------------------------------------------------------

    def _pages(self, slug: str):
        """Every page of the listing, following Workable's paging token."""
        token = None
        seen = 0
        for _ in range(MAX_PAGES):
            body = dict(LIST_BODY)
            if token:
                body["token"] = token      # in the body; the query string is ignored
            resp = self.client.post(self._list_url(slug), json=body)
            if resp.status_code != 200:
                raise FetchError(f"Workable {slug}: HTTP {resp.status_code}")
            try:
                payload = resp.json()
            except ValueError as exc:
                raise FetchError(f"Workable {slug}: response was not JSON") from exc

            results = payload.get("results") or []
            yield results
            seen += len(results)

            token = payload.get("nextPage") or payload.get("token")
            total = payload.get("total")
            # Stop on any of: no token, an empty page, or having seen the
            # count the API itself reported. A paging bug must not loop.
            if not token or not results or (total is not None and seen >= total):
                return
        log.warning("workable/%s: stopped at %d pages", slug, MAX_PAGES)

    def stub_jobs(self, company: CompanyRef) -> list[RawJob]:
        """Postings with title and location but no description.

        Enough for the title and location filters, which is the point: the
        description costs a request per posting and most postings are
        rejected on their title.
        """
        stubs: list[RawJob] = []
        for page in self._pages(company.slug):
            for entry in page:
                stub = self._to_job(company, entry, description=None)
                if stub is not None:
                    stubs.append(stub)
        return stubs

    def fill_details(self, company: CompanyRef, jobs: list[RawJob]) -> list[RawJob]:
        """Fetch the description for postings that survived the filter."""
        filled: list[RawJob] = []
        for job in jobs:
            try:
                detail = self.client.get_json(
                    self._detail_url(company.slug, job.external_id))
            except (FetchError, RobotsDisallowed) as exc:
                # One unreachable posting must not lose the rest of the board.
                log.warning("workable/%s: %s has no detail (%s)",
                            company.slug, job.external_id, exc)
                filled.append(job)
                continue
            if isinstance(detail, dict):
                job = self._to_job(company, detail, description=True) or job
            filled.append(job)
        return filled

    def fetch_jobs(self, company: CompanyRef) -> list[RawJob]:
        """Every posting, with descriptions. Used when nothing pre-filters."""
        return self.fill_details(company, self.stub_jobs(company))

    # -- shaping ------------------------------------------------------------

    def _to_job(self, company: CompanyRef, entry: dict,
                description) -> RawJob | None:
        shortcode = str(entry.get("shortcode") or entry.get("id") or "").strip()
        title = (entry.get("title") or "").strip()
        if not shortcode or not title:
            return None

        body = None
        if description is not None:
            # description and requirements are separate HTML fields, and the
            # requirements are what the scorer weighs most, so keep both.
            parts = [html_to_text(entry.get(k) or "")
                     for k in ("description", "requirements", "benefits")]
            body = "\n\n".join(p for p in parts if p).strip() or None

        return RawJob(
            source=self.name,
            company=company.name or company.slug,
            company_slug=company.slug,
            external_id=shortcode,
            title=title,
            location=_location(entry),
            description=body,
            application_url=f"https://apply.workable.com/{company.slug}/j/{shortcode}/",
            posted_at=_published(entry.get("published")),
            requirements=(extract_requirements(body) if body else None),
        )


def _location(entry: dict) -> str | None:
    """A human-readable location, or None when the posting does not say.

    Workable gives a structured object and sometimes a list of them. "Remote"
    is a property of the posting rather than a place, so it is only used when
    there is no city to name -- a remote job in Boston is still in Boston to
    anyone filtering by state.
    """
    places = entry.get("locations") or []
    if not places and isinstance(entry.get("location"), dict):
        places = [entry["location"]]

    named = []
    for place in places:
        if not isinstance(place, dict):
            continue
        bits = [str(place.get(k) or "").strip()
                for k in ("city", "region", "country")]
        joined = ", ".join(b for b in bits if b)
        if joined:
            named.append(joined)

    if named:
        return " | ".join(dict.fromkeys(named))
    if str(entry.get("remote")).lower() == "true" or entry.get("workplace") == "remote":
        return "Remote"
    return None


def _published(value) -> datetime | None:
    """Workable's ISO timestamp, or None. An unreadable date is not a date."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
