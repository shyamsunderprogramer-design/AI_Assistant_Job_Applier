"""Workday scraper — the public career-site API every Workday tenant exposes.

    POST https://<tenant>.<dc>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
    GET  https://<tenant>.<dc>.myworkdayjobs.com/wday/cxs/<tenant>/<site><path>

Workday is where the large employers are, and it is the biggest gap in a board
list built from Greenhouse, Lever and Ashby alone. Two things make it different
from those three, and both shape this module.

**A slug is not enough.** Greenhouse needs "nvidia"; Workday needs the tenant,
the datacenter it is hosted in, and the name of the career site — and none of
the three can be guessed. A bare tenant host answers 406 whether or not the
tenant lives there, so probing tells you nothing. The triple is packed into the
existing single `slug` field as "tenant:dc:site", which keeps the company table
and the discovery cache unchanged.

**The list has no descriptions.** Every posting needs a second request, and a
tenant can have two thousand of them. So the caller filters on title and
location first, using what the list already carries, and only then are
descriptions fetched — twenty requests instead of two thousand.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from scraper.base import CompanyRef, PortalScraper, RawJob, extract_requirements, html_to_text
from scraper.http_client import FetchError, RobotsDisallowed

log = logging.getLogger(__name__)

# A tenant may hold thousands of postings; Workday caps a page at 20.
PAGE_SIZE = 20
MAX_PAGES = 50              # 1,000 postings per company is already generous
# Descriptions cost one request each, so only ever fetch them for postings a
# filter has already kept.
MAX_DETAILS = 60

# Workday writes "6 Locations" when a posting spans several offices. That is a
# count, not a place: it can never match a location filter, so every multi-site
# posting would be dropped before anyone saw it. Treat it as unknown and let the
# detail record supply the real location.
_LOCATION_COUNT = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)


def _location(text: str | None) -> str | None:
    value = (text or "").strip()
    if not value or _LOCATION_COUNT.match(value):
        return None
    return value


WORKDAY_URL = re.compile(
    r"https?://([a-z0-9][a-z0-9-]*)\.(wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)",
    re.I,
)


def parse_slug(slug: str) -> tuple[str, str, str] | None:
    """"tenant:dc:site" -> its three parts, or None when malformed."""
    parts = (slug or "").split(":")
    if len(parts) != 3 or not all(p.strip() for p in parts):
        return None
    tenant, dc, site = (p.strip() for p in parts)
    if not re.fullmatch(r"wd\d+", dc, re.I):
        return None
    return tenant, dc.lower(), site


def make_slug(tenant: str, dc: str, site: str) -> str:
    return f"{tenant}:{dc}:{site}"


def slug_from_url(url: str) -> str | None:
    """Recover "tenant:dc:site" from any myworkdayjobs URL.

    This is how a Workday board is found at all: a company's careers page
    redirects to one of these, and the URL names all three parts. Guessing
    them is not possible, so the careers URL is the only practical route in.
    """
    found = WORKDAY_URL.search(url or "")
    if not found:
        return None
    tenant, dc, site = found.group(1), found.group(2).lower(), found.group(3)
    # A locale segment ("en-US") is not a site name.
    if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", site):
        return None
    return make_slug(tenant, dc, site)


def _api_root(tenant: str, dc: str, site: str) -> str:
    return f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"


def _posted_at(value: str | None) -> datetime | None:
    """Workday sends an ISO date on the detail record; the list says "Posted Today"."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class WorkdayScraper(PortalScraper):
    """Reads one Workday tenant's public career site."""

    name = "workday"

    def board_url(self, slug: str) -> str:
        parts = parse_slug(slug)
        if not parts:
            return ""
        tenant, dc, site = parts
        return f"https://{tenant}.{dc}.myworkdayjobs.com/{site}"

    def board_exists(self, slug: str) -> bool:
        parts = parse_slug(slug)
        if not parts:
            return False
        try:
            payload = self._page(*parts, offset=0, limit=1)
        except (FetchError, RobotsDisallowed, ValueError):
            return False
        return isinstance(payload, dict) and "jobPostings" in payload

    # -- the two API calls -------------------------------------------------
    def _page(self, tenant: str, dc: str, site: str, offset: int, limit: int) -> dict:
        response = self.client.post(
            f"{_api_root(tenant, dc, site)}/jobs",
            json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""},
            headers={"Accept": "application/json"},
        )
        return response.json()

    def _detail(self, tenant: str, dc: str, site: str, path: str) -> dict:
        response = self.client.get(
            f"{_api_root(tenant, dc, site)}{path}",
            headers={"Accept": "application/json"},
        )
        return (response.json() or {}).get("jobPostingInfo") or {}

    # -- the interface -----------------------------------------------------
    def fetch_jobs(self, company: CompanyRef) -> list[RawJob]:
        """Every posting for one tenant, descriptions included.

        Listings arrive without descriptions, so `stub_jobs` + `fill_details`
        exist separately for the runner to filter in between. This method is
        the whole thing, for callers that want it.
        """
        stubs = self.stub_jobs(company)
        return self.fill_details(company, stubs)

    def stub_jobs(self, company: CompanyRef) -> list[RawJob]:
        """Postings with title and location but no description yet.

        Enough for the title/location filter to reject most of them, which is
        the point: a description costs a request, and a tenant can hold two
        thousand postings.
        """
        parts = parse_slug(company.slug)
        if not parts:
            raise FetchError(f"{company.slug!r} is not a tenant:dc:site slug")
        tenant, dc, site = parts

        stubs: list[RawJob] = []
        # `total` is sent on the first page only; later pages report 0, so
        # trusting it per-page stops the walk after forty postings.
        total: int | None = None
        for page in range(MAX_PAGES):
            payload = self._page(tenant, dc, site, offset=page * PAGE_SIZE, limit=PAGE_SIZE)
            if total is None:
                total = payload.get("total") or None
            postings = payload.get("jobPostings") or []
            if not postings:
                break
            for entry in postings:
                path = entry.get("externalPath")
                if not path:
                    continue
                stubs.append(
                    RawJob(
                        source=self.name,
                        company=company.name,
                        company_slug=company.slug,
                        # The real requisition id arrives with the detail; the
                        # path is unique and stable enough to identify it until
                        # then, and is replaced below.
                        external_id=path,
                        title=(entry.get("title") or "").strip(),
                        location=_location(entry.get("locationsText")),
                        description=None,
                        application_url=self.board_url(company.slug),
                    )
                )
            if total is not None and len(stubs) >= total:
                break
        return stubs

    def fill_details(self, company: CompanyRef, stubs: list[RawJob]) -> list[RawJob]:
        """Fetch the description for each posting that survived filtering."""
        parts = parse_slug(company.slug)
        if not parts:
            return []
        tenant, dc, site = parts

        filled: list[RawJob] = []
        for stub in stubs[:MAX_DETAILS]:
            try:
                info = self._detail(tenant, dc, site, stub.external_id)
            except (FetchError, RobotsDisallowed) as exc:
                # One unreachable posting must not cost the whole tenant.
                log.debug("Workday detail failed for %s: %s", stub.external_id, exc)
                continue
            except ValueError:
                continue

            description = html_to_text(info.get("jobDescription"))
            stub.description = description
            stub.requirements = extract_requirements(description)
            stub.posted_at = _posted_at(info.get("startDate") or info.get("postedOn"))
            # The list said "6 Locations"; the detail names one.
            stub.location = _location(info.get("location")) or stub.location
            # Prefer the requisition id: stable across re-postings, where the
            # URL path changes whenever the title is edited.
            stub.external_id = str(info.get("jobReqId") or stub.external_id)
            stub.application_url = info.get("externalUrl") or stub.application_url
            filled.append(stub)
        if len(stubs) > MAX_DETAILS:
            log.info("%s: %d postings matched, fetching the first %d",
                     company.name, len(stubs), MAX_DETAILS)
        return filled
