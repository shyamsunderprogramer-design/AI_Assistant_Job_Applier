"""Working Nomads — remote jobs, public JSON, no key.

https://www.workingnomads.com/api/exposed_jobs/ — the latest postings in one
list; the title filter keeps the relevant ones.
"""

from data_engineering.scraper.feeds.common import FeedJob, posting

NAME, LABEL, ENV, KEYED_API = "workingnomads", "Working Nomads", (), False
API = "https://www.workingnomads.com/api/exposed_jobs/"


def fetch(http, titles, cfg) -> list[FeedJob]:
    jobs = []
    for r in http.get_json(API) or []:
        raw = posting(NAME, job_id=r.get("url"), title=r.get("title"), company=r.get("company_name"),
                      url=r.get("url"), location=f"Remote ({r.get('location') or 'anywhere'})",
                      description=r.get("description"), posted=r.get("pub_date"))
        if raw:
            jobs.append(FeedJob(raw))
    return jobs
