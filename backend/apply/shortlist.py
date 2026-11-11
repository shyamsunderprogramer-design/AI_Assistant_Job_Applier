"""The morning's picks: the strongest jobs found, added to the chosen-jobs list for the person to review
(3 Nov 2026, step 1 of the Morning shortlist plan).

At the end of the morning run, and again after the night's top-up, the best open matches are checked one
by one, best score first, and up to `shortlist.per_day` (5) a day are added to "Your chosen jobs" -- the
same list the person fills from the Jobs page (backend/apply/chosen.py, execution.py). Nothing else:

  * no resume is tailored, no cover letter written, no form opened and nothing sent. The person prepares
    a picked job when they press Prepare these, and applies when they press Apply to these, as for the
    jobs they tick themselves (their choice of 21 Oct 2026, kept on 3 Nov: "Do NOT automatically tailor");
  * the jobs the person chose themselves are never touched, moved or counted against the day's picks.

Off until switched on (`shortlist.enabled: true` in config.yaml). The settings, all under `shortlist.`:

  enabled                   false        nothing is picked, and the morning run has no shortlist step
  per_day                   5            at most this many picked in one day, the top-up included
  min_fit                   0.80         the lowest match score considered, on a full posting's score
  required_skills           []           when set, a posting must mention at least one of them
  max_years_required        none         when set, a posting asking for more years than this is left out
  workplaces                []           e.g. [remote, hybrid]; a posting that does not say is not left out
  job_types                 []           e.g. [full-time]; a posting that does not say is not left out
  min_salary                none         a yearly figure; only a posting that states its pay is held to it
  exclude_companies         []           never picked; the person's current employer always is excluded
  allow_needs_confirmation  false        a job whose eligibility needs the person's answer is left out
  max_considered            400          how many of the best matches are looked at, at most

Which jobs are never picked, each with the reason written down (data/apply/shortlist.json): one the person
removed (for good) or put off until a date not yet passed; one on the chosen list now or before, whatever
became of it; one applied to, by the app's record or the person's mark; one picked on an earlier day; one
whose link goes to a listing site (Adzuna, Jobicy, an alert email ...) unless the person saved the employer's
own posting on the job's page, or the listing's page leads to it (backend/apply/listings.py: read last, for a
job every other rule lets through, a few a morning); the same title at the same company as a job applied to, on the list or picked
today; a second job at a company with one picked today or on the list; one any run's rule rules out (closed,
no link, marked, a requirement the profile rules out, abroad, too old); one Not eligible -- never -- or
needing the person's answer (unless allowed); one the person's own preferences leave out.

Visa sponsorship a posting does not mention is unknown: such a job can be picked, labelled "Eligible with
warnings -- sponsorship unknown". A company's past H-1B record is no part of the choice.

Deterministic: the same jobs and the same settings give the same picks, best score first, then the oldest
job id. Idempotent: a job is picked once, ever (shortlist_picks.job_id is unique), in the same transaction
as its entry on the list, so a second morning run, a top-up or two processes at once add nothing twice.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from backend.config.loader import PROJECT_ROOT

RULE_VERSION = "2026-11-07.1"      # 11-07: a listing's link followed to the employer's own posting
                                   # 11-04.1: Not today and Restore bring a job back to be considered
REPORT = PROJECT_ROOT / "data" / "apply" / "shortlist.json"
# The person's own settings, from the Settings page (4 Nov 2026): over config.yaml's, key by key. Its own
# file, so saving them never rewrites config.yaml and its notes. No file: config.yaml's, then the defaults.
SETTINGS = PROJECT_ROOT / "data" / "apply" / "shortlist_settings.json"
WORKPLACES = ("remote", "hybrid", "onsite")

DEFAULTS = {
    "enabled": False, "per_day": 5, "min_fit": 0.80, "required_skills": [], "max_years_required": None,
    "workplaces": [], "job_types": [], "min_salary": None, "exclude_companies": [],
    "allow_needs_confirmation": False, "max_considered": 400,
}
REMOVED, DEFERRED, RESTORED = "removed", "deferred", "restored"
SPONSORSHIP_UNKNOWN = "Eligible with warnings — Sponsorship unknown"
HOURS_A_YEAR = 2080


# -- settings ------------------------------------------------------------------------------------------

def _words(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def saved() -> dict:
    """The settings saved from the Settings page; {} when none were, or the file is unreadable."""
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in DEFAULTS} if isinstance(data, dict) else {}


def settings(cfg) -> dict:
    """The shortlist's settings: the ones saved from the Settings page, else config.yaml's, else the
    defaults -- key by key."""
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        value = cfg.get(f"shortlist.{key}", None)
        if value is not None:
            out[key] = value
    out.update({k: v for k, v in saved().items() if v is not None or k in ("max_years_required", "min_salary")})
    out["enabled"] = out["enabled"] is True
    out["per_day"] = max(0, int(out["per_day"]))
    out["min_fit"] = float(out["min_fit"])
    out["max_considered"] = max(1, int(out["max_considered"]))
    out["allow_needs_confirmation"] = out["allow_needs_confirmation"] is True
    for key in ("required_skills", "workplaces", "job_types", "exclude_companies"):
        out[key] = _words(out[key])
    for key in ("max_years_required", "min_salary"):
        out[key] = None if out[key] in (None, "") else float(out[key])
    return out


def enabled(cfg) -> bool:
    return settings(cfg)["enabled"]


def save_settings(values: dict) -> dict:
    """Check and keep the settings sent from the Settings page; only the keys sent change. Raises
    ValueError, in words for the page, on anything out of range: nothing is kept then."""
    from backend.core.jobfields import JOB_TYPES
    if not isinstance(values, dict):
        raise ValueError("Send the settings as an object.")
    unknown = sorted(set(values) - set(DEFAULTS) - {"max_considered"})
    if unknown or "max_considered" in values:
        raise ValueError(f"Not a setting here: {', '.join(unknown or ['max_considered'])}.")
    out = saved()

    def number(key, low, high, *, whole=False, optional=False):
        value = values[key]
        if value in (None, "") and optional:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high \
                or (whole and int(value) != value):
            raise ValueError(f"{key.replace('_', ' ')}: a {'whole ' if whole else ''}number from {low:g} to {high:g}.")
        return int(value) if whole else float(value)

    def names(key, allowed=None):
        value = values[key]
        items = [s.strip() for s in value.split(",")] if isinstance(value, str) else value
        if not isinstance(items, list) or not all(isinstance(s, str) for s in items):
            raise ValueError(f"{key.replace('_', ' ')}: a list of words.")
        items = list(dict.fromkeys(s.strip() for s in items if s.strip()))
        if len(items) > 50 or any(len(s) > 80 for s in items):
            raise ValueError(f"{key.replace('_', ' ')}: at most 50, each under 80 letters.")
        if allowed is not None and set(items) - set(allowed):
            raise ValueError(f"{key.replace('_', ' ')}: only {', '.join(allowed)}.")
        return items

    for key in values:
        if key in ("enabled", "allow_needs_confirmation"):
            if not isinstance(values[key], bool):
                raise ValueError(f"{key.replace('_', ' ')}: on or off.")
            out[key] = values[key]
        elif key == "per_day":
            out[key] = number(key, 1, 25, whole=True)
        elif key == "min_fit":
            out[key] = number(key, 0.5, 1.0)
        elif key == "max_years_required":
            out[key] = number(key, 0, 40, optional=True)
        elif key == "min_salary":
            out[key] = number(key, 0, 2_000_000, optional=True)
        elif key == "workplaces":
            out[key] = names(key, WORKPLACES)
        elif key == "job_types":
            out[key] = names(key, tuple(JOB_TYPES))
        else:                                        # required_skills, exclude_companies
            out[key] = names(key)
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    temp = SETTINGS.with_name(f".{SETTINGS.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temp, SETTINGS)
    return out


# -- the shortlist's own tables, and the one question the Submit decision asks --------------------------

def ensure_tables() -> None:
    """The picks' and the person's Remove / Not today tables, created when first needed: never by a
    process merely starting (they are not in models.Base), so the person's database gains nothing while
    the shortlist is switched off. Creating a table that exists already does nothing."""
    from data_engineering.db import session as db
    from data_engineering.db.models import ShortlistBase
    if db._engine is None:
        raise RuntimeError("init_engine() must be called before the shortlist is used")
    ShortlistBase.metadata.create_all(db._engine)


def morning_pick(job_id: int) -> bool:
    """Whether the morning ever picked this job. The app never presses Submit by itself on such a job,
    whatever the person's Submit setting or the hold say (execution._one, 4 Nov 2026): the person
    presses it. No shortlist table means nothing was ever picked. Any doubt -- the database busy, an
    error -- answers yes, so a doubt can only ever leave Submit to the person."""
    from sqlalchemy import inspect

    from data_engineering.db.models import ShortlistPick
    from data_engineering.db.session import get_session
    try:
        with get_session() as session:
            if not inspect(session.get_bind()).has_table(ShortlistPick.__tablename__):
                return False
            return session.query(ShortlistPick.id).filter(ShortlistPick.job_id == int(job_id)).first() is not None
    except Exception:
        return True


# -- small helpers ------------------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plain(when: datetime) -> datetime:
    """SQLite keeps no time zone: stored as UTC."""
    return when.astimezone(timezone.utc).replace(tzinfo=None)


def _aware(when: datetime) -> datetime:
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when


def day_of(now: datetime) -> str:
    """The person's own date, which a day's picks count against."""
    return now.astimezone().date().isoformat()


def _norm(text: str | None) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", (text or "").lower()).split())


_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "plc",
             "gmbh", "lp", "llp"}


def _company(name: str | None) -> str:
    """A company however a site spells it: "Globex, Inc." and "Globex" are one."""
    words = _norm(name).split()
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)


def _same_job(company: str | None, title: str | None) -> tuple[str, str]:
    """The same job however a site spells it: the company without "Inc." and the like, and the title,
    letters and digits only."""
    return _company(company), _norm(title)


def _yearly(amount, period: str | None) -> float | None:
    if amount is None:
        return None
    period = (period or "year").lower()
    if period.startswith("hour"):
        return float(amount) * HOURS_A_YEAR
    if period.startswith("month"):
        return float(amount) * 12
    if period.startswith("year") or period.startswith("annual"):
        return float(amount)
    return None                                     # a week, a day: not compared


def employer_posting(job) -> str | None:
    """How the job's link is the employer's own posting, or None when it goes to a listing site."""
    from backend.core.joblinks import POSTING, kind
    from data_engineering.scraper.runner import SCRAPER_TYPES
    if job.source in SCRAPER_TYPES:
        return f"on {job.source}, the employer's own hiring system"
    own = getattr(job, "employer_url", None)
    if own and (getattr(job, "employer_url_kind", None) or kind(own)) == POSTING:
        return "the employer's own posting, saved on the job's page"
    return None


def _mentions(text: str, skill: str) -> bool:
    return re.search(rf"(?<![0-9a-z]){re.escape(skill.lower())}(?![0-9a-z])", text) is not None


def _label(verdict) -> str:
    from backend.core.eligibility import ELIGIBLE, WITH_WARNINGS
    if verdict.decision == ELIGIBLE:
        return "Eligible"
    if verdict.decision != WITH_WARNINGS:            # allowed by allow_needs_confirmation only
        return "Needs your confirmation: " + "; ".join(verdict.unknowns or verdict.reasons)
    warnings = [w for w in verdict.warnings if "has sponsored" not in w]   # a past record is no evidence
    if any(w.startswith("Sponsorship unknown") for w in warnings):
        rest = [w for w in warnings if not w.startswith("Sponsorship unknown")]
        return SPONSORSHIP_UNKNOWN + (f"; {'; '.join(rest)}" if rest else "")
    return "Eligible with warnings: " + "; ".join(warnings)


# -- the person's word: removed, not today ------------------------------------------------------------

def _record(job_id: int, kind: str, until: datetime | None = None, note: str = "") -> None:
    from backend.apply.execution import _when_free
    from data_engineering.db.models import ShortlistExclusion
    from data_engineering.db.session import get_session

    @_when_free
    def write():
        with get_session() as session:
            session.add(ShortlistExclusion(job_id=int(job_id), kind=kind, at=_plain(_now()), note=note[:400],
                                           until=_plain(until) if until else None))
    ensure_tables()
    write()


def remove(job_id: int, note: str = "") -> None:
    """Never pick this job again (the person's Remove)."""
    _record(job_id, REMOVED, note=note)


def defer(job_id: int, *, now: datetime | None = None, note: str = "") -> datetime:
    """Not today: left out until the person's next midnight, then considered again like any other job --
    not promised a place. Returns when it may be considered again (UTC)."""
    local = (now or _now()).astimezone()
    until = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    _record(job_id, DEFERRED, until=until, note=note)
    return until.astimezone(timezone.utc)


def restore(job_id: int, note: str = "") -> None:
    """Undo Remove or Not today."""
    _record(job_id, RESTORED, note=note)


def latest_words(session) -> dict:
    """The person's latest word on each job (Remove, Not today, Restore): job id -> its row."""
    from data_engineering.db.models import ShortlistExclusion
    latest: dict[int, ShortlistExclusion] = {}
    for row in session.query(ShortlistExclusion).order_by(ShortlistExclusion.id):
        latest[row.job_id] = row
    return latest


def exclusions(session, now: datetime) -> dict[int, str]:
    """The jobs the person's word leaves out now, with the reason: the latest word on each job stands."""
    out = {}
    for job_id, row in latest_words(session).items():
        if row.kind == REMOVED:
            out[job_id] = "you removed it from the morning's picks"
        elif row.kind == DEFERRED and row.until is not None and _aware(row.until) > now:
            out[job_id] = f"you put it off until {_aware(row.until).astimezone():%b %-d}"
    return out


def returning(session, now: datetime) -> set[int]:
    """The jobs the person brought back: put off with Not today and that day passed, or restored. Each
    is considered again like any other job -- not promised a place -- though it was picked and taken
    off the list before; one on the list now, applied to, or picked today still is not picked."""
    return {job_id for job_id, row in latest_words(session).items()
            if row.kind == RESTORED or (row.kind == DEFERRED and row.until is not None and _aware(row.until) <= now)}


# -- what is taken already ----------------------------------------------------------------------------

def _taken(session, now: datetime) -> dict:
    """Everything that rules a job out before its own checks: the list, applications, earlier picks."""
    from backend.apply import execution, runner
    from data_engineering.db.models import ApplyItem, Job, ShortlistPick
    applied_statuses = set(execution.APPLIED_STATUSES)

    listed: dict[int, str] = {}                     # job id -> its latest state on the chosen list
    for job_id, state in session.query(ApplyItem.job_id, ApplyItem.state).order_by(ApplyItem.id):
        listed[job_id] = state
    live_ids = {j for j, s in listed.items() if s not in execution.FINISHED}

    applied_ids, applied_keys = set(), set()
    for job_id, company, title in session.query(Job.id, Job.company, Job.title).filter(
            (Job.status.in_(applied_statuses)) | (Job.applied_at.isnot(None))):
        applied_ids.add(job_id)
        applied_keys.add(_same_job(company, title))
    try:
        lines = runner.AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            if isinstance(entry.get("job_id"), int):
                applied_ids.add(entry["job_id"])
            if entry.get("company") and entry.get("title"):
                applied_keys.add(_same_job(entry["company"], entry["title"]))

    listed_keys, listed_companies = set(), set()
    for job_id, company, title in session.query(Job.id, Job.company, Job.title).filter(
            Job.id.in_(live_ids or {-1})):
        listed_keys.add(_same_job(company, title))
        listed_companies.add(_company(company))

    today = day_of(now)
    picks: dict[int, str] = {}                      # job id -> the latest day it was picked
    picked_today = []
    for job_id, day in session.query(ShortlistPick.job_id, ShortlistPick.day).order_by(ShortlistPick.day):
        picks[job_id] = day
        if day == today:
            picked_today.append(job_id)
    today_keys, today_companies = set(), set()
    for company, title in session.query(Job.company, Job.title).filter(Job.id.in_(picked_today or [-1])):
        today_keys.add(_same_job(company, title))
        today_companies.add(_company(company))
    return {"listed": listed, "live": live_ids, "applied_ids": applied_ids, "applied_keys": applied_keys,
            "listed_keys": listed_keys, "listed_companies": listed_companies, "picks": picks,
            "picked_today": len(picked_today), "today_keys": today_keys, "today_companies": today_companies}


# -- one job --------------------------------------------------------------------------------------------

def _left_out(cfg, job, applicant, search, s: dict, taken: dict, excluded: dict, own: set[str]):
    """(why this job is not picked, or None; its eligibility verdict, or None). The first reason found,
    cheapest first, so the same job always gives the same reason."""
    from backend.apply import execution, listings, runner
    from backend.core.joblinks import POSTING
    from backend.core.eligibility import INELIGIBLE, NEEDS_INFORMATION, UNVERIFIED

    if job.id in excluded:
        return excluded[job.id], None
    state = taken["listed"].get(job.id)
    label = str(execution.LABELS.get(state) or state).lower() if state is not None else ""
    if job.id in taken["live"]:
        return f"on your chosen list already ({label})", None
    if job.id in taken["applied_ids"]:
        return "you applied to it already", None
    back = job.id in taken["returning"]               # brought back by Not today or Restore
    if state is not None and not back:
        return f"it was on your chosen list before ({label}): it is not picked again", None
    day = taken["picks"].get(job.id)
    if day == taken["today"]:
        return "picked today already", None
    if day and not back:
        return f"picked on {day} already", None
    company = _company(job.company)
    if company in own:
        return f"you excluded {job.company}", None
    looker = taken.get("looker")
    listing = employer_posting(job) is None
    if listing:
        known = looker.known(job.id) if looker else None
        if looker is None or job.source not in listings.SUPPORTED or job.employer_url:   # a careers site saved
            return (f"its link goes to a listing site ({job.source}), not the employer's own posting: "
                    "save the employer's posting on the job's page to have it considered"), None
        if known and known.outcome == listings.FOUND:   # found before, and taken off the job's page since
            return ("its link goes to a listing site; the employer's posting found from it was taken off "
                    "the job's page, so it is not picked: save one there to have it considered"), None
        if known:
            return _listing_left_out(job, known.detail), None
    key = _same_job(job.company, job.title)
    if key in taken["applied_keys"]:
        return f"you applied to the same job at {job.company} already (through another site)", None
    if key in taken["listed_keys"]:
        return f"the same job at {job.company} is on your chosen list already", None
    if key in taken["today_keys"]:
        return f"the same job at {job.company} was picked today from another site", None
    if company in taken["today_companies"]:
        return f"one job a company a day: one at {job.company} is picked today", None
    if company in taken["listed_companies"]:
        return f"another job at {job.company} is on your chosen list", None

    blocked = runner.blocked_reasons(cfg, job, applicant, search, any_board=True)
    if blocked:
        return blocked[0][1], None
    verdict = runner.decision(cfg, job, applicant, search)
    if verdict.decision == INELIGIBLE:
        return "not eligible: " + "; ".join(verdict.reasons), verdict
    if verdict.decision in (NEEDS_INFORMATION, UNVERIFIED) and not s["allow_needs_confirmation"]:
        return "needs your confirmation: " + "; ".join(verdict.unknowns or verdict.reasons), verdict

    text = "\n".join(p for p in (job.title, job.requirements, job.description) if p).lower()
    if s["required_skills"] and not any(_mentions(text, k) for k in s["required_skills"]):
        return f"it mentions none of your required skills ({', '.join(s['required_skills'])})", verdict
    if s["max_years_required"] is not None and job.experience_min_years is not None \
            and job.experience_min_years > s["max_years_required"]:
        return (f"it asks for {job.experience_min_years}+ years; your limit is "
                f"{s['max_years_required']:g}"), verdict
    if s["workplaces"] and job.workplace and job.workplace.lower() not in {w.lower() for w in s["workplaces"]}:
        return f"it is {job.workplace}; you chose {', '.join(s['workplaces'])}", verdict
    if s["job_types"] and job.job_type and job.job_type.lower() not in {t.lower() for t in s["job_types"]}:
        return f"it is {job.job_type}; you chose {', '.join(s['job_types'])}", verdict
    if s["min_salary"] is not None:
        top = _yearly(job.salary_max if job.salary_max is not None else job.salary_min, job.salary_period)
        if top is not None and top < s["min_salary"]:
            return f"its stated pay (up to {top:,.0f} a year) is under your lowest ({s['min_salary']:,.0f})", verdict
    if listing:                                      # last: the only rule that reads a page
        found = looker.look(job)
        if found is None:
            return (f"its link goes to a listing site ({job.source}): its employer's posting is looked for at "
                    "the next picking (this morning's looks are used up)"), verdict
        if found.outcome != listings.FOUND:
            return _listing_left_out(job, found.detail), verdict
        job.employer_url, job.employer_url_kind = found.url, POSTING
    return None, verdict


def _listing_left_out(job, detail: str) -> str:
    return (f"its link goes to a listing site ({job.source}) and no employer posting was confirmed: {detail}. "
            "Save the employer's posting on the job's page to have it considered")


def _why_picked(job, verdict, s: dict, found=None, checked: frozenset = frozenset()) -> list[str]:
    from backend.core.jobage import age_label
    from backend.core.joblinks import apply_link
    text = "\n".join(p for p in (job.title, job.requirements, job.description) if p).lower()
    how = (f"{found.detail}: {found.url} (its listing's own link is kept)" if found
           else employer_posting(job) or "")
    reasons = [f"match score {round((job.ats_match_score or 0) * 100)}% (at least {round(s['min_fit'] * 100)}%)",
               _label(verdict), how]
    if _site_of(apply_link(job)) in checked:
        reasons.append("its site asked for a browser check before: if it does again, the job goes to your own "
                       "browser to send, and is never marked sent")
    skills = [k for k in s["required_skills"] if _mentions(text, k)]
    if skills:
        reasons.append("mentions " + ", ".join(skills))
    age = age_label(job)
    if age:
        reasons.append(f"posted {age}" if age.endswith("just now") else f"posted {age} ago")
    return [r for r in reasons if r]


# -- adding a pick ---------------------------------------------------------------------------------------

def _add(job_id: int, reasons: list[str], day: str, now: datetime) -> int | None:
    """Put one picked job at the end of the chosen list and write the pick down, in one transaction.
    None when it was taken meanwhile (picked, or put on the list from somewhere else)."""
    from backend.apply import execution
    from data_engineering.db.models import ApplyItem, ShortlistPick
    from data_engineering.db.session import get_session

    @execution._when_free
    def write():
        try:
            with get_session() as session:
                if session.query(ShortlistPick.id).filter(ShortlistPick.job_id == job_id,
                                                          ShortlistPick.day == day).first():
                    return None
                if session.query(ApplyItem.id).filter(ApplyItem.job_id == job_id,
                                                      ApplyItem.state.notin_(execution.FINISHED)).first():
                    return None
                top = session.query(func.max(ApplyItem.position)).scalar() or 0
                item = ApplyItem(job_id=job_id, state=execution.QUEUED, position=top + 1,
                                 added_at=_plain(now), updated_at=_plain(now))
                session.add(item)
                session.flush()
                execution._event(session, "Added to your chosen jobs", item=item, state=execution.QUEUED)
                execution._event(session, "Picked this morning: " + "; ".join(reasons), item=item)
                session.add(ShortlistPick(job_id=job_id, item_id=item.id, day=day, picked_at=_plain(now),
                                          rule_version=RULE_VERSION, reasons=json.dumps(reasons)))
                return item.id
        except IntegrityError:
            return None
    return write()


def _write_report(report: dict) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temp = REPORT.with_name(f".{REPORT.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(report, indent=1), encoding="utf-8")
    os.replace(temp, REPORT)


# -- the morning's picks ---------------------------------------------------------------------------------

def _site_of(url: str | None) -> str:
    from urllib.parse import urlsplit
    try:
        return (urlsplit(url or "").hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def checked_sites(session) -> frozenset:
    """The sites whose browser check stopped a form before, from the chosen list's own records."""
    from backend.core.joblinks import apply_link
    from data_engineering.db.models import ApplyEvent, Job
    rows = (session.query(Job).join(ApplyEvent, ApplyEvent.job_id == Job.id)
            .filter(ApplyEvent.message.like("%browser check%")).distinct().all())
    return frozenset(filter(None, (_site_of(apply_link(j)) for j in rows)))


def pick(cfg, *, now: datetime | None = None, say=print, fetch_for=None) -> dict:
    """Pick today's jobs for the chosen list. Returns, and writes to data/apply/shortlist.json, what was
    picked and why, and every job looked at and left out, with the reason. Opens no form, prepares no
    document, sends nothing. Nothing at all while the shortlist is switched off."""
    from backend.apply import chosen, execution, listings, runner
    from backend.apply.queue import APPLIABLE_STATUSES
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session

    s = settings(cfg)
    now = now or _now()
    report = {"at": now.isoformat(timespec="seconds"), "day": day_of(now), "rule_version": RULE_VERSION,
              "per_day": s["per_day"], "min_fit": s["min_fit"], "picked": [], "left_out": [],
              "picked_today": 0, "considered": 0, "note": ""}
    if not s["enabled"]:
        report["note"] = "the morning's picks are switched off"
        return report
    applicant, missing = chosen._applicant()
    if missing:
        report["note"] = f"nothing picked: your details are not complete ({missing})"
        _write_report(report)
        say(f"shortlist: {report['note']}")
        return report
    ensure_tables()                                  # switched on and picking: only now, never at startup
    search = runner.searched_places(cfg)
    own = {_company(c) for c in s["exclude_companies"]}
    employment = getattr(applicant, "employment", None)
    current = employment.get("current_employer") if isinstance(employment, dict) else None
    if current:
        own.add(_company(str(current)))

    with get_session() as session:
        taken = _taken(session, now)
        taken.update(returning=returning(session, now), today=day_of(now),
                     looker=listings.Looker(fetch_for or (lambda: listings.default_fetch(cfg)),
                                            listings.latest(session), now))
        excluded = exclusions(session, now)
        checked = checked_sites(session)
        jobs = (session.query(Job)
                .filter(Job.is_open.is_(True), Job.score_basis == "full", Job.ats_match_score.isnot(None),
                        Job.ats_match_score >= s["min_fit"], Job.status.in_(APPLIABLE_STATUSES))
                .order_by(Job.ats_match_score.desc(), Job.id.asc())
                .limit(s["max_considered"]).all())
        for job in jobs:
            session.expunge(job)
    room = max(0, s["per_day"] - taken["picked_today"])
    report["considered"] = len(jobs)
    picked, decisions = [], {}
    for job in jobs:
        row = {"job_id": job.id, "company": job.company, "title": job.title, "board": job.source,
               "score": round((job.ats_match_score or 0) * 100)}
        if len(picked) >= room:
            report["left_out"].append({**row, "reason": f"today's {s['per_day']} are picked"})
            continue
        why, verdict = _left_out(cfg, job, applicant, search, s, taken, excluded, own)
        if why:
            report["left_out"].append({**row, "reason": why})
            continue
        reasons = _why_picked(job, verdict, s, taken["looker"].found.get(job.id), checked)
        item_id = _add(job.id, reasons, report["day"], now)
        if item_id is None:
            report["left_out"].append({**row, "reason": "taken meanwhile: picked or added from somewhere else"})
            continue
        picked.append({**row, "item_id": item_id, "reasons": reasons})
        decisions[job.id] = verdict
        taken["today_keys"].add(_same_job(job.company, job.title))
        taken["today_companies"].add(_company(job.company))
    if decisions:
        execution.keep_decisions(decisions)          # each one's eligibility, its first step on the list
    report["picked"] = picked
    report["picked_today"] = taken["picked_today"] + len(picked)
    if not jobs:
        report["note"] = f"no open job scores {round(s['min_fit'] * 100)}% or more"
    _write_report(report)
    say(f"shortlist: {len(picked)} picked for your chosen jobs ({report['picked_today']} of {s['per_day']} "
        f"today), {len(report['left_out'])} left out of {len(jobs)} looked at")
    for p in picked:
        say(f"  picked: {p['company']} — {p['title']} · {'; '.join(p['reasons'])}")
    return report


# -- the Auto-apply page's "This morning's picks" (4 Nov 2026, step 2) --------------------------------------

class PickError(ValueError):
    """A press on the picks the app will not act on, in words the person can act on."""


def _has_tables(session) -> bool:
    from sqlalchemy import inspect

    from data_engineering.db.models import ShortlistPick
    return inspect(session.get_bind()).has_table(ShortlistPick.__tablename__)


_WORKPLACE_WORDS = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}


def _job_facts(job) -> dict:
    from backend.core.joblinks import apply_link
    return {"job_id": job.id, "company": job.company, "title": job.title, "board": job.source,
            "score": None if job.ats_match_score is None else round(job.ats_match_score * 100),
            "location": job.location or "", "workplace": _WORKPLACE_WORDS.get(job.workplace or "", ""),
            "posting": apply_link(job) if employer_posting(job) else ""}


def report() -> dict:
    """The latest picking's report (data/apply/shortlist.json): every job looked at and why it was left
    out. {} before the first picking."""
    try:
        data = json.loads(REPORT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def stage(entry: dict | None, *, removed: bool = False) -> dict:
    """Where a pick stands, in one of the plan's words (4 Nov 2026, step 3), read from its entry on the
    chosen list as that list shows it -- its state, its note, its documents and the person's word on each.
    No state of its own: the list's state machine is the only one. {"key", "label", "hint"}."""
    if not entry:
        return {"key": "gone", "label": "Off your list", "hint": "No longer on Your chosen jobs."}
    state, note = entry["state"], entry.get("note") or ""
    docs = [d for d in entry.get("documents") or [] if d.get("state") == "ready"]
    said = entry.get("outcome") or note
    if state == "queued":
        decision = entry.get("decision") or {}
        if note.startswith("Waits for your answer:") or decision.get("decision") == "NEEDS_INFORMATION":
            return {"key": "confirm", "label": "Needs your confirmation",
                    "hint": note if note.startswith("Waits for your answer:")
                    else "; ".join(decision.get("unknowns") or decision.get("reasons") or [])}
        if note.startswith("Waits for your answers:"):
            return {"key": "answers", "label": "Needs answers", "hint": note}
        if entry.get("answers_waiting"):
            n = entry["answers_waiting"]
            return {"key": "answers", "label": "Needs answers",
                    "hint": f"{n} required question{'s' if n != 1 else ''} on its form wait for your answers on the "
                            "Questions page: its form does not open before."}
        if not docs:
            return {"key": "selected", "label": "Selected",
                    "hint": "Press Prepare to have its resume and cover letter made. Nothing is made before you do."}
        if any(d.get("approval") == "rejected" for d in docs):
            return {"key": "approval", "label": "Needs document approval",
                    "hint": "You rejected a document: it is never sent, and this job waits for a new one."}
        if any(d.get("approval") != "approved" for d in docs):
            return {"key": "approval", "label": "Needs document approval",
                    "hint": "Approve or reject each document on Your chosen jobs below."}
        return {"key": "review", "label": "Ready for review",
                "hint": "Its documents are approved. Apply to these opens its form; you press Submit."}
    if state in ("validating", "preparing"):
        return {"key": "preparing", "label": "Preparing documents", "hint": entry.get("current") or said}
    if state in ("ready", "running"):
        return {"key": "form", "label": "Its form is opening", "hint": entry.get("current") or said}
    if state == "waiting_user":
        return {"key": "manual", "label": "Ready for manual submission",
                "hint": entry.get("action") or "Its form is filled: check it and press Submit yourself."}
    if state == "pending_confirmation":
        action = entry.get("action") or ""
        if "Send it from your own Chrome" in action:   # handed to the person's own browser, never sent here
            return {"key": "manual", "label": "Ready for manual submission", "hint": action}
        return {"key": "manual", "label": "Did it go?", "hint": action or said}
    if state == "applied":
        return {"key": "applied", "label": "Applied", "hint": said}
    if state in ("failed", "skipped"):
        return {"key": "failed", "label": "Failed / blocked", "hint": said}
    return {"key": "off", "label": "Removed" if removed else "Taken off your list", "hint": said}


def view(cfg, *, now: datetime | None = None) -> dict:
    """The picks as the Auto-apply page shows them: today's, each with why it was picked and its entry on
    the chosen list as that list shows it (its state, eligibility, documents); the jobs the person
    removed or put off, to restore; and the latest report's left-out jobs and reasons. Reads only: it
    creates no table and changes nothing."""
    from backend.apply import execution
    from data_engineering.db.models import Job, ShortlistPick
    from data_engineering.db.session import get_session

    now = now or _now()
    s = settings(cfg)
    out = {"enabled": s["enabled"], "per_day": s["per_day"], "min_score": round(s["min_fit"] * 100),
           "day": day_of(now), "picks": [], "set_aside": [], "report": report()}
    with get_session() as session:
        if not _has_tables(session):
            return out
        rows = (session.query(ShortlistPick, Job).outerjoin(Job, Job.id == ShortlistPick.job_id)
                .filter(ShortlistPick.day == out["day"]).order_by(ShortlistPick.id).all())
        picks = [(p.job_id, p.item_id, json.loads(p.reasons or "[]"), job and _job_facts(job)) for p, job in rows]
        words = latest_words(session)
        aside = []
        for job_id, row in words.items():
            if row.kind == REMOVED or (row.kind == DEFERRED and row.until is not None and _aware(row.until) > now):
                job = session.get(Job, job_id)
                aside.append({**(_job_facts(job) if job else {"job_id": job_id, "company": "", "title": ""}),
                              "word": row.kind, "at": _aware(row.at).isoformat(timespec="seconds"),
                              "until": _aware(row.until).isoformat(timespec="seconds") if row.until else None})
        live = execution.run_view()["live"]
    entries = {v["id"]: v for v in execution.item_views([i for _, i, _, _ in picks], live)}
    asked = _answers_waiting([(job_id, entries.get(item_id)) for job_id, item_id, _, _ in picks])
    for job_id, item_id, reasons, facts in picks:
        entry = entries.get(item_id)
        if entry and asked.get(job_id):
            entry = {**entry, "answers_waiting": asked[job_id]}
        state = entry["state"] if entry else ""
        removed = getattr(words.get(job_id), "kind", None) == REMOVED
        out["picks"].append({**(facts or {"job_id": job_id, "company": "", "title": ""}), "item_id": item_id,
                             "reasons": reasons, "entry": entry, "stage": stage(entry, removed=removed),
                             "can": {"remove": not removed and state not in execution.ON_ITS_WAY
                                     and state not in (execution.PENDING, execution.APPLIED),
                                     "defer": state == execution.QUEUED,
                                     "prepare": state == execution.QUEUED and not (entry or {}).get("documents")}})
    out["set_aside"] = sorted(aside, key=lambda r: r["at"], reverse=True)
    return out


def _answers_waiting(picks) -> dict[int, int]:
    """job id -> how many required questions on its form wait for the person's answer, for each pick
    waiting on the list: read from the questions kept when its documents were made, as a run reads them
    (runner.questions_waiting), so the card says "Needs answers" before any run has looked."""
    from types import SimpleNamespace

    from backend.apply import chosen, prepare, runner
    waiting = [job_id for job_id, entry in picks if entry and entry.get("state") == "queued"]
    if not waiting:
        return {}
    kept = prepare.load()
    if not any(str(j) in kept for j in waiting):
        return {}
    try:
        applicant, _ = chosen._applicant()
    except Exception:                                # the person's details unreadable: none answered
        applicant = None
    out = {}
    for job_id in waiting:
        n = len(runner.questions_waiting(SimpleNamespace(id=job_id), applicant, kept))
        if n:
            out[job_id] = n
    return out


def take_off(job_id: int, word: str, *, now: datetime | None = None) -> dict:
    """Remove (for good) or Not today (until the person's next midnight) one of the morning's picks: its
    entry on the chosen list is cancelled if it is still waiting -- nothing was sent -- and the person's
    word is kept. Only a morning pick: a job the person chose themselves is refused, and so is one on its
    way, waiting for "Did it go?", or applied to (its record stays as it is)."""
    from backend.apply import execution
    from data_engineering.db.models import ApplyItem, ShortlistPick
    from data_engineering.db.session import get_session

    if word not in (REMOVED, DEFERRED):
        raise PickError("Remove or Not today?")
    with get_session() as session:
        if not _has_tables(session):
            raise PickError("That job is not one of the morning's picks.")
        pick = (session.query(ShortlistPick).filter(ShortlistPick.job_id == job_id)
                .order_by(ShortlistPick.id.desc()).first())
        if pick is None:
            raise PickError("That job is not one of the morning's picks: a job you chose yourself is taken off "
                            "with Cancel on Your chosen jobs.")
        item = session.get(ApplyItem, pick.item_id) if pick.item_id else None
        state = item.state if item is not None else None
    if state == execution.APPLIED:
        raise PickError("You applied to it already: it stays on your list and in your history.")
    if state in execution.ON_ITS_WAY or state == execution.PENDING:
        raise PickError(f"It is {execution.LABELS[state].lower()} now: wait for it to finish, or pause the run, "
                        "then press this again.")
    if word == DEFERRED and state != execution.QUEUED:
        # Not today is for a pick still waiting. One cancelled, failed, removed or set aside stays out:
        # only Restore, the person's own word, ever brings such a job back (4 Nov 2026).
        raise PickError("It is off your chosen jobs already, so it is not picked again: Retry on Your chosen "
                        "jobs puts it back there, and Restore brings back a job you removed.")
    if state == execution.QUEUED and pick.item_id:
        execution.cancel(pick.item_id)               # nothing was sent; the list keeps its steps
        execution.note(pick.item_id, "Removed from the morning's picks by you" if word == REMOVED
                       else "Not today: put off until tomorrow, when it is considered again")
    if word == REMOVED:
        remove(job_id)
        return {"word": REMOVED, "cancelled": state == execution.QUEUED}
    until = defer(job_id, now=now)
    return {"word": DEFERRED, "until": until.isoformat(timespec="seconds"), "cancelled": state == execution.QUEUED}


def bring_back(job_id: int) -> dict:
    """Restore a job the person removed or put off: considered again from the next picking, like any
    other job -- not put back on the list, and not promised a place."""
    from data_engineering.db.session import get_session
    with get_session() as session:
        word = latest_words(session).get(job_id) if _has_tables(session) else None
        kind = word.kind if word is not None else None
    if kind not in (REMOVED, DEFERRED):
        raise PickError("That job was not removed or put off.")
    restore(job_id)
    return {"word": RESTORED}


def to_prepare(job_id: int | None = None, *, now: datetime | None = None) -> list[int]:
    """The entries a Prepare press on the picks is for (item ids): that pick's, or each of today's picks
    still waiting with no documents made. [] when there is none to make. Reads only."""
    from backend.apply import execution
    from data_engineering.db.models import ApplyArtifact, ApplyItem, ShortlistPick
    from data_engineering.db.session import get_session
    with get_session() as session:
        if not _has_tables(session):
            return []
        q = session.query(ShortlistPick.item_id)
        q = q.filter(ShortlistPick.job_id == job_id) if job_id else q.filter(ShortlistPick.day == day_of(now or _now()))
        ids = [i for (i,) in q.order_by(ShortlistPick.id) if i]
        if job_id and ids:
            ids = ids[-1:]                               # its latest pick
        waiting = {i for (i,) in session.query(ApplyItem.id).filter(ApplyItem.id.in_(ids or [-1]),
                                                                     ApplyItem.state == execution.QUEUED)}
        made = {i for (i,) in session.query(ApplyArtifact.item_id).filter(ApplyArtifact.item_id.in_(ids or [-1]),
                                                                           ApplyArtifact.state == "ready")}
    return [i for i in ids if i in waiting and i not in made]
