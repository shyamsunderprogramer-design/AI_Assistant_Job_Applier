"""What the assistant may look at and propose -- the whole of its reach.

The assistant is Claude Code, run by the web app's chat (or opened in this
folder) with every built-in tool switched off and only these available. So
this file is the boundary: nothing here runs a command, reads an arbitrary
path, or touches the network.

**Ask** tools read the job database and the status of the runs.

**Action** tools do not act. Each records a pending action and says what it
will do; the person confirms it -- the Confirm button in the chat, or
`confirm_action` from a terminal session -- and only then does the web app run
it, through the same code as its own buttons. A model that misreads a request
can at worst propose the wrong thing, in plain words, next to a Cancel button.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

STATE_DIR = PROJECT_ROOT / "data" / "assistant"
PENDING_FILE = STATE_DIR / "pending.json"
APPLICATIONS_LOG = PROJECT_ROOT / "data" / "applications.jsonl"
DESCRIPTION_CHARS = 2500
_lock = threading.Lock()


def _session():
    from data_engineering.db import session as db
    from backend.config.loader import load_config

    if db._SessionFactory is None:          # the MCP server is its own process
        db.init_engine(load_config().database_url)
    return db.get_session()


def _row(job) -> dict:
    from backend.core.jobage import age_label

    return {"id": job.id, "company": job.company, "title": job.title,
            "location": job.location or "", "board": job.source,
            "match": None if job.ats_match_score is None else round(job.ats_match_score * 100),
            "age": age_label(job) or "", "status": job.status, "open": bool(job.is_open)}


# -- ask ------------------------------------------------------------------------

def search_jobs(text: str = "", board: str = "", min_match: int = 0, posted_within_days: int = 0,
                status: str = "", open_only: bool = True, limit: int = 15) -> dict:
    """Jobs matching every filter given, best match first."""
    from data_engineering.db.models import Job

    limit = max(1, min(int(limit or 15), 50))
    with _session() as session:
        query = session.query(Job)
        if open_only:
            query = query.filter(Job.is_open.is_(True))
        if board:
            query = query.filter(Job.source == board.strip().lower())
        if status:
            query = query.filter(Job.status == status.strip())
        if min_match:
            query = query.filter(Job.ats_match_score >= float(min_match) / 100)
        if posted_within_days:
            since = datetime.now(timezone.utc) - timedelta(days=int(posted_within_days))
            query = query.filter(((Job.posted_at.isnot(None)) & (Job.posted_at >= since.replace(tzinfo=None)))
                                 | ((Job.posted_at.is_(None)) & (Job.found_at >= since.replace(tzinfo=None))))
        for word in re.findall(r"[\w+#.-]+", text or "")[:6]:
            like = f"%{word}%"
            query = query.filter(Job.title.ilike(like) | Job.company.ilike(like) | Job.location.ilike(like))
        total = query.count()
        jobs = query.order_by(Job.ats_match_score.is_(None), Job.ats_match_score.desc()).limit(limit).all()
        return {"total": total, "shown": len(jobs), "jobs": [_row(j) for j in jobs]}


def job_detail(job_id: int) -> dict:
    """One job: the facts, the start of its description, and what exists for it."""
    from data_engineering.db.models import Job

    with _session() as session:
        job = session.get(Job, int(job_id))
        if job is None:
            return {"error": f"No job {job_id}."}
        out = _row(job)
        out.update({
            "url": job.application_url, "workplace": job.workplace or "",
            "salary": _salary(job), "experience_years": _years(job),
            "description": (job.description or "")[:DESCRIPTION_CHARS],
            "description_truncated": len(job.description or "") > DESCRIPTION_CHARS,
        })
        company, title, external = job.company, job.title, job.external_id or ""
    out["documents"] = _documents(company, title, external)
    return out


def tailoring_review(job_id: int) -> dict:
    """The review note of this job's tailored resume: gaps, rewrites, what was cut."""
    from data_engineering.db.models import Job
    from ml.resume.packet import packet_root
    from ml.resume.writer import output_filename
    from backend.config.loader import load_config

    with _session() as session:
        job = session.get(Job, int(job_id))
        if job is None:
            return {"error": f"No job {job_id}."}
        stem = output_filename(job.company, job.title, job.external_id or "")[:-5]
    note = packet_root(load_config()) / f"{stem}_review.txt"
    if not note.exists():
        return {"exists": False, "note": "No tailored resume for this job yet."}
    return {"exists": True, "review": note.read_text(encoding="utf-8")[:12000]}


def pipeline_status() -> dict:
    """Open jobs, scores, today's applications, and every long run's progress."""
    from backend.config.loader import load_config
    from backend.core.status import collect, render
    from data_engineering.db.models import Job

    with _session() as session:
        by_status = {}
        for (value,) in session.query(Job.status).filter(Job.is_open.is_(True)):
            by_status[value] = by_status.get(value, 0) + 1
    try:
        runs = render(collect(load_config()))
    except Exception as exc:
        runs = f"(status unavailable: {type(exc).__name__})"
    return {"open_jobs_by_status": by_status, "applied_today": len(_applications(since_hours=24)),
            "runs": runs}


def recent_applications(limit: int = 10) -> dict:
    """The applications recorded as sent, newest first."""
    rows = _applications()
    rows.reverse()
    keep = ("at", "job_id", "company", "title", "how", "resume_source")
    return {"total": len(rows), "applications": [{k: r.get(k) for k in keep} for r in rows[:int(limit or 10)]]}


# -- propose -----------------------------------------------------------------------

def propose_tailor(job_id: int) -> dict:
    """Propose writing a resume tailored to one job."""
    job = _brief(job_id)
    if "error" in job:
        return job
    return _propose("tailor", {"job_id": job["id"]},
                    f"Tailor your resume for {job['company']} — {job['title']} (job {job['id']})")


def propose_apply(job_id: int = 0, count: int = 0) -> dict:
    """Propose an apply run: one job, or the next `count` from the queue."""
    if job_id:
        job = _brief(job_id)
        if "error" in job:
            return job
        return _propose("apply", {"job_id": job["id"]},
                        f"Open the application for {job['company']} — {job['title']} (job {job['id']}) "
                        f"and fill it; you sign in where asked and press Submit yourself")
    count = max(1, min(int(count or 1), 10))
    return _propose("apply", {"count": count},
                    f"Apply to the next {count} job(s) in your queue, freshest and best match first; "
                    f"you sign in where asked and press Submit on each")


def propose_remember_answer(question: str, answer: str) -> dict:
    """Propose saving an answer to a screening question for future forms."""
    from backend.apply.answers import normalise

    question, answer = normalise(question), (answer or "").strip()
    if not question or not answer:
        return {"error": "Both a question and an answer are needed."}
    if len(answer) > 500:
        return {"error": "That answer is too long to reuse on a form."}
    return _propose("remember", {"question": question, "answer": answer},
                    f"Remember for future forms — “{question}”: “{answer}”")


def propose_set_status(job_id: int, status: str) -> dict:
    """Propose changing a job's status (e.g. Not Interested, Interviewing)."""
    from data_engineering.db.models import STATUS_VALUES

    if status not in STATUS_VALUES:
        return {"error": f"{status!r} is not a status. Use one of: {', '.join(STATUS_VALUES)}"}
    job = _brief(job_id)
    if "error" in job:
        return job
    return _propose("status", {"job_id": job["id"], "status": status},
                    f"Mark {job['company']} — {job['title']} (job {job['id']}) as {status}")


def pending_actions() -> list[dict]:
    return [a for a in _load_pending() if a["state"] == "pending"]


# -- the confirm side (the web app calls these) -------------------------------------

def take(action_id: str, state: str) -> dict | None:
    """Mark a pending action confirmed or cancelled, once. None if not pending."""
    with _lock:
        actions = _load_pending()
        for action in actions:
            if action["id"] == action_id and action["state"] == "pending":
                action["state"], action["decided_at"] = state, time.time()
                _save_pending(actions)
                return action
    return None


def reopen(action_id: str) -> None:
    """Back to pending: it was confirmed but could not start."""
    with _lock:
        actions = _load_pending()
        for action in actions:
            if action["id"] == action_id:
                action["state"] = "pending"
        _save_pending(actions)


def run_direct(action: dict) -> str:
    """Carry out the actions that need no background run. Returns what happened."""
    if action["kind"] == "remember":
        from backend.apply.answers import BANK_PATH, load
        import yaml

        bank = load()
        bank[action["params"]["question"]] = action["params"]["answer"]
        BANK_PATH.parent.mkdir(parents=True, exist_ok=True)
        BANK_PATH.write_text(
            "# Your own answers to screening questions, learned from forms you\n"
            "# finished yourself. Edit or delete any line; it is reused exactly.\n"
            + yaml.safe_dump(dict(sorted(bank.items())), sort_keys=False, allow_unicode=True),
            encoding="utf-8")
        return "Saved — it will be used on the next form that asks."
    if action["kind"] == "status":
        from data_engineering.db.models import Job

        with _session() as session:
            job = session.get(Job, action["params"]["job_id"])
            if job is None:
                return "That job no longer exists."
            job.status = action["params"]["status"]
            job.exported_to_excel = False
        return f"Marked {action['params']['status']}."
    raise ValueError(action["kind"])


# -- helpers --------------------------------------------------------------------------

def _brief(job_id) -> dict:
    from data_engineering.db.models import Job

    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return {"error": f"{job_id!r} is not a job id."}
    with _session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return {"error": f"No job {job_id}."}
        return {"id": job.id, "company": job.company, "title": job.title}


def _propose(kind: str, params: dict, summary: str) -> dict:
    action = {"id": uuid.uuid4().hex[:10], "kind": kind, "params": params, "summary": summary,
              "state": "pending", "created_at": time.time()}
    with _lock:
        actions = [a for a in _load_pending() if time.time() - a.get("created_at", 0) < 7 * 86400]
        actions.append(action)
        _save_pending(actions)
    return {"pending_action": action["id"], "will_do": summary,
            "next": "Nothing has happened yet. The person confirms or cancels it."}


def _load_pending() -> list[dict]:
    try:
        return json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_pending(actions: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PENDING_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(actions, indent=1), encoding="utf-8")
    tmp.replace(PENDING_FILE)


def _applications(since_hours: float | None = None) -> list[dict]:
    try:
        lines = APPLICATIONS_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours) if since_hours else None
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if cutoff is not None:
            try:
                if datetime.fromisoformat(row["at"]) < cutoff:
                    continue
            except (KeyError, ValueError):
                continue
        rows.append(row)
    return rows


def _salary(job) -> str:
    if not (job.salary_min or job.salary_max):
        return ""
    low, high = job.salary_min or job.salary_max, job.salary_max or job.salary_min
    return f"{job.salary_currency or ''} {low:,}–{high:,} per {job.salary_period or 'year'}".strip()


def _years(job) -> str:
    if job.experience_min_years is None:
        return ""
    if job.experience_max_years and job.experience_max_years != job.experience_min_years:
        return f"{job.experience_min_years}–{job.experience_max_years}"
    return f"{job.experience_min_years}+"


def _documents(company: str, title: str, external: str) -> dict:
    from ml.resume.packet import packet_root
    from ml.resume.writer import output_filename
    from backend.config.loader import load_config

    stem = output_filename(company, title, external)[:-5]
    root = packet_root(load_config())
    return {"tailored_resume": (root / f"{stem}.pdf").exists(),
            "review_note": (root / f"{stem}_review.txt").exists(),
            "selected_resume": (root / f"{stem}_selected.pdf").exists()}
