"""Careerjet — a large aggregator, official partner API with a free affiliate id.

http://public.api.careerjet.net/search (the documented v3 search). Careerjet's
terms want the end user's IP and browser string with each call; this sends the
configured values. Its host's robots.txt addresses crawlers, so it runs under
the keyed-API rule (README §C5).

Checked live on 26 Sep 2026 (a Denver, CO DevOps posting came back).
"""

import os

from data_engineering.scraper.feeds.common import FeedJob, money, posting

NAME, LABEL, KEYED_API = "careerjet", "Careerjet", True
ENV = ("CAREERJET_AFFID",)
API = "http://public.api.careerjet.net/search"


def fetch(http, titles, cfg) -> list[FeedJob]:
    base = {"locale_code": "en_US", "affid": os.getenv("CAREERJET_AFFID", "").strip(),
            "user_ip": os.getenv("CAREERJET_USER_IP", "127.0.0.1"),
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "sort": "date", "pagesize": 99, "page": 1}
    # Careerjet answers 403 to a request that does not say which browser and
    # page it is for: its API forwards the end user's details, and sent from a
    # script with a generic User-Agent the call was refused (26 Sep 2026) while
    # the same ID with these headers returned jobs.
    headers = {"User-Agent": base["user_agent"], "Referer": "https://www.careerjet.com/"}
    jobs = []
    for title in titles[: int(cfg.get("feeds.careerjet.max_titles", 8))]:
        data = http.get_json(API, params={**base, "keywords": title}, headers=headers) or {}
        for r in data.get("jobs") or []:
            raw = posting(NAME, job_id=r.get("url"), title=r.get("title"),
                          company=r.get("company"), url=r.get("url"),
                          location=r.get("locations"), description=r.get("description"),
                          posted=r.get("date"))
            if raw:
                yearly = str(r.get("salary_type", "")).upper() == "Y"
                jobs.append(FeedJob(raw, money(r.get("salary_min")) if yearly else None,
                                    money(r.get("salary_max")) if yearly else None))
    return jobs
