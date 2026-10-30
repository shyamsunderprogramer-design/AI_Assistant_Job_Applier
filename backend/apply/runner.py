"""Work through the apply queue: fill each form, submit or hand it over, record it.

One visible browser for the whole run, one tab per application. For each job:

  1. Pick the resume. The tailored one if the guard passed it; otherwise tailor
     now; if that is refused or no model is running, the base resume — which is
     the person's own words, so it is always safe to send.
  2. Open the Greenhouse form itself (`greenhouse.form_url`), not the posting's
     link, and fill everything the profile can answer.
  3. Legacy runs always wait for the person to press Submit. Only the durable
     chosen-jobs workflow may request automatic submission, after its Phase 3
     checks; CAPTCHA, declarations and blank required questions still stop it.
  4. Only a confirmation page counts as applied. Then the job is marked
     Applied, and a line goes into data/applications.jsonl with what was sent.

Printed lines are the interface: the web page shows the tail of this run's
output, so every line is written for the person watching.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.apply import greenhouse
from backend.apply.profile import Applicant, ProfileIncomplete, check_ready, load
from backend.apply.queue import (SAVED, _company_key, age_limit_days, age_limit_months, age_note,
                                 build_queue, summarise, too_old)
from backend.config.loader import PROJECT_ROOT, web_url
from data_engineering.db.models import Job
from data_engineering.db.session import get_session
from data_engineering.scraper.filters import resolve_filter

log = logging.getLogger(__name__)

AUDIT_LOG = PROJECT_ROOT / "data" / "applications.jsonl"
DEFAULT_WAIT_MINUTES = 15


class NotReady(RuntimeError):
    """Nothing can be applied to until the person fixes this."""


@dataclass
class Outcome:
    job_id: int
    company: str
    title: str
    result: str          # submitted | skipped | timeout | not-filled | stopped | handoff
    how: str = ""        # auto | you
    detail: str = ""
    handoff: str = ""    # the form to open in the person's own browser, once the app's is closed


# Boards whose CAPTCHA turns the app's own browser away at Submit even when the person presses it
# (Lever's hCaptcha: "There was an error verifying your application"). The app fills the form to
# learn what it asks, then hands it over: the person sends it from their own browser.
HANDOFF = (re.compile(r"^https://jobs\.(?:eu\.)?lever\.co/", re.I),)


def hands_off(url: str) -> bool:
    return any(p.match(url or "") for p in HANDOFF)


def open_own_browser(url: str) -> bool:
    """The person's own Chrome -- their profile, their sign-ins -- not the app's; else their browser."""
    for args in (["open", "-a", "Google Chrome", url], ["open", url]):
        try:
            if subprocess.run(args, capture_output=True, timeout=20).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def say(line: str) -> None:
    print(line, flush=True)


# -- limits ---------------------------------------------------------------

def start_of_today_utc(now: datetime | None = None) -> datetime:
    """Local midnight, in UTC. "Ten a day" means the person's day."""
    local = (now or datetime.now(timezone.utc)).astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def remaining_today(cfg, applicant: Applicant, applied_today: int) -> int:
    """Both caps apply: the project's and the person's. The lower one wins."""
    project_cap = int(cfg.get("limits.max_applications_per_day", 15) or 0)
    return max(0, min(project_cap, applicant.max_per_day) - applied_today)


def _applied_since(session, since: datetime) -> int:
    return (session.query(Job)
            .filter(Job.applied_at.isnot(None), Job.applied_at >= since.replace(tzinfo=None))
            .count())


def _last_applied_by_company(session) -> dict[str, datetime]:
    latest: dict[str, datetime] = {}
    for company, when in (session.query(Job.company, Job.applied_at)
                          .filter(Job.applied_at.isnot(None)).all()):
        if company not in latest or when > latest[company]:
            latest[company] = when
    return latest


# -- documents ------------------------------------------------------------

def tailored_resume(cfg, job) -> Path | None:
    """The guard-approved tailored resume for this job, PDF first, if any.

    One longer than the page limit was made before the limit existed and is
    skipped: attaching a nine-page resume is worse than tailoring again.
    """
    from ml.resume.packet import packet_root
    from ml.resume.pipeline import over_page_limit
    from ml.resume.writer import output_filename

    docx = packet_root(cfg) / output_filename(job.company, job.title, job.external_id or "")
    if over_page_limit(cfg, docx):
        return None
    for candidate in (docx.with_suffix(".pdf"), docx):
        if candidate.exists():
            return candidate
    return None


def base_resume() -> Path | None:
    from ml.resume.parser import find_base_resume
    return find_base_resume(PROJECT_ROOT / "ml" / "resume")


def selected_master(cfg, job) -> Path | None:
    """The master resume cut down to this posting, with no model involved.

    Nothing is reworded — it is the person's own lines, the ones this job
    description most needs, to the page limit. The fallback whenever
    tailoring is refused or unavailable, instead of the whole master resume.
    """
    from ml.resume.fit import fit_to_pages
    from ml.resume.packet import packet_root
    from ml.resume.pdf import write_pdf
    from ml.resume.pipeline import load_base_resume
    from ml.resume.tailor import TailorResult
    from ml.resume.writer import output_filename

    try:
        base = load_base_resume(cfg)
    except Exception:
        return base_resume()
    result = TailorResult(summary=None)
    limit = int(cfg.get("resume.max_pages", 4) or 0)
    if limit > 0:
        fit_to_pages(base, result, job.description or job.title,
                     requirements=job.requirements, company=job.company, max_pages=limit)
    stem = output_filename(job.company, job.title, job.external_id or "")[:-5]
    target = packet_root(cfg) / f"{stem}_selected.pdf"
    write_pdf(base, result, target)
    return target


def choose_resume(cfg, job, *, tailor: bool = True) -> tuple[Path | None, str]:
    """(file to attach, where it came from).

    A tailored resume set to be made again (one made from a master since repaired) is never
    sent as it is: it is tailored again first, or the person's own lines go instead."""
    from ml.tailoring.pipeline import needs_redo
    redo = needs_redo(job.id)
    found = None if redo else tailored_resume(cfg, job)
    if found:
        return found, "tailored"
    note = "tailoring off"
    if tailor:
        try:
            if redo:
                say(f"Tailoring the resume again first: the one made for this job was {redo} "
                    "(about 8 minutes)…")
                from backend.apply.prepare import _tailor
                _tailor(cfg, job, again=True)
                status = "did not replace the old one"
            else:
                from ml.resume.pipeline import tailor_jobs
                outcomes = tailor_jobs(cfg, [job.id])
                status = outcomes[0].status if outcomes else "not tailored"
            found = None if needs_redo(job.id) else tailored_resume(cfg, job)
            if found:
                return found, "tailored just now"
            note = f"tailoring {status}"
        except Exception as exc:              # no model running, no key, ...
            note = f"tailoring unavailable ({type(exc).__name__})"
    return selected_master(cfg, job), f"your resume selected for this job — {note}"


def file_name(applicant, company: str, kind: str, suffix: str) -> str:
    """The name an employer sees: "Jordan_Example_Resume_Acme.pdf" -- the person, what it is, for whom.

    The company keeps its own capitals and drops "Inc."/"LLC"; anything that is not a letter or a
    digit becomes one underscore, so the name survives every upload form.
    """
    from ml.resume.packet import file_name as named   # one rule for the job's folder and the upload
    return named(getattr(applicant, "full_name", ""), company, kind, suffix)


def sent_copy(path: Path, evidence: Path, applicant, company: str, kind: str) -> Path:
    """A copy of `path` named for the employer, in the run's send/ folder."""
    folder = evidence / "send"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / file_name(applicant, company, kind, Path(path).suffix)
    shutil.copy2(path, target)
    return target


def write_letter_if_missing(cfg, job, head: str = "") -> bool:
    """Auto-apply does every step of the job page's checklist: a cover letter is written for a job
    that has none -- the same writer and honesty check as the page's "let Claude write it" -- so
    a form that asks for one gets one. Returns whether the job has a letter now.

    A letter the check rejects, or a model that does not answer, leaves the application to go on
    without one: the job keeps its status (the page's writer would mark it Manual Review).
    """
    from ml.resume.packet import LETTER_FILE, packet_dir
    folder = packet_dir(cfg, job.company, job.title, job.external_id or "", job_id=job.id)
    if (folder / LETTER_FILE).exists():
        return True
    if cfg is not None and not cfg.get("apply.write_letter", True):
        return False
    from ml.resume.pipeline import write_letter
    say(f"{head}: writing the cover letter (a minute)…" if head else "Writing the cover letter…")
    with get_session() as session:
        row = session.get(Job, job.id)
        status_before = row.status if row is not None else job.status
    try:
        outcome = write_letter(cfg, job.id)
        written = outcome.status not in ("rejected", "error") and (folder / LETTER_FILE).exists()
    except Exception as exc:                      # no letter is better than no application
        log.warning("Cover letter for job %s not written: %s", job.id, exc)
        written = False
    if not written:
        with get_session() as session:
            row = session.get(Job, job.id)
            if row is not None and row.status != status_before:
                row.status = status_before           # a failed letter is not a verdict on the job
        say("   No cover letter this time: the draft did not pass the honesty check, or no model answered. "
            "The application goes on without one; the job page can write one.")
    else:
        say("   Cover letter written and checked.")
    return written


def cover_letter(cfg, job, evidence: Path) -> Path | None:
    """The letter as it goes out: a PDF in the tailored resume's template, else the text.

    The PDF and .docx are written whenever the letter is (ml/resume/letter_edit.py), with the
    name and contact line on top. One older than the text -- the letter edited outside the
    page -- is drawn again first, so a stale one is never sent.
    """
    import shutil
    from ml.resume import letter_edit
    from ml.resume.packet import LETTER_FILE, packet_dir

    folder = packet_dir(cfg, job.company, job.title, job.external_id or "", job_id=job.id)
    letter = folder / LETTER_FILE
    if not letter.exists():
        return None
    pdf = letter_edit.document(folder, "pdf")
    if pdf is None or pdf.stat().st_mtime < letter.stat().st_mtime:
        letter_edit._documents(folder, letter.read_text(encoding="utf-8"), *letter_edit.look_for_job(cfg, job.id))
        pdf = letter_edit.document(folder, "pdf")
    if pdf is not None:
        target = evidence / "cover-letter.pdf"
        shutil.copy2(pdf, target)
        return target
    target = evidence / "cover-letter.txt"
    target.write_text(letter.read_text(encoding="utf-8"), encoding="utf-8")
    return target


# -- recording ------------------------------------------------------------

APPLIED_STATUSES = ("Applied", "Interviewing", "Offer", "Rejected")


_LINK: dict | None = None        # the chosen job's place on its list, while its form is filled (`linked`)


@contextmanager
def linked(link: dict):
    """While this form is filled, the application's line names where it came from on the chosen
    jobs' list -- the entry, the run, the attempt, and exactly which documents went, by checksum
    (backend/apply/execution._link, 26 Oct 2026) -- and so does a "Did it go?" kept for later."""
    global _LINK
    before, _LINK = _LINK, link
    try:
        yield
    finally:
        _LINK = before


def _queue_link() -> dict:
    return {"queue": _LINK} if _LINK else {}


def record_application(job_id: int, entry: dict, *, audit_log: Path | None = None,
                       at: datetime | None = None) -> bool:
    """Atomically mark Applied and enqueue its history, then export it. Returns whether new.

    Once only (25 Oct 2026): its own page, the chosen jobs' queue and an employer's email can each
    say a job went. Recorded already, the first record stands -- the day it went, a later stage it
    has moved on to (Interviewing, Offer, Rejected), and its one line in the log; until then a
    second call wrote a second line, put the day forward and could turn Interviewing back into
    Applied. A stage the person set by hand with no record yet keeps its name and gains the day.

    `at` is when it went, when that is known better than "now": an employer's
    "thank you for applying" email carries the moment of the submission."""
    now = at or datetime.now(timezone.utc)
    from backend.apply.history_outbox import enqueue
    payload = {**entry, **_queue_link()}
    with get_session() as session:
        recorded = mark_applied(session, job_id, now) is not False
        if recorded:
            enqueue(session, job_id, payload, now, audit_log or AUDIT_LOG)
    write_audit(job_id, payload, now, audit_log)
    return recorded


def record_marked(session, job, stage: str, where: str) -> bool:
    """A job the person marked as applied by hand -- its status on the job page, the assistant, a stage on
    the Applied page -- with no application record yet: its history line says exactly that, "marked",
    never a submission the app saw, in the same transaction as the status (29 Oct 2026; until then these
    left the job Applied with no history at all). Nothing is invented: no documents, no run, and the job's
    applied_at is left as it was (it means when an application went out, which nobody here knows).
    Returns whether a line was added: none when the job has one already. Export it after the commit
    (history_outbox.export_pending)."""
    from backend.apply import tracker
    from backend.apply.history_outbox import enqueue
    from data_engineering.db.models import ApplyHistoryOutbox
    if job.id in tracker._audit() or session.query(ApplyHistoryOutbox.event_id).filter(
            ApplyHistoryOutbox.job_id == job.id).first() is not None:
        return False
    enqueue(session, job.id, {"company": job.company, "title": job.title, "how": "marked", "stage": stage,
                              "marked_on": where}, datetime.now(timezone.utc), AUDIT_LOG)
    return True


def mark_applied(session, job_id: int, now: datetime) -> bool | None:
    """The job marked Applied in this session's transaction, once: True when this marked it, False
    when it was recorded already (the first record stands), None when there is no such job. One
    conditional update, not a read and then a write: two processes recording the same job at the
    same moment (the run, and the person's "Yes, it went") are taken one at a time by SQLite, and
    the second finds it recorded. In the caller's transaction, so a claim can carry it (execution.answer)."""
    from sqlalchemy import case, func, or_
    taken = (session.query(Job)
             .filter(Job.id == job_id, or_(Job.status.is_(None), Job.status.notin_(APPLIED_STATUSES),
                                           Job.applied_at.is_(None)))
             .update({"status": case((Job.status.in_(APPLIED_STATUSES), Job.status), else_="Applied"),
                      "applied_at": func.coalesce(Job.applied_at, now)}, synchronize_session=False))
    if taken:
        return True
    return False if session.get(Job, job_id) is not None else None


def write_audit(job_id: int, entry: dict, now: datetime, audit_log: Path | None = None) -> None:
    """Export the event already committed with Applied; never reconstruct its payload."""
    from backend.apply.history_outbox import export_pending
    export_pending(job_id)


# -- the run --------------------------------------------------------------

def _eligible(job, applicant: Applicant, quiet: bool = False) -> bool:
    """Checked again at apply time: a job prepared before the profile said otherwise
    (a Top Secret role, prepared before "no clearance" was answered) must not go out."""
    from backend.core.eligibility import eligible
    ok, why = eligible(job, applicant.authorisation or {})
    if not ok and not quiet:
        say(f"Not applying to {job.company} — {job.title}: {', '.join(why)} (from your profile).")
    return ok


def not_wanted_now(cfg, job, search=None) -> str | None:
    """Why a job chosen earlier is not to go now, or None. A job prepared at three and a half
    months passes four while it waits on the Apply page, and one stored before the location
    rule caught it is abroad (two of the ready ones were, 23 Oct 2026)."""
    abroad = (search or searched_places(cfg)).location_reason(job.location)
    if abroad:
        return abroad
    if too_old(job, age_limit_days(cfg)):
        return age_note(job, age_limit_months(cfg))
    return None


def _still_wanted(cfg, job) -> bool:
    """Checked again at apply time, like `_eligible`."""
    why = not_wanted_now(cfg, job)
    if why:
        say(f"Not applying to {job.company} — {job.title}: {why}.")
    return why is None


def _blocks(cfg, job, applicant: Applicant, search=None, *, any_board: bool = False):
    """Each reason a run cannot apply to this job, as (kind, words), one at a time and the cheap
    ones first: "posting" (closed, or no link), "marked" (applied to, or marked by the person),
    "board" (a form no run fills), "profile" (a stated requirement the profile rules out), and
    "rule" (the person's own: where a job may be, how old). With `any_board` -- the jobs the person
    chose (26 Oct 2026) -- any board goes, assisted where the app has no filler of its own (`assisted`)."""
    from backend.apply.queue import APPLIABLE_STATUSES, FILLABLE
    from backend.core.eligibility import eligible
    from backend.core.joblinks import apply_link
    if not job.is_open:
        yield "posting", "the posting has closed"
    if job.status not in APPLIABLE_STATUSES:
        yield "marked", f"it is marked {job.status}"
    if job.source not in FILLABLE and not any_board:
        yield "board", (f"a run fills Greenhouse, Workday and JazzHR forms, and this one is on {job.source}: "
                        "press Auto-apply on its page")
    if not ((apply_link(job) if any_board else job.application_url) or "").strip():
        yield "posting", "the posting has no link to apply through"
    ok, why = eligible(job, applicant.authorisation or {})
    if not ok:
        yield "profile", f"{', '.join(why)} (from your profile)"
    rule = not_wanted_now(cfg, job, search)
    if rule:
        yield "rule", rule


def blocked_reason(cfg, job, applicant: Applicant, search=None, *, any_board: bool = False) -> str | None:
    """Why a run cannot apply to this job at all, in words the person can act on; None when it
    can. When it goes is another matter: one a company, twelve hours apart, and today's limit."""
    return next((words for _, words in _blocks(cfg, job, applicant, search, any_board=any_board)), None)


def blocked_reasons(cfg, job, applicant: Applicant, search=None, *, any_board: bool = False) -> list[tuple[str, str]]:
    """Every reason, not only the first: (kind, words), as `_blocks` gives them."""
    return list(_blocks(cfg, job, applicant, search, any_board=any_board))


def assisted(job) -> str:
    """How a chosen job on a board with no filler of its own goes (26 Oct 2026): the app opens the
    employer's form and fills what it can, and the person finishes it and presses Submit -- never the
    app, even when the posting leads to a Greenhouse form; a Lever form is sent from the person's own
    browser. "" for Greenhouse, Workday and JazzHR, which a run fills as it always has."""
    from backend.apply.queue import FILLABLE
    from backend.core.joblinks import apply_link
    if job is None or job.source in FILLABLE:
        return ""
    if job.source == "lever" or hands_off(apply_link(job)):
        return ("Assisted: the app fills its form, then you send it from your own browser "
                "(its CAPTCHA turns the app's window away)")
    return f"Assisted: the app opens the employer's form from {job.source} and fills what it can; you finish it and press Submit"


_RULED_OUT_BY = {"posting": "the posting's own state", "marked": "your own mark on the job",
                 "rule": "your own rule, not the employer's"}


def decision(cfg, job, applicant: Applicant, search=None):
    """The eligibility decision for this job (backend/core/eligibility.decide, 26 Oct 2026): the
    posting's stated requirements against the profile, with its own words, and what else of a
    run's rules rules it out. Whether a run fills its form is the app's limit, not the person's
    eligibility, and is no part of it."""
    from backend.core.eligibility import decide
    ruled_out = [f"{words} ({_RULED_OUT_BY[kind]})" for kind, words in _blocks(cfg, job, applicant, search, any_board=True)
                 if kind in _RULED_OUT_BY]
    return decide(job, applicant.authorisation or {}, ruled_out=ruled_out)


def questions_waiting(job, applicant, entries: dict | None = None) -> list[dict]:
    """The required questions on this job's form that nothing answers yet -- read from the board when
    its documents were made ready (backend/apply/prepare.keep_questions) -- for the person to answer on
    the Questions page before its form opens (26 Oct 2026). [] when its form was never read ahead: then
    any question the form asks waits for them on the form, as before. Never answered by a guess:
    only the person's own answers, profile and saved answers count (prepare._still_open)."""
    from backend.apply import prepare
    entry = (entries if entries is not None else prepare.load()).get(str(job.id))
    if not entry or not entry.get("questions"):
        return []
    person = applicant if hasattr(applicant, "answer_for") else None     # details not filled in yet
    return [q for q in prepare._still_open(entry, person) if q.get("required")]


def needs_answer(job, applicant: Applicant) -> bool:
    """The posting states a requirement the person's profile leaves unanswered (a clearance, US
    citizenship, a visa): it waits, set aside, until they answer. Never taken for a yes or a no."""
    from backend.core.eligibility import UNKNOWN, assess
    return any(found == UNKNOWN for _, found, _ in assess(job, applicant.authorisation or {}))


def jobs_by_id(session, job_ids) -> dict:
    """These jobs, by id, read a few hundred at a time: SQLite caps the parameters of one query."""
    ids, found = list(dict.fromkeys(job_ids)), {}
    for n in range(0, len(ids), 500):
        found.update((j.id, j) for j in session.query(Job).filter(Job.id.in_(ids[n:n + 500])))
    return found


def chosen_queue(cfg, applicant: Applicant, session, chosen_ids, limit: int, search=None):
    """The jobs the person chose on the Jobs page, for "Apply to these" on the Auto-apply page.

    Never swapped for the app's own picks (24 Oct 2026), and every rule of a run holds
    (`blocked_reason`) but the board's: any board goes, assisted (`assisted`); one whose posting asks what the profile leaves unanswered waits for the
    answer (`needs_answer`, 26 Oct 2026), and one whose form asks questions nothing answers yet waits
    for the person's answers (`questions_waiting`). Of two chosen jobs at one company the better fit
    goes first: one a company each run, twelve hours apart, as for every run."""
    from backend.apply import prepare
    search = search or searched_places(cfg)
    found = jobs_by_id(session, chosen_ids)
    entries = prepare.load()
    jobs = [found[i] for i in dict.fromkeys(chosen_ids)
            if i in found and blocked_reason(cfg, found[i], applicant, search, any_board=True) is None
            and not needs_answer(found[i], applicant) and not questions_waiting(found[i], applicant, entries)]
    return build_queue(jobs, limit=limit, last_applied=_last_applied_by_company(session),
                       max_age_days=age_limit_days(cfg), chosen_ids=chosen_ids,
                       sources={j.source for j in jobs})          # any board: assisted where need be


def candidates(cfg, applicant: Applicant, limit: int, job_id: int | None = None,
               job_ids: list[int] | None = None, chosen_ids: list[int] | None = None):
    """What this run will apply to, and how many today's cap still allows."""
    with get_session() as session:
        left = remaining_today(cfg, applicant, _applied_since(session, start_of_today_utc()))
        if chosen_ids:
            queue = chosen_queue(cfg, applicant, session, chosen_ids, min(limit, left))
        elif job_ids:
            # The prepared ones, in the order they were chosen this morning.
            jobs = [j for j in (session.get(Job, i) for i in job_ids)
                    if j is not None and j.is_open and _eligible(j, applicant)
                    and _still_wanted(cfg, j)]
            queue = build_queue(jobs, limit=min(len(jobs), left), max_per_company=len(jobs))
            order = {i: n for n, i in enumerate(job_ids)}
            queue.sort(key=lambda c: order.get(c.job_id, 0))
        elif job_id is not None:
            job = session.get(Job, job_id)
            jobs = [job] if job is not None and _eligible(job, applicant) else []
            # Asked for by name, from its own page: any board will do, and any age -- the age
            # limit is for what the app picks. A form the app has no filler of its own for is
            # followed and filled by backend/apply/anyform.py.
            queue = build_queue(jobs, limit=1, sources=(jobs[0].source,) if jobs else ())
        else:
            queue = _picks(cfg, applicant, session, min(limit, left))
    return queue, left


def searched_places(cfg):
    """The search's rule for where a job may be (its `location_reason`). With no search to be
    had from the resume, config.yaml's: applying goes on while the search is put right."""
    from data_engineering.scraper.filters import JobFilter, UndeterminedSearch
    try:
        return resolve_filter(cfg)
    except UndeterminedSearch:
        return JobFilter.from_config(cfg)


def _picks(cfg, applicant: Applicant, session, limit: int):
    """Apply to next 10's choice: the jobs saved to the Watch list, then roles at watched
    companies, then the best fit of the rest, under every rule of the queue."""
    from backend.core import watchlist
    min_score = cfg.get("apply.min_score", cfg.get("resume.min_score", 0.45))
    floor = float(min_score) if min_score is not None else None
    saved = watchlist.starred_ids()
    search = searched_places(cfg)
    # The fit first, being cheap: the place is read only for the jobs that could go.
    jobs = [j for j in session.query(Job).filter(Job.is_open.is_(True)).all()
            if (j.id in saved or floor is None or (j.ats_match_score or 0) >= floor)
            and _eligible(j, applicant, quiet=True) and not search.location_reason(j.location)]
    return build_queue(
        jobs, limit=limit, min_score=floor,
        last_applied=_last_applied_by_company(session),
        max_age_days=age_limit_days(cfg),
        saved=saved, watched=watchlist.watched_job_ids(jobs),
    )


def next_up(cfg, limit: int = 10) -> dict:
    """The Auto-apply page's list: what Apply to next 10 would apply to now, and why a saved job
    is not among them. Opens nothing and sends nothing. With today's limit reached it still
    shows the next ones: they go on the next run."""
    from backend.core import watchlist
    from backend.core.jobage import age_label
    from ml.tailoring.pipeline import needs_redo
    applicant = load(PROJECT_ROOT / "backend" / "config" / "applicant.yaml")
    with get_session() as session:
        left = remaining_today(cfg, applicant, _applied_since(session, start_of_today_utc()))
        queue = _picks(cfg, applicant, session, limit)
        picks = []
        for c in queue:
            job = session.get(Job, c.job_id)
            if job is None:
                continue
            picks.append({"id": job.id, "title": job.title, "company": job.company,
                          "location": job.location or "", "score": c.score_pct, "age": age_label(job),
                          "board": job.source, "status": job.status, "why": c.group_name,
                          "saved": c.group == SAVED,
                          "resume": tailored_resume(cfg, job) is not None and not needs_redo(job.id)})
        chosen = {_company_key(c.company) for c in queue}
        last_applied = _last_applied_by_company(session)
        left_out = []
        for job_id in sorted(watchlist.starred_ids() - {c.job_id for c in queue}):
            job = session.get(Job, job_id)
            if job is not None:
                left_out.append({"id": job.id, "title": job.title, "company": job.company,
                                 "why": _not_picked(cfg, job, applicant, last_applied, chosen)})
    return {"picks": picks, "left_out": left_out, "left_today": left,
            "auto_submit": False, "max_age_months": age_limit_months(cfg)}


def _not_picked(cfg, job, applicant: Applicant, last_applied: dict, chosen: set[str]) -> str:
    """Why a job the person saved is not in the next run, in words they can act on."""
    from backend.apply.queue import DEFAULT_COOLDOWN_HOURS
    from data_engineering.scraper.lifecycle import as_utc
    blocked = blocked_reason(cfg, job, applicant)
    if blocked:
        return blocked
    key = _company_key(job.company)
    last = max((as_utc(w) for name, w in last_applied.items() if _company_key(name) == key), default=None)
    if last is not None and datetime.now(timezone.utc) - last < timedelta(hours=DEFAULT_COOLDOWN_HOURS):
        return f"you applied to {job.company} in the last {DEFAULT_COOLDOWN_HOURS} hours: one a company, half a day apart"
    if key in chosen:
        return f"another saved job at {job.company} goes first: one a company each run"
    return "after these: it goes on a later run"


def why_not(job_id: int) -> str:
    """Why one job asked for by name is not going out."""
    from backend.apply.queue import APPLIABLE_STATUSES
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return "there is no job with that number."
        if not job.is_open:
            return "the posting has closed."
        if job.status not in APPLIABLE_STATUSES:
            return f"it is marked {job.status} already."
        from backend.core.joblinks import apply_link
        if not apply_link(job).strip():
            return "the posting has no link to apply through."
    return "your profile rules it out (the line above says why)."


def _ready_applicant(cfg) -> Applicant:
    """The person's details, or NotReady saying what is missing and where to fill it in."""
    try:
        applicant = load(PROJECT_ROOT / "backend" / "config" / "applicant.yaml")
    except ProfileIncomplete as exc:
        raise NotReady(f"{exc}\nFill in your details at {web_url('/applicant', cfg)}")
    problems = check_ready(applicant)
    if problems:
        raise NotReady("Your applicant details are incomplete:\n"
                       + "\n".join(f"  - {p}" for p in problems)
                       + f"\nFix them at {web_url('/applicant', cfg)}")
    return applicant


def run(cfg, *, limit: int = 10, job_id: int | None = None, tailor: bool = True,
        wait_minutes: float | None = None, browser_factory=None,
        prepared: bool = False, chosen: bool = False, paid_ok: bool = False) -> list[Outcome]:
    applicant = _ready_applicant(cfg)
    wait_s = 60 * float(wait_minutes if wait_minutes is not None
                        else cfg.get("apply.wait_minutes", DEFAULT_WAIT_MINUTES))
    if chosen:
        # The jobs the person chose: gone through from the queue kept in the database, one at a
        # time, each checked again just before it goes (backend/apply/execution.py).
        from backend.apply import execution
        return execution.work(cfg, applicant, browser_factory=browser_factory, tailor=tailor, wait_s=wait_s,
                              paid_ok=paid_ok)

    ready = {}
    if prepared:
        from backend.apply.prepare import pending
        ready = {e["job_id"]: e for e in pending()}
        if not ready:
            say("Nothing prepared to apply to. The morning run prepares the day's best matches; "
                "or press Prepare now on the Apply page.")
            return []
    queue, left = candidates(cfg, applicant, limit, job_id, list(ready) or None)
    if left <= 0 and job_id is None:
        say("Today's application limit is reached — nothing more until tomorrow.")
        return []
    if not queue:
        if job_id is not None:
            say(f"Can't auto-apply to this one: {why_not(job_id)}")
            return []
        months = age_limit_months(cfg)
        say("Nothing to apply to right now: no open Greenhouse, Workday or JazzHR posting"
            + (f" under {months:g} months old" if months > 0 else "")
            + " above your score threshold that you haven't applied to or marked yourself.")
        return []

    # Safety gate (26 Oct 2026): these entry points lack Phase 3 document and
    # question checks. The saved preference is not authority for a legacy run.
    auto = False
    say(f"Applying to {len(queue)} ({summarise(queue)}) · {left} left today · "
        f"{'auto-submit where no CAPTCHA' if auto else 'you press Submit on each'}")

    context, close = open_browser(cfg, browser_factory)
    outcomes: list[Outcome] = []
    try:
        for index, candidate in enumerate(queue, start=1):
            with get_session() as session:
                job = session.get(Job, candidate.job_id)
                if job is not None:
                    session.expunge(job)
            if job is None:                          # deleted since the queue was made (a prune, say)
                say(f"[{index}/{len(queue)}] {candidate.company} — {candidate.title}: no longer in your "
                    f"jobs list. Skipped.")
                continue
            head = f"[{index}/{len(queue)}] {job.company} — {job.title}"
            try:
                page = context.new_page()
            except Exception:
                say(f"{head}: the browser was closed — stopping.")
                outcomes.append(Outcome(job.id, job.company, job.title, "stopped"))
                break
            with answers_for(applicant, job, ready):
                outcome = apply_one(cfg, page, job, applicant, head, auto=auto, tailor=tailor, wait_s=wait_s)
            outcomes.append(outcome)
            if job.id in ready and outcome.result in ("submitted", "skipped"):
                from backend.apply.prepare import mark
                mark(job.id, "applied" if outcome.result == "submitted" else "skipped")
            close_page(page)
    finally:
        close()
    return finish(outcomes)


def open_browser(cfg, browser_factory=None):
    """The app's own browser -- one window, a tab per form -- and how to close it."""
    if browser_factory is not None:
        return browser_factory(), lambda: None
    from playwright.sync_api import sync_playwright

    from backend.apply.helper import launch
    manager = sync_playwright().start()
    browser, context = launch(manager, cfg, user_agent=greenhouse.USER_AGENT,
                              viewport={"width": 1280, "height": 1400})

    def close():
        for closer in (c for c in (context, browser) if c is not None):   # a profile window has no browser
            try:
                closer.close()
            except Exception:
                pass
        manager.stop()
    return context, close


def close_page(page) -> None:
    try:
        if not page.is_closed():
            page.close()
    except Exception:
        pass


@contextmanager
def answers_for(applicant: Applicant, job, ready: dict | None = None):
    """This form's own answers, given on the Apply page, for this job only; the usual ones after."""
    saved = dict(applicant.answers)
    applicant.job = job
    entry = (ready or {}).get(job.id)
    if entry is None:
        from backend.apply.prepare import load as load_prepared
        entry = load_prepared().get(str(job.id))
    if entry is not None:
        from backend.apply.answers import normalise
        own = {normalise(k): v for k, v in (entry.get("answers") or {}).items()}
        applicant.answers.update(own)
        applicant.job_answers = own
    try:
        yield
    finally:
        applicant.answers.clear()
        applicant.answers.update(saved)
        applicant.job, applicant.job_answers = None, {}


def finish(outcomes: list[Outcome]) -> list[Outcome]:
    """After the run: forms for the person's own browser opened there, and what came of it all."""
    # Only now, with the app's window gone: a URL sent to Chrome while it was open could land in it.
    handed = [o for o in outcomes if o.handoff]
    for o in handed:
        if not open_own_browser(o.handoff):
            say(f"   Open {o.handoff} in your own browser to send {o.company}'s form.")
    sent = [o for o in outcomes if o.result == "submitted"]
    say(f"Done: {len(sent)} submitted, "
        f"{sum(o.result == 'skipped' for o in outcomes)} skipped, "
        f"{sum(o.result in ('timeout', 'not-filled') for o in outcomes)} not sent"
        + (f", {len(handed)} to send from your own browser" if handed else "") + ".")
    return outcomes


@dataclass
class Prepared:
    """What one application sends: the resume and cover letter under the names the employer sees,
    and the files each was copied from (`origins`: "resume", "cover_letter"), so a run can tell
    when the job's own document has changed since (backend/apply/execution.seal)."""
    kept: Path
    letter: Path | None
    resume_note: str
    evidence: Path
    origins: dict = field(default_factory=dict)


def prepare_chosen(cfg, *, tailor: bool = True, paid_ok: bool = False) -> int:
    """Prepare these: the chosen jobs' resumes and cover letters made ready and written down, each
    file with its checksum; no form opened, nothing sent (backend/apply/execution.prepare_these)."""
    from backend.apply import execution
    return execution.prepare_these(cfg, _ready_applicant(cfg), tailor=tailor, paid_ok=paid_ok)


def apply_one(cfg, page, job, applicant: Applicant, head: str, *,
              auto: bool, tailor: bool, wait_s: float) -> Outcome:
    ready = prepare_documents(cfg, job, applicant, head, tailor=tailor)
    if isinstance(ready, str):
        return Outcome(job.id, job.company, job.title, "not-filled", detail=ready)
    # Also guard direct callers of this legacy helper. Chosen jobs use fill_form
    # only after eligibility, required questions and document sealing succeed.
    return fill_form(cfg, page, job, applicant, head, ready, auto=False, wait_s=wait_s)


def prepare_documents(cfg, job, applicant: Applicant, head: str, *, tailor: bool) -> Prepared | str:
    """The resume (tailored now if need be) and the cover letter, ready to attach; or why not.
    Split from filling the form (24 Oct 2026) so the chosen jobs' queue can say which it is doing."""
    evidence = greenhouse._evidence_dir(job.id)

    resume, resume_note = choose_resume(cfg, job, tailor=tailor)
    if resume is None:
        say(f"{head}: no resume to attach — put yours in ml/resume/. Skipped.")
        return "no resume"
    shutil.copy2(resume, evidence / f"resume{resume.suffix}")    # exactly what went out, for the app's pages
    # What the employer receives is named for the person and the company, never "resume.pdf".
    kept = sent_copy(resume, evidence, applicant, job.company, "Resume")
    try:
        # The job page's checklist reads the folder: without it, a run that tailored
        # the resume still showed "0 of 4 done" and the letter step stayed locked.
        from ml.resume.packet import build_packet
        build_packet(cfg, job)
    except Exception as exc:                   # a folder problem never stops the application
        log.warning("No application folder for job %s: %s", job.id, exc)
    write_letter_if_missing(cfg, job, head)
    letter = cover_letter(cfg, job, evidence)
    origins: dict[str, Path | None] = {"resume": resume}
    if letter is not None:
        letter = sent_copy(letter, evidence, applicant, job.company, "Cover_Letter")
        origins["cover_letter"] = _letter_origin(cfg, job)
    return Prepared(kept, letter, resume_note, evidence, origins)


def _letter_origin(cfg, job) -> Path | None:
    """The job's own cover letter that `cover_letter` copied: its PDF, else its text."""
    from ml.resume import letter_edit
    from ml.resume.packet import LETTER_FILE, packet_dir
    folder = packet_dir(cfg, job.company, job.title, job.external_id or "", job_id=job.id)
    return letter_edit.document(folder, "pdf") or ((folder / LETTER_FILE) if (folder / LETTER_FILE).exists() else None)


def writer(cfg) -> tuple[str, str, bool]:
    """The writing model tailoring and cover letters use, as set in Settings: (name, label, free).
    Free is the person's own plan or this Mac's model -- no bill per use. Its fallbacks are only ever
    free ones (ml/resume/llm.complete); a writer that bills is used only when the person chose it,
    and a run asks before using one (26 Oct 2026)."""
    from ml.resume import llm
    name = llm.provider_name(cfg)
    spec = llm.PROVIDERS.get(name, {})
    return name, str(spec.get("label", name)), bool(spec.get("free", False))


def fill_form(cfg, page, job, applicant: Applicant, head: str, ready: Prepared, *,
              auto: bool, wait_s: float) -> Outcome:
    """Open the job's form in the app's browser, fill it, and see how it ends."""
    outcome = Outcome(job.id, job.company, job.title, "not-filled")
    kept, letter, resume_note, evidence = ready.kept, ready.letter, ready.resume_note, ready.evidence
    if job.source == "workday":
        return _apply_workday(cfg, page, job, applicant, head, outcome, kept, letter,
                              resume_note, evidence, wait_s)
    if job.source == "jazzhr":
        return _apply_jazzhr(page, job, applicant, head, outcome, kept, letter, resume_note, evidence, wait_s)
    if job.source != "greenhouse":
        return _apply_anywhere(cfg, page, job, applicant, head, outcome, kept, letter, resume_note,
                               evidence, wait_s, auto=auto)
    return _apply_greenhouse(page, job, applicant, head, outcome, kept, letter, resume_note, evidence,
                             wait_s, greenhouse.form_url(job.company_slug, job.external_id), auto=auto)


def _apply_greenhouse(page, job, applicant, head, outcome, kept, letter, resume_note, evidence, wait_s,
                      url, *, auto: bool) -> Outcome:
    """Greenhouse: filled, and sent by itself only when nothing on it needs the person."""
    result = greenhouse.fill(page, job.id, url, applicant, kept, letter,
                             submit=auto, evidence=evidence)
    if not result.ok:
        say(f"{head}: {result.summary()}")
        outcome.detail = result.error or ""
        return outcome

    say(f"{head}: {result.summary()} · {resume_note}")
    answers: dict[str, str] = {}
    if result.submitted and greenhouse.await_submission(page, 30) == "submitted":
        how, verdict = "auto", "submitted"
    else:
        waiting = []
        if result.submitted and greenhouse.challenge_shown(page):
            waiting.append("the CAPTCHA puzzle that appeared")
        elif result.submitted:
            waiting.append("whatever the form flagged after Submit")
        if result.captcha:
            waiting.append("the CAPTCHA")
        if result.unchecked:
            waiting.append("what the app could not check: " + "; ".join(result.unchecked[:3]))
        if result.declarations:
            waiting.append(f"{len(result.declarations)} legal question(s)")
        if result.required_blank:
            waiting.append("required: " + ", ".join(result.required_blank[:4]))
        say(f"   Waiting for you — finish {'; '.join(waiting) or 'the form'}, "
            f"then press Submit — the \"Submit application\" button at the very bottom of the form, "
            f"outlined in orange. Close the tab to skip this one.")
        verdict, answers = greenhouse.watch_submission(page, wait_s)
        how = "you"

    _settle(outcome, job, verdict, how, url, kept, resume_note, letter, result, evidence, answers)
    return outcome


def _apply_workday(cfg, page, job, applicant, head, outcome, kept, letter, resume_note,
                   evidence, wait_s, url: str | None = None) -> Outcome:
    """Workday: sign-in and Submit are the person's; every step between is filled."""
    from backend.apply import workday

    url = url or job.application_url
    say(f"{head}: opening Workday · {resume_note}")
    # The sign-in wait comes out of the same budget, so allow for it.
    verdict, result = workday.apply(page, job.id, url, applicant, kept,
                                    evidence=evidence, wait_s=wait_s + 300, say=say)
    if verdict == "not-filled":
        say(f"{head}: {result.summary()}")
        outcome.detail = result.error or ""
        return outcome
    _settle(outcome, job, verdict, "you", url, kept, resume_note, letter,
            result, evidence, result.given)
    return outcome


def _letter_text(cfg, job) -> str | None:
    """The cover letter as plain text, for a form that takes it in a box rather than a file."""
    from ml.resume.packet import LETTER_FILE, packet_dir
    folder = packet_dir(cfg, job.company, job.title, job.external_id or "", job_id=job.id)
    try:
        return (folder / LETTER_FILE).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _apply_anywhere(cfg, page, job, applicant, head, outcome, kept, letter, resume_note, evidence,
                    wait_s, *, auto: bool) -> Outcome:
    """Any other board: follow the posting to the employer's own form, fill it, wait for the person."""
    from backend.apply import anyform

    # The employer's own page, when the person added it on the job's page, rather than the job site's.
    # This job's own page on the company's site, when the person added it, rather than the job site's
    # link; a careers site added is a link to look from, never taken for the form (joblinks).
    from backend.core.joblinks import apply_link
    start = apply_link(job)
    own = start != job.application_url
    say(f"{head}: opening {'the page you added' if own else f'the posting on {job.source}'} · {resume_note}")
    reached = anyform.reach(page, start, say=say, check_s=min(wait_s, 300))
    if reached.error:
        say(f"{head}: not filled: {reached.error}")
        outcome.detail = reached.error
        return outcome
    page = reached.page
    if reached.kind == "signin-handoff":
        result = anyform.FillResult(job_id=job.id, url=start)
        return _hand_off(job, start, result, head, outcome, kept, letter, resume_note, evidence,
                         reason=anyform.sign_in_barrier(page) or "This page requires sign-in.")
    if reached.kind == "greenhouse" and anyform.greenhouse_form(reached.url):
        say("   It leads to a Greenhouse form.")
        return _apply_greenhouse(page, job, applicant, head, outcome, kept, letter, resume_note, evidence,
                                 wait_s, anyform.greenhouse_form(reached.url), auto=False)
    if reached.kind == "jazzhr":
        say("   It leads to a JazzHR form.")
        return _apply_jazzhr(page, job, applicant, head, outcome, kept, letter, resume_note, evidence,
                             wait_s, url=reached.url)
    if reached.kind == "workday":
        say("   It leads to Workday.")
        return _apply_workday(cfg, page, job, applicant, head, outcome, kept, letter, resume_note,
                              evidence, wait_s, url=reached.url)

    letter_text = _letter_text(cfg, job)
    seen: set[str] = set()
    result = anyform.fill(page, job.id, reached.url, applicant, kept, letter,
                          letter_text=letter_text, evidence=evidence, seen=seen)
    waiting: list[str] = []
    if reached.kind == "signin":
        say("   This site wants you to sign in or make an account first. Do that in the window, "
            "and the app fills the form when it shows.")
    elif reached.kind == "unknown":
        say("   The application form isn't on this page. Open it in the window (its Apply button), "
            "and the app fills it when it shows.")
    elif hands_off(reached.url):
        return _hand_off(job, reached.url, result, head, outcome, kept, letter, resume_note, evidence)
    else:
        say(f"{head}: {result.summary()} · on {reached.url.split('?')[0][:90]}")
        if result.captcha:
            waiting.append("the CAPTCHA")
        if result.declarations:
            waiting.append("the box(es) to tick yourself: " + "; ".join(d[:50] for d in result.declarations[:3]))
        if result.required_blank:
            waiting.append("required: " + ", ".join(result.required_blank[:4]))
        anyform.show_submit(page, scroll=not (result.required_blank or result.declarations))
    say(f"   Waiting for you — check it{', finish ' + '; '.join(waiting) if waiting else ''}, then press "
        f"Submit (outlined in orange). Leave the tab open until this says Applied; close it to skip.")
    verdict, answers, page = anyform.watch(page, applicant, result, wait_s, resume_path=kept,
                                           letter_path=letter, letter_text=letter_text, seen=seen,
                                           evidence=evidence, say=say)
    if verdict == "signin-handoff":
        return _hand_off(job, start, result, head, outcome, kept, letter, resume_note, evidence,
                         reason=anyform.sign_in_barrier(page) or "This page requires sign-in.")
    if verdict == "submitted":
        anyform.forget_unconfirmed(job.id)
    elif result.filled or result.attachments:
        # The site may have said "sent" in words the app does not know: the job page asks.
        anyform.keep_unconfirmed(job.id, {
            "company": job.company, "title": job.title, "url": reached.url, "how": "you",
            "resume": str(kept), "resume_source": resume_note,
            "cover_letter": str(letter) if letter else None, "filled": result.filled,
            "evidence": str(evidence), **_queue_link()})
    _settle(outcome, job, verdict, "you", reached.url, kept, resume_note, letter, result, evidence, answers)
    return outcome


def _hand_off(job, url, result, head, outcome, kept, letter, resume_note, evidence, *, reason=None) -> Outcome:
    """A form whose CAPTCHA turns the app away: what it asks, answered, for the person's own browser.
    The job page lists each answer with a Copy button and the files to attach, and asks whether it
    went -- "Yes" records the application as the app would have."""
    from backend.apply import anyform
    anyform.keep_unconfirmed(job.id, {
        "company": job.company, "title": job.title, "url": url, "how": "you", "handoff": True,
        "resume": str(kept), "resume_source": resume_note, "cover_letter": str(letter) if letter else None,
        "filled": result.filled, "left": list(result.required_blank) + list(result.declarations),
        "evidence": str(evidence), **_queue_link()})
    say(f"{head}: {result.summary()}")
    reason = reason or "This site's CAPTCHA turns away the app's own browser even when you press Submit."
    say(f"   {reason} Continue in your own browser: the posting or form opens in your own Chrome "
        "when this window closes. The job page lists "
        "every answer with a Copy button and the files to attach; say there once it's sent.")
    outcome.result, outcome.how, outcome.handoff = "handoff", "you", url
    return outcome


def _apply_jazzhr(page, job, applicant, head, outcome, kept, letter, resume_note, evidence, wait_s,
                  url: str | None = None) -> Outcome:
    """JazzHR: every field the profile and the person's answers cover is filled; the
    Human Check and Submit are the person's."""
    from backend.apply import jazzhr
    url = url or job.application_url
    say(f"{head}: opening the JazzHR form · {resume_note}")
    result = jazzhr.fill(page, job.id, url, applicant, kept, evidence=evidence)
    if not result.ok:
        say(f"{head}: {result.summary()}")
        outcome.detail = result.error or ""
        return outcome
    say(f"{head}: {result.summary()}")
    waiting = ["tick “I'm not a robot” (the Human Check, outlined in orange)"] if result.captcha else []
    if result.required_blank:
        waiting.append("required: " + ", ".join(result.required_blank[:4]))
    say(f"   Waiting for you — check the answers, {'; '.join(waiting) or 'look it over'}, then press "
        f"Submit Application at the bottom. Close the tab to skip this one.")
    verdict, answers = jazzhr.watch_submission(page, wait_s, evidence=evidence)
    _settle(outcome, job, verdict, "you", url, kept, resume_note, letter, result,
            evidence, answers)
    return outcome


def _settle(outcome, job, verdict, how, url, kept, resume_note, letter, result, evidence,
            answers) -> None:
    """Record and report how one application ended."""
    outcome.result, outcome.how = verdict, how
    # Your written answers ("Describe the deepest AWS estate you have owned…") for the next forms that
    # ask something like them (backend/apply/written.py), whatever became of this one: the last reading of
    # the form, which is what you sent -- or, on a form the app sent itself, what the app typed into it
    # (the answers you gave for this job on the Apply page among them).
    source = answers or (result.filled if verdict == "submitted" else None)
    if source:
        from backend.apply import written
        kept_written = written.keep_from(source, job.company, job.title, job.id)
        if kept_written:
            say(f"   Kept {len(kept_written)} written answer(s) of yours for similar questions later.")
    if verdict == "submitted":
        # The form as it went: your final answers -- what you typed or changed on it -- over what the
        # app typed. Only the app's own fields were kept, and a whole set of answers had to be pasted in.
        final = {**(result.filled or {}), **(answers or {})}
        record_application(job.id, {
            "company": job.company, "title": job.title, "url": url, "how": how,
            "resume": str(kept), "resume_source": resume_note,
            "cover_letter": str(letter) if letter else None,
            "filled": final, "filled_by_app": result.filled, "evidence": str(evidence),
        })
        say(f"   Applied ✓ ({'sent automatically' if how == 'auto' else 'you submitted'})")
        if answers:
            from backend.apply.answers import remember
            learned = remember(answers, company=job.company)
            if learned:
                say(f"   Learned {len(learned)} of your answers for next time: "
                    + "; ".join(learned[:4]))
    elif verdict == "skipped":
        say("   Skipped (tab closed) — it stays in the queue for next time.")
        # What the person typed before closing is still theirs: general answers
        # ("5+ years of Kubernetes?") are kept, so the same question is not asked
        # again. Company-specific ones ("Why five9?") are never reused.
        if answers:
            from backend.apply.answers import remember
            learned = remember(answers, company=job.company)
            if learned:
                say(f"   Kept {len(learned)} of your answers for next time: " + "; ".join(learned[:4]))
    else:
        say("   No submission in the time allowed — moving on; it stays in the queue.")
        if answers:                     # what was typed before time ran out is still theirs
            from backend.apply.answers import remember
            learned = remember(answers, company=job.company)
            if learned:
                say(f"   Kept {len(learned)} of your answers for next time: " + "; ".join(learned[:4]))
