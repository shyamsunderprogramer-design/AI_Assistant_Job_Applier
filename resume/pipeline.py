"""Phase 3 orchestration: score → tailor → guard → write → record.

Scoring runs without an API key, so `score-only` mode is useful on its own:
it ranks the whole backlog by fit before any money is spent on tailoring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from config.loader import PROJECT_ROOT
from db.models import Job
from db.session import get_session
from resume.parser import Resume, find_base_resume, parse_resume
from resume.scorer import score_resume
from resume.writer import output_filename, write_review_note, write_tailored_resume

log = logging.getLogger(__name__)


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


def load_base_resume(cfg) -> Resume:
    configured = cfg.get("resume.base_path")
    path = Path(configured) if configured else None
    if path and not path.is_absolute():
        path = PROJECT_ROOT / path
    if path is None or not path.exists():
        found = find_base_resume(PROJECT_ROOT / "resume")
        if found is None:
            raise FileNotFoundError(
                "No base resume found. Put your .docx or .pdf in resume/ "
                "or set resume.base_path in config.yaml."
            )
        path = found
    log.info("Base resume: %s", path)
    return parse_resume(path)


def score_jobs(
    cfg, limit: int = 0, rescore: bool = False, include_closed: bool = False
) -> list[JobOutcome]:
    """Score every open job against the base resume. No API calls, no cost.

    Closed postings are skipped: ranking a filled role wastes the user's
    attention, which is the whole point of the ranking.
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
        if limit:
            jobs = jobs[:limit]

        for job in jobs:
            result = score_resume(
                resume_text,
                job.description or job.title,
                requirements=job.requirements,
                company=job.company,
            )
            job.ats_match_score = result.score
            job.exported_to_excel = False  # push the new score to Excel

            # The flag must be symmetric: a job that clears the threshold on a
            # later run (better resume, retuned threshold) has to lose a stale
            # "Manual Review", or the sheet accumulates flags that never clear.
            # Only the two system-owned states are touched — anything the user
            # set (Applied, Rejected, ...) is left alone.
            below = result.score < threshold
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
                )
            )

    return outcomes


def estimate_tailoring(cfg, job_ids: list[int] | None = None, limit: int = 0) -> dict:
    """What a tailoring run would cost, without making a single API call.

    Measures the real prompts rather than guessing at their size, so the number
    is checkable against the ledger afterwards.
    """
    from resume.cost import estimate_call
    from resume.tailor import MODEL, build_prompt

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


def tailor_jobs(
    cfg,
    job_ids: list[int] | None = None,
    limit: int = 0,
    include_closed: bool = False,
) -> list[JobOutcome]:
    """Tailor the resume for open jobs at or above the score threshold.

    Closed postings are skipped by default — a tailoring call is the most
    expensive thing here, and spending one on a filled role buys nothing.
    An explicit --job-id still wins, so a deliberate choice is never blocked.

    Every call is costed and checked against `resume.max_spend_per_run_usd`
    BEFORE it is made. A cap discovered by exceeding it is not a cap.
    """
    from resume.cost import Ledger, estimate_call
    from resume.tailor import MODEL, build_prompt, tailor_resume  # lazy: needs SDK + key

    resume = load_base_resume(cfg)
    threshold = float(cfg.get("resume.min_score", 0.45))
    out_dir = PROJECT_ROOT / cfg.get("resume.output_dir", "resume/output")
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

            if target.exists() and not cfg.get("resume.overwrite", False):
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

            try:
                result = tailor_resume(
                    resume=resume,
                    job_title=job.title,
                    company=job.company,
                    jd_text=job.description or "",
                    ledger=ledger,
                    cfg=cfg,
                )
            except Exception as exc:
                log.error("Tailoring failed for job %s: %s", job.id, exc)
                outcomes.append(
                    JobOutcome(job.id, job.company, job.title, job.ats_match_score or 0.0,
                               "error", str(exc))
                )
                continue

            note_path = out_dir / (target.stem + "_review.txt")
            write_review_note(result, note_path)

            if not result.accepted:
                job.status = "Manual Review"
                outcomes.append(
                    JobOutcome(job.id, job.company, job.title, job.ats_match_score or 0.0,
                               "rejected", result.guard.report(), None)
                )
                continue

            write_tailored_resume(resume, result, target)

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
