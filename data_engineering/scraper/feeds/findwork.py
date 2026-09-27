"""Findwork.dev — software and DevOps jobs, API with a free key.

https://findwork.dev/developers/ — searched per job title, newest first; the
key goes in an `Authorization: Token ...` header.
"""

import os

from data_engineering.scraper.feeds.common import FeedJob, posting

NAME, LABEL, KEYED_API = "findwork", "Findwork", False
ENV = ("FINDWORK_API_KEY",)
API = "https://findwork.dev/api/jobs/"


def fetch(http, titles, cfg) -> list[FeedJob]:
    headers = {"Authorization": f"Token {os.getenv('FINDWORK_API_KEY', '').strip()}"}
    jobs = []
    for title in titles[: int(cfg.get("feeds.findwork.max_titles", 8))]:
        data = http.get_json(API, params={"search": title, "sort_by": "date"},
                             headers=headers) or {}
        for r in data.get("results") or []:
            place = r.get("location") or ""
            if r.get("remote"):
                place = f"Remote{f' ({place})' if place else ''}"
            raw = posting(NAME, job_id=r.get("id"), title=r.get("role"),
                          company=r.get("company_name"), url=r.get("url"),
                          location=place or None, description=r.get("text"),
                          posted=r.get("date_posted"))
            if raw:
                jobs.append(FeedJob(raw))
    return jobs
