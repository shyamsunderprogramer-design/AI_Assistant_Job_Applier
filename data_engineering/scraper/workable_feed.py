"""Workable's official job feed: every job its customers publish on Jobs by Workable, in one file (Nov 2026).

https://www.workable.com/boards/workable.xml is the XML feed Workable offers job boards it has no integration
with (help.workable.com, "Utilizing the XML job feed"): updated every hour, one request an hour at most, the whole
file each time (no filtering parameters), compressed (a request without gzip is refused with 403, one too many
with 429), and each job's URL used exactly as given, because Workable attributes candidates by it. In Nov 2026 it
was about 150 MB compressed, 875 MB of XML and 170,000 jobs from 5,300 companies.

It does NOT replace reading the Workable boards. Measured 26 Nov 2026: of the jobs our board reads had confirmed
open since 23 Nov, 25 of 28 were in it (a job is in it only if published on Jobs by Workable), and 50 of the 67
boards then listing jobs. So:

  * its postings are stored under their own source, `workable-feed`, never as a board's;
  * a job the boards already hold (the same Workable shortcode) is never added again, and a feed copy whose
    board copy arrives later is closed if nobody acted on it (devops/discovery_merge.py, merge_feeds);
  * nothing a board holds is ever closed or changed because the feed left it out;
  * a feed posting is closed only after it has been missing from complete, checked copies of the feed for
    CLOSE_AFTER_DAYS (a cut-short or much smaller file closes nothing).

The file is streamed to disk and parsed as a stream by expat with every DOCTYPE, entity declaration and external
entity refused, keeping only the postings the search wants (and the shortcodes, to see which known ones are still
listed). Its log lines name companies and titles: they go to the caller's private log, never to stdout.
"""

from __future__ import annotations

import gzip
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from xml.parsers import expat

log = logging.getLogger(__name__)

SOURCE, LABEL = "workable-feed", "Workable job feed"
URL = "https://www.workable.com/boards/workable.xml"
BOARD_SOURCE = "workable"
CLOSE_AFTER_DAYS = 3
MAX_BYTES = 1_500_000_000          # compressed: ten times its Nov 2026 size; a bigger answer is not this feed
MIN_SHARE = 0.8                    # a copy missing over 20% of the postings seen in the last day is incomplete
MIN_JOBS = 20_000                  # the whole feed held 170,000 in Nov 2026
DEADLINE_S = 900
FIELDS = {"title", "date", "referencenumber", "url", "company", "city", "state", "country", "remote",
          "description", "jobtype"}


class Refused(ValueError):
    """The document declared a DOCTYPE or an entity: never this feed, and never expanded."""


@dataclass
class Summary:
    name: str = SOURCE
    status: str = "skipped"         # read | failed | skipped
    why: str = ""
    calls: int = 0
    bytes: int = 0
    jobs: int = 0                    # in the file
    matched: int = 0
    new: int = 0
    seen: int = 0
    on_boards: int = 0               # already held by the Workable boards: not added again
    duplicates: int = 0              # the same company and title already open from another source
    retired: int = 0
    complete: bool = False
    seconds: dict = field(default_factory=dict)


# -- the download ---------------------------------------------------------------------------------------------

def download(dest: Path, session=None, deadline_s: int = DEADLINE_S) -> int:
    """The compressed file into `dest`, kept compressed. One request; a 403, a 429 or any other refusal is
    reported, never retried (one request an hour is the rule). Returns its size."""
    import requests
    s = session or requests.Session()
    headers = {"Accept-Encoding": "gzip", "User-Agent": os.getenv("SCRAPER_USER_AGENT") or "AI-Job-Applier feed reader"}
    started = time.monotonic()
    with s.get(URL, headers=headers, stream=True, timeout=(30, 120)) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        if (resp.headers.get("Content-Encoding") or "").lower() != "gzip":
            raise RuntimeError("the feed did not come compressed")
        size = 0
        with open(dest, "wb") as fh:
            for chunk in resp.raw.stream(1 << 20, decode_content=False):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise RuntimeError("the feed is larger than any copy of it seen")
                if time.monotonic() - started > deadline_s:
                    raise TimeoutError("the download took too long")
                fh.write(chunk)
    return size


# -- one request an hour, across runs ------------------------------------------------------------------------------

MIN_GAP_S = 3600 + 5 * 60           # Workable's hour, and a margin


def _utc(stamp: str):
    from datetime import datetime, timezone
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)


def too_soon(now=None, api=None) -> str:
    """Why the feed must not be asked now ("" when it may): it was read here within the hour (the saved
    database's newest feed posting), or another run of this workflow started its job-search-sites job within
    the hour (whatever came of it: a refused request counts too). When GitHub cannot be asked, it is not
    asked either: one skipped read costs less than a second request inside the hour."""
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc)
    try:
        from sqlalchemy import func

        from data_engineering.db.models import Job
        from data_engineering.db.session import get_session
        with get_session() as session:
            last = session.query(func.max(Job.last_seen_at)).filter(Job.source == SOURCE).scalar()
        if last is not None:
            last = last.replace(tzinfo=timezone.utc) if last.tzinfo is None else last
            if (now - last).total_seconds() < MIN_GAP_S:
                return f"read {int((now - last).total_seconds() // 60)} min ago"
    except Exception:                                             # noqa: BLE001 -- the database is checked below
        log.warning("the feed's last read could not be looked up", exc_info=True)
    repo, run_id = os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_RUN_ID")
    if not repo:
        return ""                                                 # not on GitHub: nothing else reads it
    get = api or _github_get
    try:
        since = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        runs = get(f"repos/{repo}/actions/workflows/daily-scrape.yml/runs?created=>={since}&per_page=20")
        for run in runs.get("workflow_runs") or []:
            if str(run.get("id")) == str(run_id):
                continue
            for job in get(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100").get("jobs") or []:
                started = job.get("started_at")
                if (job.get("name") == "feeds" and started and job.get("conclusion") not in ("skipped", "cancelled")
                        and (now - _utc(started)).total_seconds() < MIN_GAP_S):
                    return f"run {run['id']} asked it at {started[11:16]} UTC"
    except Exception as exc:                                      # noqa: BLE001
        return f"the other runs could not be checked ({type(exc).__name__})"
    return ""


def _github_get(path: str) -> dict:
    import requests
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
    resp = requests.get(f"https://api.github.com/{path}", timeout=30,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    resp.raise_for_status()
    return resp.json()


# -- the parse ---------------------------------------------------------------------------------------------------

def _refuse(*_args):
    raise Refused("a DOCTYPE or entity declaration")


def parse(path: Path, keep) -> tuple[list[dict], set[str]]:
    """Stream the compressed file: (the postings `keep(job)` accepts, every shortcode in the file).
    Raises Refused or expat.ExpatError for a document that is not a well-formed feed, and EOFError when the
    file was cut short."""
    p = expat.ParserCreate()
    p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    p.StartDoctypeDeclHandler = _refuse
    p.EntityDeclHandler = _refuse
    p.UnparsedEntityDeclHandler = _refuse
    p.ExternalEntityRefHandler = _refuse
    p.buffer_text = True
    kept: list[dict] = []
    codes: set[str] = set()
    state: dict = {"job": None, "field": None, "buf": []}

    def start(name, _attrs):
        if name == "job":
            state["job"] = {}
        elif state["job"] is not None and name in FIELDS:
            state["field"], state["buf"] = name, []

    def end(name):
        job = state["job"]
        if job is None:
            return
        if name == "job":
            code = job.get("referencenumber", "")
            if code:
                codes.add(code)
                if keep(job):
                    kept.append(job)
            state["job"] = None
        elif name == state["field"]:
            job[name] = "".join(state["buf"]).strip()
            state["field"] = None

    def text(s):
        if state["field"] is not None:
            state["buf"].append(s)

    p.StartElementHandler, p.EndElementHandler, p.CharacterDataHandler = start, end, text
    with gzip.open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            p.Parse(chunk, False)
    p.Parse(b"", True)
    return kept, codes


def location(job: dict) -> str | None:
    place = ", ".join(x for x in (job.get("city"), job.get("state"), job.get("country")) if x)
    if (job.get("remote") or "").lower() == "true":
        return f"{place} (Remote)" if place else "Remote"
    return place or None


def raw_job(job: dict):
    from data_engineering.scraper.feeds.common import posting
    raw = posting(SOURCE, job_id=job.get("referencenumber"), title=job.get("title"), company=job.get("company"),
                  url=job.get("url"), location=location(job), description=job.get("description"),
                  posted=job.get("date"))
    if raw is not None:
        raw.job_type = job.get("jobtype") or None
    return raw


# -- storing -----------------------------------------------------------------------------------------------------

def _key(company: str, title: str) -> tuple[str, str]:
    from data_engineering.scraper.feeds.common import _key as key
    return key(company, title)


def complete_copy(jobs: int, codes: set[str], recent: set[str]) -> tuple[bool, str]:
    """Whether this copy is the whole feed: enough jobs, and most of the feed postings seen in the last day still
    in it (the feed is a snapshot; a cut-short or partial copy loses a large share of them at once)."""
    if jobs < MIN_JOBS:
        return False, f"{jobs} jobs: the whole feed has had far more"
    if len(recent) >= 20:
        share = len(recent & codes) / len(recent)
        if share < MIN_SHARE:
            return False, f"{share:.0%} of the postings seen in the last day are in it"
    return True, ""


def store(kept: list[dict], codes: set[str], s: Summary) -> None:
    """Add the new postings, mark the known ones seen, and (only from a complete copy) close the feed postings
    missing for CLOSE_AFTER_DAYS. Never touches a posting of any other source."""
    import hashlib
    from datetime import timedelta

    from data_engineering.db.models import Job, utcnow
    from data_engineering.db.session import get_session

    with get_session() as session:
        now = utcnow()
        naive = now.replace(tzinfo=None)
        on_boards = {e for (e,) in session.query(Job.external_id).filter(Job.source == BOARD_SOURCE)}
        mine = session.query(Job).filter(Job.source == SOURCE).all()
        recent = {r.external_id for r in mine if r.is_open and r.last_seen_at
                  and r.last_seen_at >= naive - timedelta(days=1)}
        s.complete, why = complete_copy(s.jobs, codes, recent)
        if not s.complete:
            s.why = why + ": nothing is closed"
        held = {r.external_id for r in mine}
        for row in mine:
            if row.external_id in codes:
                row.last_seen_at = now
        if s.complete:
            cutoff = naive - timedelta(days=CLOSE_AFTER_DAYS)
            for row in mine:
                if row.is_open and row.external_id not in codes and row.last_seen_at and row.last_seen_at < cutoff:
                    row.is_open, row.closed_at = False, now
                    s.retired += 1
        elsewhere = {_key(c, t) for src, c, t in session.query(Job.source, Job.company, Job.title)
                     .filter(Job.is_open.is_(True)) if src != SOURCE}
        done: set[str] = set()
        for job in kept:
            code = job.get("referencenumber") or ""
            if code in done:
                continue                    # the feed lists some jobs more than once
            done.add(code)
            if code in on_boards:
                s.on_boards += 1
                continue
            if code in held:
                s.seen += 1
                continue
            raw = raw_job(job)
            if raw is None:
                continue
            if _key(raw.company, raw.title) in elsewhere:
                s.duplicates += 1
                continue
            session.add(Job(
                source=SOURCE, company=raw.company, company_slug=raw.company_slug, external_id=raw.external_id,
                title=raw.title, location=raw.location, description=raw.description,
                requirements=raw.description, application_url=raw.application_url, posted_at=raw.posted_at,
                stated_job_type=(raw.job_type or None) and raw.job_type[:40],
                content_hash=hashlib.sha256(f"{SOURCE}{raw.external_id}{raw.title}".encode()).hexdigest(),
                is_open=True, last_seen_at=now,
            ))
            s.new += 1


def run(cfg, session=None, workdir: Path | None = None) -> Summary:
    """Download, check, parse and store the feed once. A failure stores nothing and closes nothing."""
    from data_engineering.scraper.filters import resolve_filter

    s = Summary()
    if not bool(cfg.get("feeds.workable_xml.enabled", True)):
        s.why = "disabled in config"
        return s
    wait = too_soon()
    if wait:
        s.why = f"asked at most once an hour: {wait}"
        return s
    flt = resolve_filter(cfg)
    tmp = Path(tempfile.mkdtemp(dir=workdir)) / "workable.xml.gz"
    try:
        t = time.monotonic()
        s.calls = 1
        try:
            s.bytes = download(tmp, session=session)
        except Exception as exc:                                  # noqa: BLE001 -- reported, never retried
            s.status, s.why = "failed", f"download: {type(exc).__name__}: {str(exc)[:120]}"
            return s
        s.seconds["download"] = round(time.monotonic() - t, 1)

        from types import SimpleNamespace

        def keep(job: dict) -> bool:
            probe = SimpleNamespace(title=job.get("title"), location=location(job), description="")
            if not flt.matches_stub(probe):                       # type: ignore[arg-type]
                return False
            from data_engineering.scraper.feeds.common import clean
            probe.description = clean(job.get("description"))
            return flt.matches(probe)                             # type: ignore[arg-type]

        t = time.monotonic()
        try:
            kept, codes = parse(tmp, keep)
        except (Refused, expat.ExpatError, EOFError, OSError) as exc:
            s.status, s.why = "failed", f"parse: {type(exc).__name__}"
            return s
        s.seconds["parse_and_filter"] = round(time.monotonic() - t, 1)
        s.jobs, s.matched = len(codes), len(kept)

        t = time.monotonic()
        store(kept, codes, s)
        s.seconds["store"] = round(time.monotonic() - t, 1)
        s.status = "read"
        log.info("Workable feed: %s", s)
        return s
    finally:
        try:
            tmp.unlink(missing_ok=True)
            tmp.parent.rmdir()
        except OSError:
            pass
