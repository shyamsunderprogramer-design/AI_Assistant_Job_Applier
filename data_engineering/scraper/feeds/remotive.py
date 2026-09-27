"""Remotive — remote tech jobs, public API, no key.

https://remotive.com/api/remote-jobs . Remotive asks for a handful of calls a
day at most and a link back: each posting's URL is Remotive's own page.
"""

from data_engineering.scraper.feeds.common import FeedJob, posting

NAME, LABEL, ENV, KEYED_API = "remotive", "Remotive", (), False
API = "https://remotive.com/api/remote-jobs"


def fetch(http, titles, cfg) -> list[FeedJob]:
    jobs = []
    for category in cfg.get("feeds.remotive.categories", ["devops", "software-development", "information-technology"]):
        data = http.get_json(API, params={"category": category}) or {}
        for r in data.get("jobs") or []:
            raw = posting(NAME, job_id=r.get("id"), title=r.get("title"),
                          company=r.get("company_name"), url=r.get("url"),
                          location=f"Remote ({r.get('candidate_required_location') or 'anywhere'})",
                          description=r.get("description"), posted=r.get("publication_date"))
            if raw:
                jobs.append(FeedJob(raw))
    return jobs
