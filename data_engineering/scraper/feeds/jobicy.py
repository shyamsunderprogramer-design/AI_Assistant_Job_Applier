"""Jobicy — remote jobs, public API, no key.

https://jobicy.com/api/v2/remote-jobs — searched by tag (one per job title)
and limited to US-eligible roles. Jobicy asks for no more than hourly polling.
"""

from data_engineering.scraper.feeds.common import FeedJob, money, posting

NAME, LABEL, ENV, KEYED_API = "jobicy", "Jobicy", (), False
API = "https://jobicy.com/api/v2/remote-jobs"


def fetch(http, titles, cfg) -> list[FeedJob]:
    jobs = []
    for tag in titles[: int(cfg.get("feeds.jobicy.max_titles", 5))]:
        data = http.get_json(API, params={"count": 50, "tag": tag, "geo": "usa"}) or {}
        for r in data.get("jobs") or []:
            raw = posting(NAME, job_id=r.get("id"), title=r.get("jobTitle"),
                          company=r.get("companyName"), url=r.get("url"),
                          location=f"Remote ({r.get('jobGeo') or 'anywhere'})",
                          description=r.get("jobDescription") or r.get("jobExcerpt"),
                          posted=r.get("pubDate"))
            if raw:
                period = "hour" if str(r.get("salaryPeriod", "")).lower().startswith("hour") else "year"
                jobs.append(FeedJob(raw, money(r.get("salaryMin")), money(r.get("salaryMax")),
                                    r.get("salaryCurrency") or None, period))
    return jobs
