"""USAJOBS — every US federal job, official API with a free key.

https://developer.usajobs.gov . Searched by keyword (one per job title), posted
in the last few days. The API wants the registered email as User-Agent and the
key in Authorization-Key. data.usajobs.gov's robots.txt addresses crawlers; the
API is offered for keyed use, so it runs under the keyed-API rule (README §C5).
"""

import os

from data_engineering.scraper.feeds.common import FeedJob, money, posting

NAME, LABEL, KEYED_API = "usajobs", "USAJOBS", True
ENV = ("USAJOBS_API_KEY",)
API = "https://data.usajobs.gov/api/search"


def fetch(http, titles, cfg) -> list[FeedJob]:
    email = (os.getenv("USAJOBS_EMAIL") or os.getenv("MAIL_ADDRESS") or "").strip()
    headers = {"Host": "data.usajobs.gov", "User-Agent": email,
               "Authorization-Key": os.getenv("USAJOBS_API_KEY", "").strip()}
    jobs = []
    for title in titles[: int(cfg.get("feeds.usajobs.max_titles", 8))]:
        # Series 2210 is federal IT; without it a "cloud" search returned
        # meteorological technicians and nursing assistants. Federal postings
        # stay open for weeks, so the window is longer than other feeds'.
        data = http.get_json(API, params={"Keyword": title, "ResultsPerPage": 100,
                                          "JobCategoryCode": "2210",
                                          "DatePosted": int(cfg.get("feeds.usajobs.days", 30))},
                             headers=headers) or {}
        items = ((data.get("SearchResult") or {}).get("SearchResultItems")) or []
        for item in items:
            d = item.get("MatchedObjectDescriptor") or {}
            pay = (d.get("PositionRemuneration") or [{}])[0]
            summary = ((d.get("UserArea") or {}).get("Details") or {}).get("JobSummary", "")
            raw = posting(NAME, job_id=item.get("MatchedObjectId") or d.get("PositionID"),
                          title=d.get("PositionTitle"),
                          company=d.get("OrganizationName") or d.get("DepartmentName"),
                          url=(d.get("ApplyURI") or [d.get("PositionURI")])[0],
                          location=d.get("PositionLocationDisplay"),
                          description=f"{summary}\n\n{d.get('QualificationSummary', '')}",
                          posted=d.get("PublicationStartDate"))
            if raw:
                yearly = pay.get("RateIntervalCode") == "PA"
                jobs.append(FeedJob(raw, money(pay.get("MinimumRange")) if yearly else None,
                                    money(pay.get("MaximumRange")) if yearly else None))
    return jobs


import re as _re

_FEDERAL_IT = _re.compile(r"\b(it|information technology) specialist\b", _re.I)


def accept(job: FeedJob, job_filter) -> bool:
    """Federal IT titles rarely say what the job is: "IT Specialist (SYSADMIN)".

    Such a title is accepted when the posting's own text names one of the
    person's search terms; every other title still has to pass the filter.
    """
    if job_filter.matches(job.raw):
        return True
    if not _FEDERAL_IT.search(job.raw.title or ""):
        return False
    text = (job.raw.description or "").lower()
    return any(k in text for k in job_filter.title_keywords)
