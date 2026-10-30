"""The person's watch list: companies whose openings they follow, and jobs saved for later.

A company is matched by its name however each board writes it -- "ACCENTURE LLP" is Accenture,
"GlobexCorp" is Globex Corp -- using the careers finder's own name keys. Each watched company shows
its open roles that fit the search, the boards they come from, and what has arrived since the
person last looked. A company the daily fetch does not read yet is put on it: from a link to its
job board when the person pastes one, or by finding its careers page and the board behind it.
Its board is then read straight away, so its openings show in minutes, not the next morning.

A job is saved by its id. The list says where each stands -- open or closed, applied or not --
and, when the posting names one, the date applications close.

Kept in data/watchlist.json, which is the person's own.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import threading
from datetime import datetime, timezone
from functools import lru_cache

from backend.config.loader import PROJECT_ROOT

log = logging.getLogger(__name__)

PATH = PROJECT_ROOT / "data" / "watchlist.json"
BOARD_FILE = PROJECT_ROOT / "data" / "watchlist_board.csv"
BOARDS_CSV = PROJECT_ROOT / "backend" / "config" / "boards.csv"
_lock = threading.Lock()

APPLIED = ("Applied", "Interviewing", "Offer", "Rejected")


class NotWatched(ValueError):
    """A request the watch list cannot take, with a reason the person can act on."""


# -- names -------------------------------------------------------------------------------------

@lru_cache(maxsize=65536)
def company_key(name: str | None) -> str:
    """One key per company however it is written: brackets, legal endings, case and spaces dropped."""
    from data_engineering.companies.careers_detect import name_key
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", name or "")
    return re.sub(r"[^a-z0-9]", "", name_key(text) or "")


def _looks_like_link(text: str) -> bool:
    return bool(re.match(r"(https?://|www\.)", text.strip(), re.I)) or ("/" in text and "." in text.split("/")[0])


# -- the file ------------------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> dict:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("companies", [])
    data.setdefault("jobs", [])
    return data


def _save(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(PATH)


def _update(key: str, **fields) -> None:
    with _lock:
        data = load()
        for entry in data["companies"]:
            if entry["key"] == key:
                entry.update(fields)
        _save(data)


def watched_keys() -> set[str]:
    return {c["key"] for c in load()["companies"]}


def starred_ids() -> set[int]:
    return {int(j["id"]) for j in load()["jobs"]}


def watched_job_ids(jobs) -> set[int]:
    """Which of these jobs are at a watched company, matched the way `companies_view` matches."""
    keys = watched_keys()
    if not keys:
        return set()
    return {job.id for job in jobs
            if company_key(job.company) in keys or company_key(job.company_slug) in keys}


# -- companies ---------------------------------------------------------------------------------------

def add_company(text: str, on_board=None) -> dict:
    """Watch a company, named or given by a link to its job board. Returns its entry.

    `on_board(entry)` is called, on a thread of its own, when a link names a board: the board is
    added to the daily fetch and read straight away (find_board does the same for a bare name).
    """
    from data_engineering.companies.careers_detect import detect_url, domain_root
    text = " ".join((text or "").split())
    if not text:
        raise NotWatched("Type a company's name, or paste a link to its job board.")
    hit, link = None, None
    if _looks_like_link(text):
        link = text if text.lower().startswith("http") else "https://" + text
        hit = detect_url(link)
    name = text
    if hit is not None:
        name = _board_name(hit.system, hit.slug) or (hit.slug or "").replace("-", " ").title()
    elif link:
        name = domain_root(link).replace("-", " ").title() or text     # a careers page: the site's name
    key = company_key(name)
    if not key:
        raise NotWatched("That name has no letters or digits to match on. Type the company's name.")
    with _lock:
        data = load()
        same = next((c for c in data["companies"] if c["key"] == key), None)
        if same is not None:
            raise NotWatched(f"{same['name']} is on your watch list already.")
        entry: dict = {"name": name, "key": key, "added": _now(), "seen": _now()}
        if link and hit is None:
            entry["careers_url"] = link          # a careers page, not a board: looked up from there
        data["companies"].append(entry)
        _save(data)
    if hit is not None and hit.slug:
        entry["lookup"] = {"state": "found", "system": hit.system, "slug": hit.slug,
                           "board_url": hit.board_url, "at": _now()}
        _update(key, lookup=entry["lookup"])
        if on_board is not None:
            threading.Thread(target=on_board, args=(entry,), daemon=True).start()
    return entry


def remove_company(key: str) -> bool:
    with _lock:
        data = load()
        kept = [c for c in data["companies"] if c["key"] != key]
        if len(kept) == len(data["companies"]):
            return False
        data["companies"] = kept
        _save(data)
    return True


def mark_seen(key: str) -> None:
    """The person has looked: what arrived before now is no longer new."""
    _update(key, seen=_now())


def _board_name(system: str, slug: str | None) -> str | None:
    from data_engineering.db.models import Company
    from data_engineering.db.session import get_session
    try:
        with get_session() as session:
            row = session.query(Company).filter(Company.source == system, Company.slug == slug).first()
            return row.name if row and row.name else None
    except Exception:
        return None


# -- what a watched company has ----------------------------------------------------------------------

def _when(text: str | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text) if text else None
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment and moment.tzinfo is None else moment


def _row(job, seen: str | None) -> dict:
    from backend.core.jobage import age_days, age_label
    found, since = _when(job.found_at.isoformat() if job.found_at else None), _when(seen)
    return {"id": job.id, "title": job.title, "company": job.company, "location": job.location or "",
            "score": None if job.ats_match_score is None else round(job.ats_match_score * 100),
            "age": age_label(job), "ageDays": age_days(job), "status": job.status, "board": job.source,
            "new": bool(found and since and found > since and job.status not in APPLIED)}


def _one_per_role(rows: list[dict]) -> list[dict]:
    """A role posted for several cities, shown once: the first of them, and where else it is."""
    kept: dict[str, dict] = {}
    for row in rows:
        title = re.sub(r"\s+", " ", row["title"]).strip().lower()
        if title in kept:
            first = kept[title]
            first["more"] = first.get("more", 0) + 1
            if row["location"] and len(first.setdefault("places", [])) < 3:
                first["places"].append(row["location"])
            first["new"] = first["new"] or row["new"]
            continue
        kept[title] = {**row}
    return list(kept.values())


def companies_view() -> list[dict]:
    """Every watched company: its open roles (best fit first), its boards, and what is new."""
    from data_engineering.db.models import Company, Job
    from data_engineering.db.session import get_session
    entries = load()["companies"]
    by_key = {c["key"]: {**c, "jobs": [], "boards": []} for c in entries}
    if not by_key:
        return []
    with get_session() as session:
        for board in session.query(Company).filter(Company.active.is_(True)):
            for key in {company_key(board.name), company_key(board.slug)}:
                if key in by_key:
                    by_key[key]["boards"].append({"system": board.source, "slug": board.slug,
                                                  "url": board.board_url or ""})
                    break
        for job in session.query(Job).filter(Job.is_open.is_(True)):
            key = company_key(job.company)
            if key not in by_key:
                key = company_key(job.company_slug)
            if key in by_key:
                by_key[key]["jobs"].append(_row(job, by_key[key].get("seen")))
    out = []
    for entry in by_key.values():
        # New first, then best fit; the same role posted for several places is one row.
        entry["jobs"].sort(key=lambda r: (not r["new"], r["score"] is None, -(r["score"] or 0)))
        entry["jobs"] = _one_per_role(entry["jobs"])
        entry["new"] = sum(1 for r in entry["jobs"] if r["new"])
        out.append(entry)
    out.sort(key=lambda e: (-e["new"], e["name"].lower()))
    return out


# -- saved jobs -----------------------------------------------------------------------------------------

def star(job_id: int, on: bool = True) -> bool:
    """Save a job to the watch list, or take it off. Returns whether it is on it now."""
    with _lock:
        data = load()
        jobs = [j for j in data["jobs"] if int(j["id"]) != int(job_id)]
        if on:
            jobs.append({"id": int(job_id), "added": _now()})
        data["jobs"] = jobs
        _save(data)
    return on


_DEADLINE = re.compile(
    r"(?:apply\s+by|applications?\s+(?:close|are\s+due|due|deadline|accepted\s+(?:until|through))|"
    r"closing\s+date|deadline(?:\s+to\s+apply)?|accepting\s+applications\s+(?:until|through)|"
    r"open\s+until|posting\s+(?:closes|ends))\s*(?:on|is|:|-)?\s*"
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})", re.I)


def deadline(text: str | None) -> str | None:
    """The date applications close, when the posting says it in so many words."""
    m = _DEADLINE.search(text or "")
    return m.group(1).strip() if m else None


def jobs_view() -> list[dict]:
    """The saved jobs, newest saved first, with where each stands."""
    from backend.core.jobage import age_label
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session
    saved = load()["jobs"]
    ids = [int(j["id"]) for j in saved]
    added = {int(j["id"]): j.get("added", "") for j in saved}
    out = []
    with get_session() as session:
        for job in session.query(Job).filter(Job.id.in_(ids)) if ids else []:
            out.append({"id": job.id, "title": job.title, "company": job.company, "location": job.location or "",
                        "score": None if job.ats_match_score is None else round(job.ats_match_score * 100),
                        "age": age_label(job), "status": job.status, "open": bool(job.is_open),
                        "closed": job.closed_at.date().isoformat() if job.closed_at and not job.is_open else None,
                        "deadline": deadline(job.description), "added": added.get(job.id, "")})
    out.sort(key=lambda r: r["added"], reverse=True)
    return out


# -- putting a company's board on the daily fetch -----------------------------------------------------

def add_board(system: str, slug: str, name: str, board_url: str | None = None) -> bool:
    """One board into the daily fetch, through the same import the board list uses. True when new."""
    from data_engineering import boards
    from data_engineering.db.models import Company
    from data_engineering.db.session import get_session
    with get_session() as session:
        if session.query(Company).filter(Company.source == system, Company.slug == slug).first():
            return False
    BOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(BOARD_FILE, "w", newline="", encoding="utf-8") as handle:
        out = csv.writer(handle)
        out.writerow(("source", "slug", "name", "board_url", "origin"))
        out.writerow((system, slug, name, board_url or "", "watch-list"))
    boards.import_(BOARD_FILE)
    tmp = BOARDS_CSV.with_suffix(".watch.tmp")     # the board list the fetch is backed up to
    boards.export(tmp)
    tmp.replace(BOARDS_CSV)
    return True


def read_board_now(cfg, entry: dict) -> None:
    """The company's board read now and its new openings scored, so they show within minutes."""
    from data_engineering.scraper.base import CompanyRef
    from data_engineering.scraper.runner import scrape_boards
    from ml.resume.pipeline import score_jobs
    found = entry.get("lookup") or {}
    system, slug = found.get("system"), found.get("slug")
    if not (system and slug):
        return
    key = entry["key"]
    try:
        new = add_board(system, slug, entry["name"], found.get("board_url"))
        _update(key, lookup={**found, "state": "reading", "added_to_fetch": new, "at": _now()})
        summary = scrape_boards(cfg, [CompanyRef(name=entry["name"], slug=slug, source=system)])
        score_jobs(cfg)
        _update(key, lookup={**found, "state": "found", "added_to_fetch": new, "at": _now(),
                             "read": {"kept": summary.jobs_kept, "new": summary.jobs_new}})
    except Exception as exc:                       # the entry says so; the page offers to try again
        log.warning("Reading %s/%s for the watch list failed: %s", system, slug, exc)
        _update(key, lookup={**found, "state": "error", "detail": f"{type(exc).__name__}: {str(exc)[:160]}",
                             "at": _now()})


def find_board(cfg, key: str) -> None:
    """Find a watched company's careers page and the board behind it; put the board on the fetch.

    Runs on a thread of its own: the company's website comes from the company lists, and the
    careers finder follows it to the hiring system -- a minute or two, the first time a name is
    looked up in the big company list.
    """
    from data_engineering.companies import careers_finder as cf
    from data_engineering.companies.careers_detect import registrable_domain
    entry = next((c for c in load()["companies"] if c["key"] == key), None)
    if entry is None:
        return
    name = entry["name"]
    _update(key, lookup={"state": "looking", "at": _now()})
    try:
        db = cf.connect()
        index = cf.DomainIndex(cache_path=PROJECT_ROOT / "data" / "careers" / "watch_name_domains.json")
        index.load_registry(db)
        hint = entry.get("careers_url")
        domain, how = (registrable_domain(hint), "the link you gave") if hint else index.lookup(name)
        if not domain:
            index.prepare([name], say=lambda *a, **k: None)
            domain, how = index.lookup(name)
        if not domain:
            _update(key, lookup={"state": "not_found", "at": _now(),
                                 "detail": f"No website for {name} in the company lists. Paste a link to its "
                                           "careers page or job board instead."})
            return
        client = cf.make_client(cfg)
        finder = cf.Finder(lambda url, **kw: client.fetch(url, **kw), index)
        outcome = finder.find(cf.Item("watch", key, name, domain=domain, domain_source=how, careers_hint=hint))
        hit = outcome.hit
        if hit is not None and hit.slug and hit.system in cf.WATCHED_SYSTEMS:
            found = {"state": "found", "system": hit.system, "slug": hit.slug,
                     "board_url": hit.board_url or cf._board_url(hit.system, hit.slug), "careers_url": outcome.careers_url,
                     "at": _now()}
            _update(key, lookup=found)
            read_board_now(cfg, {**entry, "lookup": found})
            return
        detail = (f"Its careers page is {outcome.careers_url}" + (f", on {hit.system}" if hit else "")
                  + ", which the app cannot read yet." if outcome.careers_url
                  else (outcome.detail or f"No careers page found on {domain}."))
        _update(key, lookup={"state": "not_found", "careers_url": outcome.careers_url, "detail": detail, "at": _now()})
    except Exception as exc:
        log.warning("Looking up %s for the watch list failed: %s", name, exc)
        _update(key, lookup={"state": "error", "detail": f"{type(exc).__name__}: {str(exc)[:160]}", "at": _now()})
