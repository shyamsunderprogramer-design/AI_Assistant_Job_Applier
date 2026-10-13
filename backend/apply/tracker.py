"""Every application, and what has happened to it since: the posting, the day it went, what was sent, its stages.

An application is a job whose status says the person applied (Applied,
Interviewing, Offer, Rejected) or that has an applied date. What went out
comes from the application record the runner keeps (data/applications.jsonl):
the resume, the letter, every answer the form was given, the screenshots. A
job the person marked Applied by hand has no record, and says so.

Stages are the person's: heard back, a recruiter screen, interviews, an offer,
a rejection, withdrawn, no response -- each with a date and a note, kept in
data/apply/stages.json. The job's status follows the latest stage that changes
it (an interview -> Interviewing, an offer -> Offer, a rejection -> Rejected),
so the Jobs page agrees with this one.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

STAGES_FILE = PROJECT_ROOT / "data" / "apply" / "stages.json"
RUNS_DIR = PROJECT_ROOT / "backend" / "apply" / "runs"
APPLIED_STATUSES = ("Applied", "Interviewing", "Offer", "Rejected")

# key: (label, the job status it sets, or None to leave the status alone)
STAGES = {
    "heard_back": ("Heard back", None),
    "recruiter_screen": ("Recruiter screen", "Interviewing"),
    "technical": ("Technical interview", "Interviewing"),
    "final": ("Final interview", "Interviewing"),
    "offer": ("Offer", "Offer"),
    "rejected": ("Rejected", "Rejected"),
    "withdrew": ("Withdrew", None),
    "no_response": ("No response", None),
}
FOLLOW_UP_DAYS = 7
EVIDENCE_FILES = ("resume.pdf", "resume.docx", "form.png", "confirmation.png", "cover-letter.pdf",
                  "cover-letter.txt", "confirmation.txt")


def _audit() -> dict[int, dict]:
    """The latest application record per job."""
    from backend.apply.runner import AUDIT_LOG
    out: dict[int, dict] = {}
    try:
        lines = AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry.get("job_id"), int):
            out[entry["job_id"]] = entry
    return out


def _stages() -> dict:
    try:
        return json.loads(STAGES_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_stages(data: dict) -> None:
    STAGES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STAGES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(STAGES_FILE)


def _when(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _current(job_status: str, events: list[dict]) -> str:
    """What to call where it stands: the latest stage, else the job's status."""
    if events:
        return STAGES.get(events[-1]["stage"], (events[-1]["stage"], None))[0]
    return job_status or "Applied"


def applications() -> list[dict]:
    """Every application, newest first, with where each stands."""
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session
    audit, stages = _audit(), _stages()
    out = []
    with get_session() as session:
        rows = session.query(Job).filter((Job.status.in_(APPLIED_STATUSES)) | (Job.applied_at.isnot(None))).all()
        for job in rows:
            record = audit.get(job.id)
            applied = _when(job.applied_at) or _when((record or {}).get("at"))
            events = sorted(stages.get(str(job.id), []), key=lambda e: e["at"])
            days = (date.today() - applied.date()).days if applied else None
            out.append({"job_id": job.id, "company": job.company, "title": job.title, "status": job.status,
                        "applied_at": applied.isoformat() if applied else None, "days": days,
                        "stage": _current(job.status, events), "stages": len(events),
                        "through_app": record is not None, "open": bool(job.is_open)})
    out.sort(key=lambda a: a["applied_at"] or "", reverse=True)
    return out


def application(job_id: int) -> dict | None:
    """One application in full: the posting, the day, what went, and its stages."""
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session
    record = _audit().get(job_id)
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return None
        applied = _when(job.applied_at) or _when((record or {}).get("at"))
        out = {"job_id": job.id, "company": job.company, "title": job.title, "location": job.location,
               "workplace": job.workplace, "status": job.status, "open": bool(job.is_open),
               "salary": [job.salary_min, job.salary_max] if job.salary_min or job.salary_max else None,
               "posting_url": (record or {}).get("url") or job.application_url, "source": job.source,
               "description": job.description or "", "applied_at": applied.isoformat() if applied else None}
    events = sorted(_stages().get(str(job_id), []), key=lambda e: e["at"])
    out["stages"] = [{**e, "label": STAGES.get(e["stage"], (e["stage"], None))[0]} for e in events]
    out["current"] = _current(out["status"], events)
    out["days"] = (date.today() - applied.date()).days if applied else None
    out["record"] = None
    if record:
        folder = Path(record.get("evidence") or "")
        files = [f for f in EVIDENCE_FILES if folder.is_dir() and (folder / f).exists()]
        filled = record.get("filled") or {}
        out["record"] = {"how": record.get("how"), "resume_source": record.get("resume_source"),
                         "cover_letter": bool(record.get("cover_letter")),
                         "answers": filled if isinstance(filled, dict) else {k: "" for k in filled},
                         "files": files}
    last = events[-1]["stage"] if events else None
    out["follow_up"] = bool(out["days"] is not None and out["days"] >= FOLLOW_UP_DAYS
                            and last in (None, "no_response") and out["status"] == "Applied")
    return out


def evidence_file(job_id: int, name: str) -> Path | None:
    """A file kept from the application, only from its own evidence folder."""
    if name not in EVIDENCE_FILES:
        return None
    record = _audit().get(job_id)
    if not record:
        return None
    folder = Path(record.get("evidence") or "").resolve()
    if RUNS_DIR.resolve() not in folder.parents:
        return None
    path = folder / name
    return path if path.exists() else None


def add_stage(job_id: int, stage: str, on: str | None = None, note: str = "") -> dict:
    """Record what happened next; the job's status follows when the stage changes it."""
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session
    if stage not in STAGES:
        raise ValueError("Choose what happened.")
    try:
        day = date.fromisoformat(on) if on else date.today()
    except ValueError:
        raise ValueError("That date could not be read.") from None
    if day > date.today():
        raise ValueError("That date is in the future.")
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise ValueError("No such job.")
        applied = _when(job.applied_at)
        if applied and day < applied.date():
            raise ValueError("That is before you applied.")
        data = _stages()
        events = data.setdefault(str(job_id), [])
        events.append({"stage": stage, "at": day.isoformat(), "note": " ".join(str(note or "").split())[:500],
                       "added": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        events.sort(key=lambda e: (e["at"], e.get("added", "")))
        _save_stages(data)
        _status_from(job, events)
    return application(job_id)


def remove_stage(job_id: int, index: int) -> dict:
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session
    data = _stages()
    events = sorted(data.get(str(job_id), []), key=lambda e: (e["at"], e.get("added", "")))
    if not 0 <= index < len(events):
        raise ValueError("No such stage.")
    events.pop(index)
    data[str(job_id)] = events
    _save_stages(data)
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is not None:
            _status_from(job, events)
    return application(job_id)


def _status_from(job, events: list[dict]) -> None:
    """The job's status, from the latest stage that sets one; Applied when none does."""
    status = "Applied"
    for e in events:
        sets = STAGES.get(e["stage"], (None, None))[1]
        if sets:
            status = sets
    job.status = status
    if job.applied_at is None:
        job.applied_at = datetime.now(timezone.utc)
