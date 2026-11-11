"""A listing site's link, followed to the employer's own posting (7 Nov 2026, step 4 of the Morning
shortlist plan).

A job found only on a site that lists other companies' jobs (Jobicy, Himalayas, Adzuna ...) is not
picked by the morning as it is: its form is that site's, or a wall. When such a job would otherwise be
picked, its listing page is read once -- with the polite reader the careers search uses: robots.txt
obeyed, one host at a time, and a hiring system's own pages never entered -- and the employer's
posting it links is taken only when all of these hold:

  * it is on a hiring system the app knows (Greenhouse, Lever, Ashby, Workday ...), reached by the
    listing's own redirect or named by a link on its page -- never made up from the company's name;
  * it is one job's posting, not a careers site or a list of jobs;
  * its board is plausibly this company's (a sidebar advert for another company's job is not it);
  * the listing names one such posting: two different ones and neither is taken.

What was found is saved on the job as the employer's posting (the person sees it, and can change it,
on the job's page); the job's own listing link is never changed. Every look is written down
(shortlist_links): where it started, where it led, what came of it. Nothing is found, or the site
turns the reader away: the job is left out, with the reason. A look that failed is tried again only
after a while (RETRY_AFTER), and a morning makes at most MAX_LOOKS looks, within DEADLINE_S.
Links from email alerts are never followed: they carry the person's own tracking.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

# Listing sites whose job pages are read; everything else that is not the employer's own hiring
# system (an email alert's link, a forum post) is not followed.
SUPPORTED = frozenset({"adzuna", "careerjet", "findwork", "himalayas", "jobicy", "jooble", "remoteok",
                       "remotive", "themuse", "weworkremotely", "workingnomads"})
FOUND, NONE, BLOCKED, ERROR = "found", "none", "blocked", "error"
RETRY_AFTER = {ERROR: timedelta(days=1), NONE: timedelta(days=7), BLOCKED: timedelta(days=7)}
MAX_LOOKS = 8
DEADLINE_S = 90

# A posting's form address, cut back to the posting: the form is opened from the posting (anyform).
_FORM_SUFFIX = re.compile(r"^(https://jobs\.(?:ashbyhq\.com|(?:eu\.)?lever\.co)/[^/?#]+/[0-9a-f-]{36})"
                          r"/(?:application|apply)/?$", re.I)


@dataclass
class Look:
    outcome: str              # found | none | blocked | error
    url: str | None           # the employer's posting, when found
    detail: str               # in the person's words: how it was found, or why not


def _site(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def posting_of(url: str) -> str:
    """The posting's own address: no tracking query, and not its form page."""
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}{parts.path}"
    if "gh_jid=" in parts.query:                     # the job's number, on an employer's own page
        jid = re.search(r"(?:^|&)gh_jid=(\d+)", parts.query)
        return f"{base}?gh_jid={jid.group(1)}" if jid else base
    m = _FORM_SUFFIX.match(base)
    return m.group(1) if m else base.rstrip("/")


def _whose(hit, company: str) -> bool:
    from data_engineering.companies.careers_detect import slug_matches_name
    slug = (hit.slug or "").split(":")[0]
    return bool(slug) and slug_matches_name(slug, company)


def look(job, fetch) -> Look:
    """Read the job's listing page once and say which employer posting it leads to, if one."""
    from backend.core.joblinks import POSTING, NotALink, clean, kind
    from data_engineering.companies.careers_detect import detect_html

    url = job.application_url or ""
    site = _site(url) or job.source
    if job.source not in SUPPORTED:
        return Look(NONE, None, f"links from {job.source} are not followed")
    page = fetch(url)
    if page.stopped_at_ats:
        hits = [page.stopped_at_ats]
    elif page.blocked or page.error in ("robots", "rate-limited"):
        why = {"robots": "its rules (robots.txt) ask apps not to read it",
               "rate-limited": "it asked the app to slow down"}.get(page.error or "", f"it turned the reader away ({page.blocked})")
        return Look(BLOCKED, None, f"{site} was not read: {why}")
    elif page.error:
        return Look(ERROR, None, f"{site} could not be read ({page.error}); it is tried again tomorrow")
    elif page.status in (404, 410):
        return Look(NONE, None, f"the listing on {site} is gone ({page.status})")
    elif page.status != 200:
        return Look(ERROR, None, f"{site} answered {page.status}; it is tried again tomorrow")
    else:
        hits = detect_html(page.html, page.final_url or url)
    postings: dict[str, str] = {}
    for hit in hits:
        if not _whose(hit, job.company or ""):
            continue
        try:
            found = clean(posting_of(hit.seen_at))
        except (NotALink, ValueError):
            continue
        if kind(found) == POSTING:
            postings.setdefault(found.lower(), found)
    if not postings:
        return Look(NONE, None, f"the listing on {site} links no posting of {job.company}'s on its own hiring system")
    if len(postings) > 1:
        return Look(NONE, None, f"the listing on {site} links {len(postings)} different postings of "
                                f"{job.company}'s, so none is taken")
    found = next(iter(postings.values()))
    return Look(FOUND, found, f"the employer's own posting ({_site(found)}), found from its listing on {site}")


def due(row, now: datetime) -> bool:
    """May this job's listing be read again? Never once found; a failed look after RETRY_AFTER."""
    if row is None:
        return True
    if row.outcome == FOUND:
        return False
    wait = RETRY_AFTER.get(row.outcome, RETRY_AFTER[ERROR])
    at = row.at if row.at.tzinfo else row.at.replace(tzinfo=now.tzinfo)
    return now - at >= wait


def latest(session) -> dict:
    """job id -> its latest look."""
    from data_engineering.db.models import ShortlistLink
    rows = session.query(ShortlistLink).order_by(ShortlistLink.id.asc()).all()
    return {r.job_id: r for r in rows}


def keep(job_id: int, from_url: str, result: Look, now: datetime) -> None:
    """Write the look down; a posting found becomes the job's employer posting, unless the person saved
    one of their own. The job's listing link stays as it was."""
    from backend.apply import execution
    from backend.core.joblinks import POSTING
    from data_engineering.db.models import Job, ShortlistLink
    from data_engineering.db.session import get_session

    plain = now.replace(tzinfo=None) if now.tzinfo else now

    @execution._when_free
    def write():
        with get_session() as session:
            session.add(ShortlistLink(job_id=job_id, from_url=from_url[:1024], url=result.url,
                                      outcome=result.outcome, detail=result.detail, at=plain))
            if result.outcome == FOUND and result.url:
                job = session.get(Job, job_id)
                if job is not None and not job.employer_url:
                    job.employer_url, job.employer_url_kind = result.url, POSTING
    write()


def default_fetch(cfg):
    """The careers search's polite reader: robots.txt, per-host pacing, never inside a hiring system."""
    from data_engineering.companies.careers_finder import make_client
    return make_client(cfg).fetch


class Looker:
    """The morning's looks: at most MAX_LOOKS, within DEADLINE_S; a job looked at before, and not due
    again, keeps what its last look found."""

    def __init__(self, fetch_for, rows: dict, now: datetime, *, max_looks: int | None = None,
                 deadline_s: float | None = None, clock=time.monotonic):
        self._fetch_for, self._fetch = fetch_for, None
        self.rows, self.now = rows, now
        self.max_looks = MAX_LOOKS if max_looks is None else max_looks
        self.clock = clock
        self.deadline = clock() + (DEADLINE_S if deadline_s is None else deadline_s)
        self.looks = 0
        self.found: dict[int, Look] = {}

    def known(self, job_id: int) -> Look | None:
        """What a look not yet due again found -- None when the job may be looked at."""
        row = self.rows.get(job_id)
        if row is None or due(row, self.now):
            return None
        return Look(row.outcome, row.url, row.detail)

    def look(self, job) -> Look | None:
        """One look, or None when this morning's looks are used up."""
        if self.looks >= self.max_looks or self.clock() > self.deadline:
            return None
        self.looks += 1
        if self._fetch is None:
            self._fetch = self._fetch_for()
        try:
            result = look(job, self._fetch)
        except Exception as exc:                     # a reader's fault is this job's error, not the morning's
            result = Look(ERROR, None, f"the listing could not be read ({type(exc).__name__}); "
                                       "it is tried again tomorrow")
        keep(job.id, job.application_url or "", result, self.now)
        if result.outcome == FOUND:
            self.found[job.id] = result
        return result
