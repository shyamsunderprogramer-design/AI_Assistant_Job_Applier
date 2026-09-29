"""Work through the apply queue: fill each form, submit or hand it over, record it.

One visible browser for the whole run, one tab per application. For each job:

  1. Pick the resume. The tailored one if the guard passed it; otherwise tailor
     now; if that is refused or no model is running, the base resume — which is
     the person's own words, so it is always safe to send.
  2. Open the Greenhouse form itself (`greenhouse.form_url`), not the posting's
     link, and fill everything the profile can answer.
  3. If nothing needs a person — no CAPTCHA, no unanswered declaration, no
     blank required question — and the profile allows it, submit. Otherwise
     wait for the person: they finish the form and press Submit, or close the
     tab to skip it.
  4. Only a confirmation page counts as applied. Then the job is marked
     Applied, and a line goes into data/applications.jsonl with what was sent.

Printed lines are the interface: the web page shows the tail of this run's
output, so every line is written for the person watching.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backend.apply import greenhouse
from backend.apply.profile import Applicant, ProfileIncomplete, check_ready, load
from backend.apply.queue import build_queue, summarise
from backend.config.loader import PROJECT_ROOT
from data_engineering.db.models import Job
from data_engineering.db.session import get_session

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
    result: str          # submitted | skipped | timeout | not-filled | stopped
    how: str = ""        # auto | you
    detail: str = ""


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
    limit = int(cfg.get("resume.max_pages", 2) or 0)
    if limit > 0:
        fit_to_pages(base, result, job.description or job.title,
                     requirements=job.requirements, company=job.company, max_pages=limit)
    stem = output_filename(job.company, job.title, job.external_id or "")[:-5]
    target = packet_root(cfg) / f"{stem}_selected.pdf"
    write_pdf(base, result, target)
    return target


def choose_resume(cfg, job, *, tailor: bool = True) -> tuple[Path | None, str]:
    """(file to attach, where it came from)."""
    found = tailored_resume(cfg, job)
    if found:
        return found, "tailored"
    note = "tailoring off"
    if tailor:
        try:
            from ml.resume.pipeline import tailor_jobs
            outcomes = tailor_jobs(cfg, [job.id])
            status = outcomes[0].status if outcomes else "not tailored"
            found = tailored_resume(cfg, job)
            if found:
                return found, "tailored just now"
            note = f"tailoring {status}"
        except Exception as exc:              # no model running, no key, ...
            note = f"tailoring unavailable ({type(exc).__name__})"
    return selected_master(cfg, job), f"your resume selected for this job — {note}"


def cover_letter(cfg, job, evidence: Path) -> Path | None:
    """The drafted letter, as a .txt Greenhouse will accept, if one exists."""
    from ml.resume.packet import LETTER_FILE, packet_dir

    letter = packet_dir(cfg, job.company, job.title, job.external_id or "") / LETTER_FILE
    if not letter.exists():
        return None
    target = evidence / "cover-letter.txt"
    target.write_text(letter.read_text(encoding="utf-8"), encoding="utf-8")
    return target


# -- recording ------------------------------------------------------------

def record_application(job_id: int, entry: dict, *, audit_log: Path | None = None) -> None:
    """Mark the job Applied and keep a line saying exactly what went out."""
    audit_log = audit_log or AUDIT_LOG          # read at call time, not import
    now = datetime.now(timezone.utc)
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is not None:
            job.status = "Applied"
            job.applied_at = now
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    with audit_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": now.isoformat(), "job_id": job_id, **entry}) + "\n")


# -- the run --------------------------------------------------------------

def candidates(cfg, applicant: Applicant, limit: int, job_id: int | None = None):
    """What this run will apply to, and how many today's cap still allows."""
    with get_session() as session:
        left = remaining_today(cfg, applicant, _applied_since(session, start_of_today_utc()))
        if job_id is not None:
            job = session.get(Job, job_id)
            jobs = [job] if job is not None else []
            queue = build_queue(jobs, limit=1)
        else:
            min_score = cfg.get("apply.min_score", cfg.get("resume.min_score", 0.45))
            queue = build_queue(
                session.query(Job).filter(Job.is_open.is_(True)).all(),
                limit=min(limit, left),
                min_score=float(min_score) if min_score is not None else None,
                last_applied=_last_applied_by_company(session),
            )
    return queue, left


def run(cfg, *, limit: int = 10, job_id: int | None = None, tailor: bool = True,
        wait_minutes: float | None = None, browser_factory=None) -> list[Outcome]:
    try:
        applicant = load(PROJECT_ROOT / "backend" / "config" / "applicant.yaml")
    except ProfileIncomplete as exc:
        raise NotReady(f"{exc}\nFill in your details at http://127.0.0.1:8770/applicant")
    problems = check_ready(applicant)
    if problems:
        raise NotReady("Your applicant details are incomplete:\n"
                       + "\n".join(f"  - {p}" for p in problems)
                       + "\nFix them at http://127.0.0.1:8770/applicant")

    queue, left = candidates(cfg, applicant, limit, job_id)
    if left <= 0 and job_id is None:
        say("Today's application limit is reached — nothing more until tomorrow.")
        return []
    if not queue:
        say("Nothing to apply to right now: no open Greenhouse or Workday posting above your "
            "score threshold that you haven't applied to or marked yourself.")
        return []

    auto = not applicant.stop_before_submit
    wait_s = 60 * float(wait_minutes if wait_minutes is not None
                        else cfg.get("apply.wait_minutes", DEFAULT_WAIT_MINUTES))
    say(f"Applying to {len(queue)} ({summarise(queue)}) · {left} left today · "
        f"{'auto-submit where no CAPTCHA' if auto else 'you press Submit on each'}")

    if browser_factory is None:
        from playwright.sync_api import sync_playwright
        manager = sync_playwright().start()
        from backend.apply.helper import launch
        browser, context = launch(manager, cfg, user_agent=greenhouse.USER_AGENT,
                                  viewport={"width": 1280, "height": 1400})
    else:
        manager, browser, context = None, None, browser_factory()

    outcomes: list[Outcome] = []
    try:
        for index, candidate in enumerate(queue, start=1):
            with get_session() as session:
                job = session.get(Job, candidate.job_id)
                session.expunge(job)
            head = f"[{index}/{len(queue)}] {job.company} — {job.title}"
            try:
                page = context.new_page()
            except Exception:
                say(f"{head}: the browser was closed — stopping.")
                outcomes.append(Outcome(job.id, job.company, job.title, "stopped"))
                break
            outcomes.append(apply_one(cfg, page, job, applicant, head,
                                      auto=auto, tailor=tailor, wait_s=wait_s))
            try:
                if not page.is_closed():
                    page.close()
            except Exception:
                pass
    finally:
        for closer in (context, browser):
            try:
                if closer is not None and browser_factory is None:
                    closer.close()
            except Exception:
                pass
        if manager is not None:
            manager.stop()

    sent = [o for o in outcomes if o.result == "submitted"]
    say(f"Done: {len(sent)} submitted, "
        f"{sum(o.result == 'skipped' for o in outcomes)} skipped, "
        f"{sum(o.result in ('timeout', 'not-filled') for o in outcomes)} not sent.")
    return outcomes


def apply_one(cfg, page, job, applicant: Applicant, head: str, *,
              auto: bool, tailor: bool, wait_s: float) -> Outcome:
    outcome = Outcome(job.id, job.company, job.title, "not-filled")
    evidence = greenhouse._evidence_dir(job.id)

    resume, resume_note = choose_resume(cfg, job, tailor=tailor)
    if resume is None:
        say(f"{head}: no resume to attach — put yours in ml/resume/. Skipped.")
        outcome.detail = "no resume"
        return outcome
    kept = evidence / f"resume{resume.suffix}"
    shutil.copy2(resume, kept)                 # exactly what went out
    letter = cover_letter(cfg, job, evidence)

    if job.source == "workday":
        return _apply_workday(cfg, page, job, applicant, head, outcome, kept, letter,
                              resume_note, evidence, wait_s)

    url = greenhouse.form_url(job.company_slug, job.external_id)
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
        if result.declarations:
            waiting.append(f"{len(result.declarations)} legal question(s)")
        if result.required_blank:
            waiting.append("required: " + ", ".join(result.required_blank[:4]))
        say(f"   Waiting for you — finish {'; '.join(waiting) or 'the form'}, "
            f"then press Submit. Close the tab to skip this one.")
        verdict, answers = greenhouse.watch_submission(page, wait_s)
        how = "you"

    _settle(outcome, job, verdict, how, url, kept, resume_note, letter, result, evidence, answers)
    return outcome


def _apply_workday(cfg, page, job, applicant, head, outcome, kept, letter, resume_note,
                   evidence, wait_s) -> Outcome:
    """Workday: sign-in and Submit are the person's; every step between is filled."""
    from backend.apply import workday

    say(f"{head}: opening Workday · {resume_note}")
    # The sign-in wait comes out of the same budget, so allow for it.
    verdict, result = workday.apply(page, job.id, job.application_url, applicant, kept,
                                    evidence=evidence, wait_s=wait_s + 300, say=say)
    if verdict == "not-filled":
        say(f"{head}: {result.summary()}")
        outcome.detail = result.error or ""
        return outcome
    _settle(outcome, job, verdict, "you", job.application_url, kept, resume_note, letter,
            result, evidence, {})
    return outcome


def _settle(outcome, job, verdict, how, url, kept, resume_note, letter, result, evidence,
            answers) -> None:
    """Record and report how one application ended."""
    outcome.result, outcome.how = verdict, how
    if verdict == "submitted":
        record_application(job.id, {
            "company": job.company, "title": job.title, "url": url, "how": how,
            "resume": str(kept), "resume_source": resume_note,
            "cover_letter": str(letter) if letter else None,
            "filled": result.filled, "evidence": str(evidence),
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
    else:
        say("   No submission in the time allowed — moving on; it stays in the queue.")
