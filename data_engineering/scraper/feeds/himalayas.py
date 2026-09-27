"""Himalayas — remote jobs, public API, no key.

https://himalayas.app/jobs/api — newest first, 20 a page, `pubDate` in Unix
seconds, `locationRestrictions` a list of countries (empty: anywhere).
"""

from data_engineering.scraper.feeds.common import FeedJob, money, posting

NAME, LABEL, ENV, KEYED_API = "himalayas", "Himalayas", (), False
API = "https://himalayas.app/jobs/api"
PAGE = 20


def fetch(http, titles, cfg) -> list[FeedJob]:
    jobs = []
    for page in range(int(cfg.get("feeds.himalayas.pages", 5))):
        data = http.get_json(API, params={"limit": PAGE, "offset": page * PAGE}) or {}
        batch = data.get("jobs") or []
        for r in batch:
            places = ", ".join(r.get("locationRestrictions") or []) or "anywhere"
            raw = posting(NAME, job_id=r.get("guid"), title=r.get("title"),
                          company=r.get("companyName"),
                          url=r.get("applicationLink") or r.get("guid"),
                          location=f"Remote ({places})",
                          description=r.get("description") or r.get("excerpt"),
                          posted=r.get("pubDate"))
            if raw:
                jobs.append(FeedJob(raw, money(r.get("minSalary")), money(r.get("maxSalary")),
                                    r.get("currency") or None,
                                    "hour" if str(r.get("salaryPeriod", "")).lower().startswith("hour") else "year"))
        if len(batch) < PAGE:
            break
    return jobs
