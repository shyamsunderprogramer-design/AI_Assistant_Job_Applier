"""Remote OK — remote jobs, public JSON feed, no key.

https://remoteok.com/api . The first element is Remote OK's legal notice: use
requires naming Remote OK as the source and linking to it, which the stored
URL (their posting page) does.
"""

from data_engineering.scraper.feeds.common import FeedJob, money, posting

NAME, LABEL, ENV, KEYED_API = "remoteok", "Remote OK", (), False
API = "https://remoteok.com/api"


def fetch(http, titles, cfg) -> list[FeedJob]:
    jobs = []
    for tag in cfg.get("feeds.remoteok.tags", ["devops", "sre", "cloud"]):
        data = http.get_json(API, params={"tag": tag}) or []
        for r in data if isinstance(data, list) else []:
            if not isinstance(r, dict) or "legal" in r:
                continue
            raw = posting(NAME, job_id=r.get("id"), title=r.get("position"),
                          company=r.get("company"), url=r.get("url") or r.get("apply_url"),
                          location=f"Remote ({r.get('location') or 'anywhere'})",
                          description=r.get("description"), posted=r.get("date"))
            if raw:
                jobs.append(FeedJob(raw, money(r.get("salary_min")), money(r.get("salary_max"))))
    return jobs
