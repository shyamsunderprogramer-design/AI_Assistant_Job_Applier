"""Phase 3 orchestration: score → tailor → guard → write → record.

Scoring runs without an API key, so `score-only` mode is useful on its own:
it ranks the whole backlog by fit before any money is spent on tailoring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backend.config.loader import PROJECT_ROOT
from data_engineering.db.models import Job, utcnow
from data_engineering.db.session import get_session
from backend.core.jobage import age_days
from ml.resume.parser import Resume, find_base_resume, parse_resume
from ml.resume.scorer import score_basis, score_resume
from ml.resume.writer import output_filename, write_review_note, write_tailored_resume

log = logging.getLogger(__name__)


def _profile_titles(cfg) -> tuple[list[str], list[str]]:
    """(titles searched for, titles actually held) from the derived profile.

    Empty lists when there is no profile, and then role fit is skipped rather
    than guessed -- an absent profile must not silently penalise every job.
    """
    try:
        from ml.resume.profile import PROFILE_FILENAME, load_profile

        profile = load_profile(
            PROJECT_ROOT / cfg.get("filters.profile_path", PROFILE_FILENAME))
        if profile is None:
            return [], []
        return list(profile.titles or []), list(profile.held_titles or [])
    except Exception:
        return [], []


@dataclass
class RunCost:
    """What a tailoring run cost, and what it was allowed to cost."""

    spent: float = 0.0
    cap: float = 0.0
    calls: int = 0
    stopped_early: bool = False


@dataclass
class JobOutcome:
    job_id: int
    company: str
    title: str
    score: float
    status: str            # scored | tailored | rejected | below-threshold | error
    detail: str = ""
    resume_path: Path | None = None
    posted_at: datetime | None = None      # for display; None when unknown
    found_at: datetime | None = None
    workplace: str | None = None
    experience_min_years: int | None = None
    experience_max_years: int | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    salary_period: str | None = None


def load_base_resume(cfg) -> Resume:
    configured = cfg.get("resume.base_path")
    path = Path(configured) if configured else None
    if path and not path.is_absolute():
        path = PROJECT_ROOT / path
    if path is None or not path.exists():
        found = find_base_resume(PROJECT_ROOT / "ml" / "resume")
        if found is None:
            raise FileNotFoundError(
                "No base resume found. Put your .docx or .pdf in ml/resume/ "
                "or set resume.base_path in config.yaml."
            )
        path = found
    log.info("Base resume: %s", path)
    return parse_resume(path)



def over_page_limit(cfg, target: Path) -> bool:
    """Is this saved resume longer than the page limit — made before the limit?

    Such a file is out of date, not finished: it is re-tailored rather than
    skipped, and never attached to an application. Judged by its PDF; a
    .docx with no PDF beside it predates both, and is treated as too long.
    """
    limit = int(cfg.get("resume.max_pages", 2) or 0)
    if limit <= 0:
        return False
    pdf = Path(target).with_suffix(".pdf")
    if not pdf.exists():
        return True
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(pdf)).pages) > limit
    except Exception:
        return False


def _fit(cfg, resume, result, job, focus_terms=None) -> None:
    """Select from the master resume for this posting, down to the page limit.

    Only an accepted result is fitted: a rejected one writes nothing anyway.
    Runs before the review note so the note lists what was left out and why.
    """
    if not result.accepted:
        return
    from ml.resume.fit import fit_to_pages

    limit = int(cfg.get("resume.max_pages", 2) or 0)
    if limit <= 0:
        return
    try:
        pages = fit_to_pages(resume, result, job.description or job.title,
                             requirements=job.requirements, company=job.company,
                             max_pages=limit, focus_terms=focus_terms)
        log.info("Fitted to %d page(s) for %s — %s", pages, job.company, job.title)
    except Exception as exc:        # never lose a tailoring over a length check
        log.warning("Could not fit to %d pages (%s: %s) — writing it untrimmed",
                    limit, type(exc).__name__, exc)

def tailor_to_target(cfg, job_id: int, *, again: bool = False) -> tuple[list[JobOutcome], dict | None]:
    """Tailor one job until its ATS match reaches `resume.ats_target` (80%).

    Each round after the first rewrites again, bringing back posting terms
    the resume has and framing its own tools in the posting's words; a round
    that does worse is thrown away, so the file only ever improves. It stops
    at the target, after `resume.max_attempts` rounds, or as soon as nothing
    honest is left to gain -- no term the resume has is missing and no kind
    of work is left to frame. What remains then is what the resume does not
    show, and the note says so instead of the resume pretending otherwise.

    `again` is the "Rewrite again" button: start from the current resume
    rather than from scratch.
    """
    from ml.resume.ats import check

    target = int(cfg.get("resume.ats_target", 80) or 80)
    rounds = max(1, int(cfg.get("resume.max_attempts", 3) or 3))
    outcomes: list[JobOutcome] = []
    ats = None
    scores: list[int] = []
    for attempt in range(rounds):
        done = tailor_jobs(cfg, [job_id], align=again or attempt > 0)
        outcomes += done
        if not done or done[-1].status not in ("tailored", "kept-previous"):
            break
        with get_session() as session:
            job = session.get(Job, job_id)
            session.expunge(job)
        ats = check(cfg, job)
        scores.append(ats["after"] or 0)
        log.info("Round %d for %s — %s: ATS match %s%%", attempt + 1, job.company, job.title,
                 ats["after"])
        if (ats["after"] or 0) >= target:
            break
        if not ats["fixable"] and not ats["concepts"]:
            break                      # nothing left that the resume can honestly show
        if len(scores) >= 2 and scores[-1] <= max(scores[:-1]):
            break                      # a round that did not help; another will not either
    if ats is not None:
        ats["target"] = target
        ats["rounds"] = len(scores)
    return outcomes, ats


def target_summary(ats: dict | None) -> str:
    """One line for the person: reached, or the honest ceiling and why."""
    if not ats or ats.get("after") is None:
        return ""
    head = (f"ATS match: your resume {ats['before']}% → tailored {ats['after']}% "
            f"({ats.get('rounds', 1)} round(s))")
    if ats["after"] >= ats.get("target", 80):
        return head + f" — reached the {ats.get('target', 80)}% target."
    missing = ats.get("tools", []) + ats.get("concepts", [])
    why = (f" This posting asks for {', '.join(missing[:6])}, which your resume does not show."
           if missing else " What is left is the posting's general wording, not skills your "
           "resume lacks — this resume is ready to send.")
    return (head + f" — {ats['after']}% is the best honest match for this job." + why
            + " Add real experience to your master resume to go higher, or press Rewrite again.")


def _snapshot(target: Path) -> dict:
    """The current tailored files and their ATS match, to put back if a rewrite is worse."""
    import json
    files = [target, target.with_suffix(".pdf"), target.with_name(target.stem + "_review.txt"),
             target.with_name(target.stem + "_ats.json")]
    saved = {f: f.read_bytes() for f in files if f.exists()}
    try:
        after = json.loads(saved[files[3]])["after"] if files[3] in saved else None
    except (ValueError, KeyError):
        after = None
    return {"files": saved, "after": after if target.with_suffix(".pdf") in saved else None}


def _restore(previous: dict) -> None:
    for path, data in previous["files"].items():
        path.write_bytes(data)


def _note_framing(note_path: Path, result, frame) -> None:
    """Name every line given one of the posting's words it did not have.

    Framing is honest only when the line's own tools do that work, and that
    is the person's call to confirm, so each one is put in front of them.
    """
    framed = []
    for bullet in result.bullets:
        before, after = (bullet.get("original") or "").lower(), (bullet.get("tailored") or "").lower()
        for term in frame or []:
            if term in after and term not in before:
                framed.append(f"  {term}  <-  {bullet.get('tailored', '')[:150]}")
    if framed:
        with note_path.open("a", encoding="utf-8") as note:
            note.write("\nFRAMED IN THE POSTING'S WORDS (check each is true of that work):\n"
                       + "\n".join(framed) + "\n")


def _write_both(resume, result, target: Path) -> None:
    """Write the .docx, and the PDF beside it.

    Employers ask for either, and exporting one from the other by hand is the
    manual step this tool exists to remove. The PDF is drawn from the same
    document plan rather than converted, so the two cannot disagree.

    A PDF failure never loses the .docx: reportlab is an optional dependency
    and a missing font or a broken install must not cost the document that
    already succeeded.
    """
    write_tailored_resume(resume, result, target)
    try:
        from ml.resume.pdf import write_pdf

        write_pdf(resume, result, target.with_suffix(".pdf"))
    except Exception as exc:
        log.warning("PDF not written for %s (%s: %s) — the .docx is unaffected",
                    target.name, type(exc).__name__, exc)


def score_jobs(
    cfg, limit: int = 0, rescore: bool = False, include_closed: bool = False,
    max_age_days: float = 0, remote_only: bool = False,
    min_salary: int = 0, max_experience: int = 0,
) -> list[JobOutcome]:
    """Score every open job against the base resume. No API calls, no cost.

    Closed postings are skipped: ranking a filled role wastes the user's
    attention, which is the whole point of the ranking. `max_age_days` drops
    postings older than a cutoff for the same reason; 0 keeps everything.
    """
    resume = load_base_resume(cfg)
    resume_text = resume.text()
    threshold = float(cfg.get("resume.min_score", 0.45))
    outcomes: list[JobOutcome] = []

    with get_session() as session:
        query = session.query(Job)
        if not include_closed:
            query = query.filter(Job.is_open.is_(True))
        if not rescore:
            query = query.filter(Job.ats_match_score.is_(None))
        jobs = query.order_by(Job.found_at.desc()).all()
        if max_age_days:
            # A posting with no date is kept: not knowing its age is not
            # evidence that it is old.
            jobs = [
                j for j in jobs
                if (age_days(j) or 0) <= max_age_days
            ]
        if remote_only:
            jobs = [j for j in jobs if j.workplace in ("remote", "hybrid")]
        if min_salary:
            # A posting that published nothing is kept: silence is not a low
            # offer, and dropping it would hide roles that simply do not say.
            jobs = [j for j in jobs if j.salary_max is None or j.salary_max >= min_salary]
        if max_experience:
            jobs = [j for j in jobs
                    if j.experience_min_years is None
                    or j.experience_min_years <= max_experience]
        if limit:
            jobs = jobs[:limit]

        # The titles this person searches for, and the titles they have held.
        # Loaded once for the run: they are the same for every posting, and
        # they are what tells a data engineer's resume that a DevSecOps
        # posting is not their job however much vocabulary it shares.
        search_titles, held_titles = _profile_titles(cfg)

        for job in jobs:
            result = score_resume(
                resume_text,
                job.description or job.title,
                requirements=job.requirements,
                company=job.company,
                # Without these the score answers only "do you have these
                # skills", and shared infrastructure vocabulary made a
                # DevSecOps posting score 93% for a data engineer.
                job_title=job.title,
                search_titles=search_titles,
                held_titles=held_titles,
            )
            job.ats_match_score = result.score
            # Record what the number was measured against, so nothing later
            # compares a title-only score with a full one as if they meant the
            # same thing.
            job.score_basis = score_basis(job.description)
            job.exported_to_excel = False  # push the new score to Excel

            # The flag must be symmetric: a job that clears the threshold on a
            # later run (better resume, retuned threshold) has to lose a stale
            # "Manual Review", or the sheet accumulates flags that never clear.
            # Only the two system-owned states are touched — anything the user
            # set (Applied, Rejected, ...) is left alone.
            # A title-only score is capped (scorer.TITLE_ONLY_CEILING) below the
            # full threshold, so judging it by that threshold flagged every
            # Adzuna posting -- 337 of 337 -- whatever its title. It is judged
            # against its own scale instead.
            limit = (min(threshold, float(cfg.get("resume.min_score_title_only", 0.45)))
                     if job.score_basis == "title" else threshold)
            below = result.score < limit
            if below and job.status == "Not Applied":
                job.status = "Manual Review"
            elif not below and job.status == "Manual Review":
                job.status = "Not Applied"

            outcomes.append(
                JobOutcome(
                    job_id=job.id,
                    company=job.company,
                    title=job.title,
                    score=result.score,
                    status="below-threshold" if below else "scored",
                    detail=result.summary(),
                    posted_at=job.posted_at,
                    found_at=job.found_at,
                    workplace=job.workplace,
                    experience_min_years=job.experience_min_years,
                    experience_max_years=job.experience_max_years,
                    salary_min=job.salary_min,
                    salary_max=job.salary_max,
                    salary_currency=job.salary_currency,
                    salary_period=job.salary_period,
                )
            )

    return outcomes


def estimate_tailoring(cfg, job_ids: list[int] | None = None, limit: int = 0) -> dict:
    """What a tailoring run would cost, without making a single API call.

    Measures the real prompts rather than guessing at their size, so the number
    is checkable against the ledger afterwards.
    """
    from ml.resume.cost import estimate_call
    from ml.resume.tailor import MODEL, build_prompt

    resume = load_base_resume(cfg)
    jobs = _tailorable_jobs(cfg, job_ids, limit, include_closed=False)
    per_job = []
    for job in jobs:
        prompt = build_prompt(resume, job.title, job.company, job.description or "")
        per_job.append((job, estimate_call(MODEL, prompt, cfg=cfg)))

    return {
        "model": MODEL,
        "jobs": [(job.id, job.company, job.title, usd) for job, usd in per_job],
        "total": sum(usd for _, usd in per_job),
        "cap": float(cfg.get("resume.max_spend_per_run_usd", 0) or 0),
    }


def _tailorable_jobs(cfg, job_ids, limit, include_closed):
    """The jobs a tailoring run would touch. Shared by estimate and run."""
    threshold = float(cfg.get("resume.min_score", 0.45))
    with get_session() as session:
        query = session.query(Job)
        if job_ids:
            query = query.filter(Job.id.in_(job_ids))
        else:
            query = query.filter(Job.ats_match_score >= threshold)
            if not include_closed:
                query = query.filter(Job.is_open.is_(True))
        jobs = query.order_by(Job.ats_match_score.desc()).all()
    return jobs[:limit] if limit else jobs


def brief_job(cfg, job_id: int, out_dir: Path | None = None) -> tuple[Path, str, str]:
    """Write a paste-anywhere tailoring brief for one job. No API, no cost."""
    from ml.resume.manual import write_brief

    resume = load_base_resume(cfg)
    out_dir = out_dir or (PROJECT_ROOT / cfg.get("resume.output_dir", "ml/resume/output"))

    with get_session() as session:
        job = session.query(Job).filter_by(id=job_id).one_or_none()
        if job is None:
            raise LookupError(f"No job with id {job_id}. Run `score` to list them.")
        path = write_brief(
            resume=resume, job_id=job.id, company=job.company, title=job.title,
            jd_text=job.description or "", url=job.application_url,
            path=out_dir / f"{output_filename(job.company, job.title, job.external_id)[:-5]}_brief.txt",
        )
        return path, job.company, job.title


def accept_reply(cfg, job_id: int, reply_text: str) -> JobOutcome:
    """Guard a pasted model reply and write the resume if it passes.

    Everything after the model call is identical to the API path: same guard,
    same writer, same review note. The guard matters more here, not less —
    text pasted from a chat window has had no check at all before now.
    """
    from ml.resume.manual import result_from_reply

    resume = load_base_resume(cfg)
    out_dir = PROJECT_ROOT / cfg.get("resume.output_dir", "ml/resume/output")

    with get_session() as session:
        job = session.query(Job).filter_by(id=job_id).one_or_none()
        if job is None:
            raise LookupError(f"No job with id {job_id}.")

        result = result_from_reply(resume, reply_text)
        target = out_dir / output_filename(job.company, job.title, job.external_id)
        _fit(cfg, resume, result, job)
        write_review_note(result, out_dir / (target.stem + "_review.txt"))

        if not result.accepted:
            job.status = "Manual Review"
            return JobOutcome(job.id, job.company, job.title, job.ats_match_score or 0.0,
                              "rejected", result.guard.report(), None)

        _write_both(resume, result, target)
        rescored = score_resume(
            resume.text() + "\n" + result.tailored_text(),
            job.description or job.title,
            requirements=job.requirements,
            company=job.company,
        )
        job.ats_match_score = rescored.score
        job.exported_to_excel = False
        return JobOutcome(job.id, job.company, job.title, rescored.score, "tailored",
                          f"{len(result.bullets)} bullets rewritten; "
                          f"{len(result.gaps)} gap(s) noted", target)


def tailor_jobs(
    cfg,
    job_ids: list[int] | None = None,
    limit: int = 0,
    include_closed: bool = False,
    align: bool = False,
) -> list[JobOutcome]:
    """Tailor the resume for open jobs at or above the score threshold.

    Closed postings are skipped by default — a tailoring call is the most
    expensive thing here, and spending one on a filled role buys nothing.
    An explicit --job-id still wins, so a deliberate choice is never blocked.

    Every call is costed and checked against `resume.max_spend_per_run_usd`
    BEFORE it is made. A cap discovered by exceeding it is not a cap.
    """
    from ml.resume.cost import Ledger, estimate_call
    from ml.resume.tailor import MODEL, build_prompt, tailor_resume  # lazy: needs SDK + key

    resume = load_base_resume(cfg)
    threshold = float(cfg.get("resume.min_score", 0.45))
    out_dir = PROJECT_ROOT / cfg.get("resume.output_dir", "ml/resume/output")
    ledger = Ledger(cap_usd=float(cfg.get("resume.max_spend_per_run_usd", 0) or 0))
    run_cost = RunCost(cap=ledger.cap_usd)
    outcomes: list[JobOutcome] = []

    with get_session() as session:
        ids = [j.id for j in _tailorable_jobs(cfg, job_ids, limit, include_closed)]
        jobs = session.query(Job).filter(Job.id.in_(ids)).all() if ids else []
        jobs.sort(key=lambda j: j.ats_match_score or 0, reverse=True)

        for job in jobs:
            filename = output_filename(job.company, job.title, job.external_id)
            target = out_dir / filename

            # A job asked for by id is a person pressing "write it" -- they
            # want it written again, not told it exists. Only a batch skips.
            if (target.exists() and not job_ids and not cfg.get("resume.overwrite", False)
                    and not over_page_limit(cfg, target)):
                outcomes.append(
                    JobOutcome(job.id, job.company, job.title, job.ats_match_score or 0.0,
                               "skipped", "already tailored", target)
                )
                continue

            if job.ats_match_score is not None and job.ats_match_score < threshold and not job_ids:
                outcomes.append(
                    JobOutcome(job.id, job.company, job.title, job.ats_match_score,
                               "below-threshold", "flagged for manual review")
                )
                continue

            # Check the cap before spending, not after.
            projected = estimate_call(
                MODEL, build_prompt(resume, job.title, job.company, job.description or ""),
                cfg=cfg,
            )
            if ledger.would_exceed(projected):
                run_cost.stopped_early = True
                outcomes.append(
                    JobOutcome(job.id, job.company, job.title, job.ats_match_score or 0.0,
                               "cap-reached",
                               f"would cost ~${projected:.3f}, only "
                               f"${ledger.remaining:.3f} left of the "
                               f"${ledger.cap_usd:.2f} cap")
                )
                break

            from ml.resume.ats import compare as ats_compare
            frame = ats_compare(resume.text(), None, job.description or job.title,
                                job.requirements, job.company)["concepts"] or None
            focus = None
            if align:
                from ml.resume.ats import check
                focus = check(cfg, job, resume.text())["fixable"] or None
                log.info("Aligning %s — %s: bringing back %s", job.company, job.title,
                         ", ".join(focus or ["nothing (none missing)"]))
            try:
                result = tailor_resume(
                    resume=resume,
                    job_title=job.title,
                    company=job.company,
                    jd_text=job.description or "",
                    ledger=ledger,
                    cfg=cfg,
                    focus_terms=focus,
                    frame_terms=frame,
                )
            except Exception as exc:
                log.error("Tailoring failed for job %s: %s", job.id, exc)
                outcomes.append(
                    JobOutcome(job.id, job.company, job.title, job.ats_match_score or 0.0,
                               "error", str(exc))
                )
                continue

            note_path = out_dir / (target.stem + "_review.txt")
            previous = _snapshot(target)
            _fit(cfg, resume, result, job, focus)
            write_review_note(result, note_path)
            _note_framing(note_path, result, frame)

            if not result.accepted:
                if previous["after"] is not None:
                    # A rewrite of a resume that already exists: the guard
                    # turned the new one down, so the good one stays -- review
                    # note included, which the lines above just overwrote.
                    _restore(previous)
                    outcomes.append(JobOutcome(
                        job.id, job.company, job.title, job.ats_match_score or 0.0,
                        "kept-previous", "the rewrite claimed something your resume does not "
                        "show, so the previous version was kept", target))
                    continue
                job.status = "Manual Review"
                outcomes.append(
                    JobOutcome(job.id, job.company, job.title, job.ats_match_score or 0.0,
                               "rejected", result.guard.report(), None)
                )
                continue

            _write_both(resume, result, target)
            from ml.resume.ats import check, summary
            ats = check(cfg, job, resume.text())
            # A rewrite is kept only if it matches the posting at least as
            # well. Model output varies run to run -- one Netflix rewrite
            # framed a new term and lost two others, 77% -> 74%.
            if previous["after"] is not None and (ats["after"] or 0) < previous["after"]:
                _restore(previous)
                log.info("Kept the previous version: it matches the posting %s%%, this "
                         "rewrite only %s%%", previous["after"], ats["after"])
                outcomes.append(JobOutcome(
                    job.id, job.company, job.title, job.ats_match_score or 0.0, "kept-previous",
                    f"kept the previous resume ({previous['after']}% ATS match) — this "
                    f"rewrite matched {ats['after']}%", target))
                continue
            log.info(summary(ats))
            with note_path.open("a", encoding="utf-8") as note:
                note.write("\n" + summary(ats) + "\n")
                if ats["fixable"]:
                    note.write("  In your resume, not in this version: " + ", ".join(ats["fixable"]) + "\n")
                if ats["gaps"]:
                    note.write("  Not in your resume at all: " + ", ".join(ats["gaps"]) + "\n")

            # Re-score against the tailored text so the sheet reflects reality.
            rescored = score_resume(
                resume.text() + "\n" + result.tailored_text(),
                job.description or job.title,
                requirements=job.requirements,
                company=job.company,
            )
            job.ats_match_score = rescored.score
            job.exported_to_excel = False

            outcomes.append(
                JobOutcome(job.id, job.company, job.title, rescored.score, "tailored",
                           f"{len(result.bullets)} bullets rewritten; "
                           f"{len(result.gaps)} gap(s) noted", target)
            )

    run_cost.spent = ledger.spent
    run_cost.calls = len(ledger.calls)
    tailor_jobs.last_run_cost = run_cost
    log.info("Tailoring run: %s", ledger.summary())
    return outcomes


# -- cover letters ----------------------------------------------------------

def letter_brief(cfg, job_id: int, out_dir: Path | None = None) -> tuple[Path, str, str]:
    """Write a paste-anywhere cover-letter prompt. Mirrors brief_job."""
    from ml.resume.letter import SYSTEM_PROMPT, build_prompt

    resume = load_base_resume(cfg)
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise LookupError(f"No job with id {job_id}.")
        company, title = job.company, job.title
        jd_text = job.description or job.title
        url = job.application_url

    out_dir = out_dir or PROJECT_ROOT / cfg.get("resume.output_dir", "ml/resume/output")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (output_filename(company, title, "")[:-5] + "_letter_brief.txt")

    path.write_text(
        f"COVER LETTER BRIEF — {company}: {title}\n"
        f"{'=' * 70}\n\n"
        f"  1. Copy everything below COPY FROM HERE\n"
        f"  2. Paste it into any model\n"
        f"  3. Save the JSON reply to a file\n"
        f"  4. python main.py accept {job_id} --file <that file> --letter\n\n"
        f"The reply is checked for fabrication before anything is written: it\n"
        f"may not claim a skill, number or date your resume does not support,\n"
        f"even one this posting asks for.\n\n"
        f"Job    : {title}\nCompany: {company}\nApply  : {url or '(none)'}\n\n"
        f"{'-' * 70}\nCOPY FROM HERE\n{'-' * 70}\n\n"
        f"{SYSTEM_PROMPT}\n\n---\n\n"
        f"{build_prompt(resume, title, company, jd_text)}",
        encoding="utf-8",
    )
    return path, company, title


def write_letter(cfg, job_id: int) -> JobOutcome:
    """Have the chosen model write the letter, then guard it like a pasted one.

    The same prompt the copy button hands over and the same checks after, so
    one click and four steps cannot produce different letters.
    """
    from ml.resume.letter import SYSTEM_PROMPT, build_prompt, result_from_reply
    from ml.resume.llm import complete

    resume = load_base_resume(cfg)
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise LookupError(f"No job with id {job_id}.")
        jd_text = job.description or job.title
        prompt = build_prompt(resume, job.title, job.company, jd_text)
    reply = complete(SYSTEM_PROMPT, prompt, cfg, max_tokens=4000).text

    # Once more, naming what was rejected -- as tailoring does. Claude wrote
    # "Andhra Pradesh" for a resume that says "AP": true, and still a name the
    # resume does not contain. Told which words, a model drops them.
    first = result_from_reply(resume, jd_text, reply)
    if not first.accepted and first.guard is not None and first.guard.violations:
        named = list(dict.fromkeys(v.value for v in first.guard.violations))
        log.info("Letter for job %s: retrying without %s", job_id, ", ".join(named))
        retry = complete(SYSTEM_PROMPT, prompt + (
            "\n\n# Your previous letter was rejected\n"
            "It used these words, and the resume above does not contain them:\n"
            + "\n".join(f"  - {w}" for w in named)
            + "\nWrite it again without them. Use only names as the resume writes them."),
            cfg, max_tokens=4000).text
        if result_from_reply(resume, jd_text, retry).accepted:
            reply = retry
    return accept_letter(cfg, job_id, reply)


def accept_letter(cfg, job_id: int, reply_text: str) -> JobOutcome:
    """Guard a pasted cover-letter reply and, if clean, write the packet."""
    from ml.resume.letter import result_from_reply
    from ml.resume.packet import build_packet

    resume = load_base_resume(cfg)
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise LookupError(f"No job with id {job_id}.")

        result = result_from_reply(resume, job.description or job.title, reply_text)

        # The folder and its job summary are written either way: a rejected
        # letter still leaves something useful, and the review explains why.
        packet = build_packet(
            cfg, job, letter_text=result.text() if result.accepted else None
        )
        (packet.path / "letter-review.txt").write_text(
            _letter_review(result), encoding="utf-8"
        )

        if not result.accepted:
            job.status = "Manual Review"
            return JobOutcome(
                job_id=job.id, company=job.company, title=job.title,
                score=job.ats_match_score or 0.0, status="rejected",
                detail=result.guard.report() if result.guard else "no guard ran",
            )
        return JobOutcome(
            job_id=job.id, company=job.company, title=job.title,
            score=job.ats_match_score or 0.0, status="letter",
            detail=f"{result.word_count()} words", resume_path=packet.path,
        )


def _letter_review(result) -> str:
    lines = [f"COVER LETTER REVIEW — {utcnow():%Y-%m-%d %H:%M} UTC", "=" * 60, ""]
    if result.why_this_company:
        lines += ["WHY THIS COMPANY", f"  {result.why_this_company}", ""]
    if result.left_out:
        lines += ["LEFT OUT — the posting wants these, the resume does not support them"]
        lines += [f"  - {item}" for item in result.left_out] + [""]
    if result.guard and not result.guard.ok:
        lines += ["FABRICATION GUARD — REJECTED:", result.guard.report(), "",
                  "Nothing was written. Fix the reply or re-run the brief."]
    else:
        lines += [f"Guard passed. {result.word_count()} words."]
    return "\n".join(lines) + "\n"
