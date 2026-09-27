"""Jobvite scraper — the public job list page, then each surviving posting.

    GET https://jobs.jobvite.com/<slug>/jobs          -> table of postings
    GET https://jobs.jobvite.com/<slug>/job/<id>      -> the posting

Jobvite's feeds need a company key; its hosted career pages do not, and they
are server-rendered HTML with stable `jv-` classes. Read live from WebMD's
board in Sep 2026. Two calls like Workable: titles and locations from the
list, descriptions only for postings that pass the filter.
"""

from __future__ import annotations

import html
import re

from data_engineering.scraper.base import CompanyRef, PortalScraper, RawJob, extract_requirements, html_to_text
from data_engineering.scraper.http_client import FetchError, RateLimited, RobotsDisallowed

ROOT = "https://jobs.jobvite.com"
ROW = re.compile(r'<a href="/(?P<slug>[^/"]+)/job/(?P<id>[A-Za-z0-9]+)"[^>]*>\s*(?P<title>[^<]{2,200}?)\s*</a>'
                 r'(?P<rest>.*?)(?=<a href="/[^/"]+/job/|</table>|$)', re.S)
LOCATION = re.compile(r'jv-job-list-location[^>]*>(.*?)</td>', re.S)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def parse_list(page: str, company: CompanyRef, source: str = "jobvite") -> list[RawJob]:
    jobs, seen = [], set()
    for m in ROW.finditer(page or ""):
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        where = LOCATION.search(m["rest"])
        jobs.append(RawJob(
            source=source, company=company.name, company_slug=company.slug,
            external_id=m["id"], title=_text(m["title"]),
            location=_text(where.group(1)) if where else None, description=None,
            application_url=f"{ROOT}/{m['slug']}/job/{m['id']}",
        ))
    return jobs


META = re.compile(r'jv-job-detail-meta">(.*?)</p>', re.S)


def parse_locations(page: str) -> str | None:
    """The posting's own locations: the list only says "2 Locations"."""
    m = META.search(page or "")
    if not m:
        return None
    parts = [_text(p) for p in re.split(r"<span[^>]*jv-inline-separator[^>]*>\s*</span>", m.group(1))]
    places = [p for p in parts[1:] if p]           # the first field is the department
    return "; ".join(places) or None


def parse_description(page: str) -> str | None:
    start = (page or "").find('class="jv-job-detail-description"')
    if start < 0:
        return None
    ends = [page.rfind("<", start, i) for i in (page.find("jv-job-detail-bottom", start),) if i > 0]
    ends += [i for i in (page.find("<footer", start), page.find("</main>", start)) if i > 0]
    end = min((i for i in ends if i > start), default=start + 40000)   # at a tag's start
    fragment = page[page.find(">", start) + 1:end]
    return html_to_text(fragment) or None


class JobviteScraper(PortalScraper):
    name = "jobvite"

    def board_url(self, slug: str) -> str:
        return f"{ROOT}/{slug}/jobs"

    def board_exists(self, slug: str) -> bool:
        try:
            resp = self.client.get(self.board_url(slug))
        except RateLimited:
            raise            # a refusal, not an answer: never cache it
        except (FetchError, RobotsDisallowed):
            return False
        return resp.status_code == 200 and "jv-job-list" in resp.text

    def stub_jobs(self, company: CompanyRef) -> list[RawJob]:
        resp = self.client.get(self.board_url(company.slug))
        if resp.status_code != 200:
            raise FetchError(f"Jobvite {company.slug}: HTTP {resp.status_code}")
        return parse_list(resp.text, company, self.name)

    def fill_details(self, company: CompanyRef, jobs: list[RawJob]) -> list[RawJob]:
        for job in jobs:
            try:
                resp = self.client.get(job.application_url)
                if resp.status_code == 200:
                    job.description = parse_description(resp.text)
                    if not job.location or re.fullmatch(r"\d+ Locations?", job.location):
                        job.location = parse_locations(resp.text) or job.location
                    job.requirements = extract_requirements(job.description) if job.description else None
            except (FetchError, RobotsDisallowed):
                pass          # one unreachable posting must not lose the board
        return jobs

    def fetch_jobs(self, company: CompanyRef) -> list[RawJob]:
        return self.fill_details(company, self.stub_jobs(company))
