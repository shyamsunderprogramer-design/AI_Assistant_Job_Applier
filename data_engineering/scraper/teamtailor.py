"""Teamtailor scraper — each career site's public RSS feed.

    GET https://<slug>.teamtailor.com/jobs.rss

Teamtailor's JSON API needs a per-company key; its RSS feed does not and
carries the full description, the posting link and structured locations in a
`tt:` namespace. Read live from Polestar's site in Sep 2026. A slug that is no
one's site answers 404.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from data_engineering.scraper.base import CompanyRef, PortalScraper, RawJob, extract_requirements, html_to_text
from data_engineering.scraper.http_client import FetchError, RateLimited, RobotsDisallowed

TT = "{https://teamtailor.com/locations}"


def parse_feed(xml_text: str, company: CompanyRef, source: str = "teamtailor") -> list[RawJob]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FetchError(f"Teamtailor {company.slug}: feed is not XML") from exc
    jobs = []
    for item in root.findall("./channel/item"):
        text = lambda tag: (item.findtext(tag) or "").strip()
        places = []
        for loc in item.iter(f"{TT}location"):
            city, country = loc.findtext(f"{TT}city") or "", loc.findtext(f"{TT}country") or ""
            places.append(", ".join(p for p in (city, country) if p) or loc.findtext(f"{TT}name") or "")
        place = "; ".join(p for p in places if p)
        remote = text("remoteStatus").lower()
        if remote in ("fully", "remote", "temporary", "hybrid"):
            place = f"{'Hybrid' if remote == 'hybrid' else 'Remote'}{f' ({place})' if place else ''}"
        try:
            posted = parsedate_to_datetime(text("pubDate"))
        except (TypeError, ValueError):
            posted = None
        description = html_to_text(text("description"))
        if not (text("guid") and text("title") and text("link")):
            continue
        jobs.append(RawJob(
            source=source, company=text("company_name") or company.name,
            company_slug=company.slug, external_id=text("guid"), title=text("title"),
            location=place or None, description=description,
            application_url=text("link"), posted_at=posted,
            requirements=extract_requirements(description),
        ))
    return jobs


class TeamtailorScraper(PortalScraper):
    name = "teamtailor"

    def _feed(self, slug: str) -> str:
        return f"https://{slug}.teamtailor.com/jobs.rss"

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.teamtailor.com/jobs"

    def board_exists(self, slug: str) -> bool:
        try:
            resp = self.client.get(self._feed(slug))
        except RateLimited:
            raise            # a refusal, not an answer: never cache it
        except (FetchError, RobotsDisallowed):
            return False
        return resp.status_code == 200 and "<rss" in resp.text[:500]

    def fetch_jobs(self, company: CompanyRef) -> list[RawJob]:
        resp = self.client.get(self._feed(company.slug))
        if resp.status_code != 200:
            raise FetchError(f"Teamtailor {company.slug}: HTTP {resp.status_code}")
        return parse_feed(resp.text, company, self.name)
