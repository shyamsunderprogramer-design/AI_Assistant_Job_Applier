"""Hacker News "Ask HN: Who is hiring?" — the monthly thread, via HN's API.

https://hn.algolia.com/api — the newest thread by `whoishiring`, then each
top-level comment, which by the thread's own rule starts with a line like

    Acme Robotics | Senior DevOps Engineer | Austin, TX or REMOTE (US) | $180k

Those lines are written by hand, so this is best effort: the company is the
first field, the title the first field that names a role, the location the
first that names a place or "remote". The whole comment is the description,
and the link is the comment itself, where the poster says how to apply.
"""

import re

from data_engineering.scraper.feeds.common import FeedJob, clean, posting

NAME, LABEL, ENV, KEYED_API = "hn-hiring", "HN Who is hiring", (), False
SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ITEM = "https://hn.algolia.com/api/v1/items/{id}"

ROLE = re.compile(r"engineer|developer|sre\b|devops|architect|admin|programmer|"
                  r"platform|infrastructure|reliability|scientist|lead\b", re.I)
PLACE = re.compile(r"remote|onsite|on-site|hybrid|\b[A-Z][a-z]+,\s*[A-Z]{2}\b|"
                   r"\b(usa|us|nyc|sf|london|berlin|toronto)\b", re.I)


def split_header(text: str) -> tuple[str, str, str] | None:
    first = clean(text).split("\n", 1)[0]
    parts = [p.strip() for p in first.split("|") if p.strip()]
    if len(parts) < 2:
        return None
    company = parts[0]
    title = next((p for p in parts[1:] if ROLE.search(p)), "")
    place = next((p for p in parts[1:] if p != title and PLACE.search(p)), "")
    return (company, title, place) if title else None


def fetch(http, titles, cfg) -> list[FeedJob]:
    data = http.get_json(SEARCH, params={"tags": "story,author_whoishiring",
                                         "hitsPerPage": 10}) or {}
    story = next((h for h in data.get("hits") or []
                  if str(h.get("title", "")).startswith("Ask HN: Who is hiring?")), None)
    if story is None:
        return []
    thread = http.get_json(ITEM.format(id=story["objectID"])) or {}
    jobs = []
    for comment in thread.get("children") or []:
        if not comment.get("text"):
            continue                       # deleted or flagged
        header = split_header(comment["text"])
        if header is None:
            continue
        company, title, place = header
        raw = posting(NAME, job_id=comment.get("id"), title=title[:200], company=company[:120],
                      url=f"https://news.ycombinator.com/item?id={comment.get('id')}",
                      location=place or None, description=comment["text"],
                      posted=comment.get("created_at"))
        if raw:
            jobs.append(FeedJob(raw))
    return jobs
