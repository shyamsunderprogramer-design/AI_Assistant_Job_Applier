"""JazzHR scraper — the public job board page, then each surviving posting.

    GET https://<slug>.applytojob.com/apply                 -> list of postings
    GET https://<slug>.applytojob.com/apply/<id>/<title>    -> the posting

Server-rendered HTML: each posting is an `<li class="list-group-item">` with
a heading link and a map-marker location. Read live from Harris Associates'
board in Sep 2026. A slug with no board lands on JazzHR's marketing page,
which has no `list-group-item-heading` in it.
"""

from __future__ import annotations

import html
import re

from data_engineering.scraper.base import CompanyRef, PortalScraper, RawJob, extract_requirements, html_to_text
from data_engineering.scraper.http_client import FetchError, RateLimited, RobotsDisallowed

ITEM = re.compile(r"<h3 class=['\"]list-group-item-heading['\"]>\s*<a href=\"(?P<url>[^\"]+/apply/(?P<id>[A-Za-z0-9]+)/[^\"]*)\">"
                  r"\s*(?P<title>[^<]{2,200}?)\s*</a>(?P<rest>.*?)</ul>", re.S)
PLACE = re.compile(r"fa-map-marker['\"]></i>\s*([^<]{2,120})")


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def parse_list(page: str, company: CompanyRef, source: str = "jazzhr") -> list[RawJob]:
    jobs = []
    for m in ITEM.finditer(page or ""):
        where = PLACE.search(m["rest"])
        jobs.append(RawJob(
            source=source, company=company.name, company_slug=company.slug,
            external_id=m["id"], title=_text(m["title"]),
            location=_text(where.group(1)) if where else None, description=None,
            application_url=m["url"],
        ))
    return jobs


def parse_description(page: str) -> str | None:
    tag = re.search(r"<div[^>]+(?:id|class)=\"[^\"]*job-description[^\"]*\"[^>]*>", page or "")
    if not tag:
        return None
    rest = page[tag.end():]
    # Cut at the start of the tag that opens the application form, not in the
    # middle of it -- a cut at the class name left '<div class="' behind.
    marks = [rest.rfind("<", 0, i) for i in (rest.find("job-application-form"),
                                             rest.find("resumator-submit")) if i > 0]
    marks += [i for i in (rest.find("<form"),) if i > 0]
    cut = min((m for m in marks if m >= 0), default=40000)
    return html_to_text(rest[:cut]) or None


class JazzHRScraper(PortalScraper):
    name = "jazzhr"

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.applytojob.com/apply"

    def board_exists(self, slug: str) -> bool:
        try:
            resp = self.client.get(self.board_url(slug))
        except RateLimited:
            raise            # a refusal, not an answer: never cache it
        except (FetchError, RobotsDisallowed):
            return False
        return resp.status_code == 200 and "list-group-item-heading" in resp.text \
            and f"{slug}.applytojob.com" in resp.url

    def stub_jobs(self, company: CompanyRef) -> list[RawJob]:
        resp = self.client.get(self.board_url(company.slug))
        if resp.status_code != 200:
            raise FetchError(f"JazzHR {company.slug}: HTTP {resp.status_code}")
        return parse_list(resp.text, company, self.name)

    def fill_details(self, company: CompanyRef, jobs: list[RawJob]) -> list[RawJob]:
        for job in jobs:
            try:
                resp = self.client.get(job.application_url)
                if resp.status_code == 200:
                    job.description = parse_description(resp.text)
                    job.requirements = extract_requirements(job.description) if job.description else None
            except (FetchError, RobotsDisallowed):
                pass          # one unreachable posting must not lose the board
        return jobs

    def fetch_jobs(self, company: CompanyRef) -> list[RawJob]:
        return self.fill_details(company, self.stub_jobs(company))
