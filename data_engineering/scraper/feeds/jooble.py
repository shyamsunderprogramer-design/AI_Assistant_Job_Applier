"""Jooble — a large aggregator of job sites, official API with a free key.

https://jooble.org/api/about — POST https://jooble.org/api/<key> with the
keywords and a location; results carry a snippet, not the whole posting.
"""

import os

from data_engineering.scraper.feeds.common import FeedJob, posting

NAME, LABEL, KEYED_API = "jooble", "Jooble", False
ENV = ("JOOBLE_API_KEY",)
API = "https://jooble.org/api/{key}"


def fetch(http, titles, cfg) -> list[FeedJob]:
    url = API.format(key=os.getenv("JOOBLE_API_KEY", "").strip())
    jobs = []
    for title in titles[: int(cfg.get("feeds.jooble.max_titles", 8))]:
        data = http.post_json(url, {"keywords": title, "location": "United States",
                                    "page": "1"}) or {}
        for r in data.get("jobs") or []:
            raw = posting(NAME, job_id=r.get("id"), title=r.get("title"),
                          company=r.get("company"), url=r.get("link"),
                          location=r.get("location"), description=r.get("snippet"),
                          posted=r.get("updated"))
            if raw:
                jobs.append(FeedJob(raw))
    return jobs
