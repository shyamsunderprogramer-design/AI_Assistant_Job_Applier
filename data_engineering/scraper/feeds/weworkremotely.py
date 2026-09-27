"""We Work Remotely — category RSS feeds, no key.

https://weworkremotely.com/categories/<category>.rss . Each item's title is
"Company: Job Title"; `region` says where applicants may live.
"""

import xml.etree.ElementTree as ET

from data_engineering.scraper.feeds.common import FeedJob, posting

NAME, LABEL, ENV, KEYED_API = "weworkremotely", "We Work Remotely", (), False
FEED = "https://weworkremotely.com/categories/{category}.rss"



def parse(xml_text: str) -> list[FeedJob]:
    jobs = []
    for item in ET.fromstring(xml_text).findall("./channel/item"):
        text = lambda tag: (item.findtext(tag) or "").strip()
        company, _, title = text("title").partition(": ")
        raw = posting(NAME, job_id=text("guid") or text("link"), title=title or text("title"),
                      company=company if title else "", url=text("link"),
                      location=f"Remote ({text('region') or 'anywhere'})",
                      description=text("description"), posted=text("pubDate"))
        if raw:
            jobs.append(FeedJob(raw))
    return jobs


def fetch(http, titles, cfg) -> list[FeedJob]:
    jobs = []
    for category in cfg.get("feeds.weworkremotely.categories",
                            ["remote-devops-sysadmin-jobs", "remote-back-end-programming-jobs"]):
        jobs += parse(http.get_text(FEED.format(category=category)))
    return jobs
