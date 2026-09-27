"""Job postings out of Dice, Glassdoor, ZipRecruiter and Monster alert emails.

These sites forbid scraping; their alert emails are the person's own mail, sent
to them, and reading those is what they are for. Each parser was written
against real alerts in the person's inbox (Sep 2026) and relies on what those
emails share: the job title or the whole card is a link, and the lines around
it carry company, location, pay and age.

    Dice          (dice@connect.dice.com)       title · company · location · "Posted: MM-DD-YYYY"
                   Messages from individual recruiters (…@user.dice.com) have no
                   structure and are not read.
    Glassdoor     (noreply@glassdoor.com)       one link per card:
                   company · rating · title · location · [pay (Employer est.)] · [Easy Apply] · age
    ZipRecruiter  (alerts@ziprecruiter.com)     title · "Company • City, ST • Remote" · pay
    Monster       (notifications.monster.com)   title · company · - · city · - · state · VIEW JOB

The link is the site's own tracking redirect: it is personal to the inbox and
leads to the posting. The same job arrives in several emails with a different
link each time, so a posting is identified by company, title and location.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from datetime import datetime, timedelta

from data_engineering.scraper.job_alerts import AlertJob, strip_html

ANCHOR = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
NOISE = re.compile(r"^[\s​-‏͏﻿\xa0]*$")


def _anchors(body: str) -> list[tuple[str, list[str]]]:
    """(href, visible lines) for every link in the email."""
    found = []
    for href, inner in ANCHOR.findall(body or ""):
        parts = [html_lib.unescape(p).strip() for p in re.split(r"<[^>]+>", inner)]
        parts = [re.sub(r"\s+", " ", p) for p in parts if p and not NOISE.match(p)]
        if parts:
            found.append((html_lib.unescape(href), parts))
    return found


def _lines(body: str) -> list[str]:
    return [l for l in strip_html(body) if not NOISE.match(l)]


def _job_id(source: str, company: str, title: str, location: str | None) -> str:
    key = "|".join(re.sub(r"\s+", " ", (x or "").lower()).strip() for x in (company, title, location))
    return f"{source}:{hashlib.sha1(key.encode()).hexdigest()[:20]}"


def _job(source, title, company, url, location=None, salary=None, posted=None, extras=None):
    if not (title and company and url):
        return None
    return AlertJob(title=title.strip(), company=company.strip(), url=url, location=location or None,
                    salary=salary or None, posted_at=posted, source=source,
                    external_id=_job_id(source, company, title, location), extras=extras or {})


def _after(lines: list[str], title: str, start: int = 0) -> tuple[int, list[str]]:
    """The index of `title` in the body lines, and the lines that follow it."""
    for i in range(start, len(lines)):
        if lines[i] == title:
            return i, lines[i + 1:i + 9]
    return -1, []


# -- Dice ------------------------------------------------------------------------

DICE_NAV = {"view all recommended jobs", "log in", "unsubscribe", "terms & conditions",
            "dice knowledge center (faqs)", "privacy policy"}


def parse_dice(body: str, received: datetime | None = None) -> list[AlertJob]:
    lines, jobs, cursor = _lines(body), [], 0
    for href, parts in _anchors(body):
        title = parts[0]
        if "elinks.dice.com/a/" not in href or title.lower() in DICE_NAV or len(parts) > 1:
            continue
        i, rest = _after(lines, title, cursor)
        if i < 0 or len(rest) < 2:
            continue
        when = next((r for r in rest[:4] if r.startswith("Posted:")), "")
        if not when:
            continue            # a newsletter link, not a job: every Dice job has a Posted: line
        cursor = i + 1
        posted = None
        m = re.search(r"(\d{2})-(\d{2})-(\d{4})", when)
        if m:
            posted = datetime(int(m[3]), int(m[1]), int(m[2]), tzinfo=received.tzinfo if received else None)
        job = _job("dice-email", title, rest[0], href,
                   location=rest[1] if not rest[1].startswith("Posted:") else None,
                   posted=posted or received)
        if job:
            jobs.append(job)
    return jobs


# -- Glassdoor -------------------------------------------------------------------

RATING = re.compile(r"^\d\.\d\s*★$")
AGE = re.compile(r"^(\d+)([hd])\+?$")


def parse_glassdoor(body: str, received: datetime | None = None) -> list[AlertJob]:
    jobs = []
    for href, parts in _anchors(body):
        if "jobListing.htm" not in href or len(parts) < 3:
            continue
        company, rest = parts[0], parts[1:]
        if rest and RATING.match(rest[0]):
            rest = rest[1:]
        if len(rest) < 2:
            continue
        title, location, tail = rest[0], rest[1], rest[2:]
        salary = next((p for p in tail if p.startswith("$")), None)
        age = next((AGE.match(p) for p in reversed(tail) if AGE.match(p)), None)
        posted = received
        if age and received:
            posted = received - (timedelta(hours=int(age[1])) if age[2] == "h" else timedelta(days=int(age[1])))
        job = _job("glassdoor-email", title, company, href, location=location, salary=salary,
                   posted=posted, extras={"easy_apply": "Easy Apply" in tail})
        if job:
            jobs.append(job)
    return jobs


# -- ZipRecruiter ----------------------------------------------------------------

ZIP_ACTIONS = {"view details", "apply now", "1-click apply", "be seen first", "estimated pay"}


def parse_ziprecruiter(body: str, received: datetime | None = None) -> list[AlertJob]:
    lines, jobs, cursor, seen = _lines(body), [], 0, set()
    for href, parts in _anchors(body):
        title = parts[0]
        if "ziprecruiter.com/" not in href or title.lower() in ZIP_ACTIONS or len(parts) > 1:
            continue
        i, rest = _after(lines, title, cursor)
        if i < 0 or not rest or "•" not in rest[0]:
            continue
        cursor = i + 1
        bits = [b.strip() for b in rest[0].split("•")]
        company, location = bits[0], ", ".join(b for b in bits[1:] if b) or None
        salary = rest[1] if len(rest) > 1 and rest[1].startswith("$") else None
        job = _job("ziprecruiter-email", title, company, href, location=location,
                   salary=salary, posted=received)
        if job and job.external_id not in seen:
            seen.add(job.external_id)
            jobs.append(job)
    return jobs


# -- Monster -----------------------------------------------------------------------

MONSTER_NAV = {"view job", "view all jobs", "view jobs in last 7 days", "sign in", "your profile",
               "contact us", "here", "modify your job alerts"}


def parse_monster(body: str, received: datetime | None = None) -> list[AlertJob]:
    lines, jobs, cursor = _lines(body), [], 0
    for href, parts in _anchors(body):
        title = parts[0]
        if "click.monster.com" not in href or title.lower() in MONSTER_NAV or len(parts) > 1:
            continue
        i, rest = _after(lines, title, cursor)
        if i < 0 or len(rest) < 2:
            continue
        cursor = i + 1
        company = rest[0]
        # "-", city, "-", state, "VIEW JOB"
        place = [r for r in rest[1:6] if r not in ("-",) and r.upper() != "VIEW JOB"][:2]
        job = _job("monster-email", title, company, href,
                   location=", ".join(place) or None, posted=received)
        if job:
            jobs.append(job)
    return jobs


# sender (as searched in IMAP) -> parser
PARSERS = {
    "dice@connect.dice.com": parse_dice,
    "noreply@glassdoor.com": parse_glassdoor,
    "alerts@ziprecruiter.com": parse_ziprecruiter,
    "notifications.monster.com": parse_monster,
}
