"""The Muse — jobs at known employers, public API; a key only raises the limit.

https://www.themuse.com/api/public/jobs — by category, newest first, 20 a
page. Many of its employers are on Greenhouse or Lever too; those copies are
skipped at storage, since the board's own copy is the better one.
"""

import os

from data_engineering.scraper.feeds.common import FeedJob, posting

NAME, LABEL, ENV, KEYED_API = "themuse", "The Muse", (), False
API = "https://www.themuse.com/api/public/jobs"


def fetch(http, titles, cfg) -> list[FeedJob]:
    key = (os.getenv("THEMUSE_API_KEY") or "").strip()
    jobs = []
    for page in range(1, int(cfg.get("feeds.themuse.pages", 5)) + 1):
        params = [("page", page), ("descending", "true")]
        params += [("category", c) for c in cfg.get("feeds.themuse.categories",
                                                    ["Computer and IT"])]
        if key:
            params.append(("api_key", key))
        data = http.get_json(API, params=params) or {}
        for r in data.get("results") or []:
            places = "; ".join(l.get("name", "") for l in r.get("locations") or []) or None
            raw = posting(NAME, job_id=r.get("id"), title=r.get("name"),
                          company=(r.get("company") or {}).get("name"),
                          url=(r.get("refs") or {}).get("landing_page"), location=places,
                          description=r.get("contents"), posted=r.get("publication_date"))
            if raw:
                jobs.append(FeedJob(raw))
        if page >= int(data.get("page_count") or 0):
            break
    return jobs
