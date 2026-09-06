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


def score_jobs(cfg, limit: int = 0, rescore: bool = False) -> list[JobOutcome]:
    """Score every job against the base resume. No API calls, no cost."""
    resume = load_base_resume(cfg)
    resume_text = resume.text()
    threshold = float(cfg.get("resume.min_score", 0.45))
    outcomes: list[JobOutcome] = []

    with get_session() as session:
        query = session.query(Job)
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


def tailor_jobs(cfg, job_ids: list[int] | None = None, limit: int = 0) -> list[JobOutcome]:
    """Tailor the resume for jobs at or above the score threshold."""
    from resume.tailor import tailor_resume  # lazy: needs the anthropic SDK + key

    resume = load_base_resume(cfg)
    threshold = float(cfg.get("resume.min_score", 0.45))
    out_dir = PROJECT_ROOT / cfg.get("resume.output_dir", "resume/output")
    outcomes: list[JobOutcome] = []

    with get_session() as session:
        query = session.query(Job)
        if job_ids:
            query = query.filter(Job.id.in_(job_ids))
        else:
            query = query.filter(Job.ats_match_score >= threshold)
        jobs = query.order_by(Job.ats_match_score.desc()).all()
        if limit:
            jobs = jobs[:limit]

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

            try:
                result = tailor_resume(
                    resume=resume,
                    job_title=job.title,
                    company=job.company,
                    jd_text=job.description or "",
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

    return outcomes
