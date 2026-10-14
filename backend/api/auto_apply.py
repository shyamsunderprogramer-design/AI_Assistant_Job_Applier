"""Auto-apply from a job's own page: the app's browser applies to this one posting, with the person.

Any board will do. Greenhouse, Workday and JazzHR forms have fillers of their own; every other
posting is followed to the employer's form and filled from the profile (backend/apply/anyform.py).
The page starts the run, shows what it says while it goes, and, when a site answered Submit in
words the app does not know, asks the person whether it went.
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import jsonify, request

from backend.api.app import PROJECT_ROOT, app, applicant_path, start_task, task_exit_code, task_log, task_pid
from data_engineering.db.models import Job
from data_engineering.db.session import get_session

# Which job the app's apply run is on, so a job's page shows its own run and nobody else's.
MARKER = PROJECT_ROOT / "data" / "webapp_apply.job"
APPLY_TASKS = ("apply", "apply-ready")      # one browser profile: one apply run at a time
APPLIED = ("Applied", "Interviewing", "Offer", "Rejected")
STARTABLE = ("Not Applied", "Manual Review", "Skipped")


def _marked() -> int | None:
    try:
        return int(MARKER.read_text().strip())
    except (OSError, ValueError):
        return None


def _refuse(message: str, code: int = 409):
    return jsonify(ok=False, error=message), code


@app.post("/job/<int:job_id>/auto-apply")
def auto_apply_start(job_id: int):
    from backend.apply import anyform
    from backend.apply.profile import ProfileIncomplete, check_ready, load
    from backend.core.eligibility import eligible

    try:
        applicant = load(applicant_path())
    except ProfileIncomplete:
        return _refuse("Fill in your details first, in Settings → Applicant: the form is filled from them.")
    problems = check_ready(applicant)
    if problems:
        return _refuse("Your applicant details are missing something the forms ask for: "
                       + "; ".join(problems[:3]) + ". Fix it in Settings → Applicant.")
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return _refuse("No such job.", 404)
        if job.status in APPLIED:
            return _refuse(f"It's marked {job.status} already. See it on the Applied page.")
        if job.status not in STARTABLE or not job.is_open:
            return _refuse("This posting has closed.")
        if not (job.application_url or "").strip():
            return _refuse("This posting has no link to apply through.")
        ok, why = eligible(job, applicant.authorisation or {})
        if not ok:
            return _refuse(f"Your profile rules this one out: {', '.join(why)}. Change it in Settings → "
                           f"Applicant if that's wrong, or correct the posting's reading above.")
        busy = next((t for t in APPLY_TASKS if task_pid(t)), None)
        if busy:
            return _refuse("An Auto-apply run is going already, and its browser window is open. Finish it "
                           "or close that window, then press this again.")
        if job.status == "Skipped":
            job.status = "Not Applied"           # pressing Auto-apply is the person changing their mind
    anyform.forget_unconfirmed(job_id)           # a new attempt replaces the last one's question
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(str(job_id))
    # Half an hour per form: this is one posting the person chose, perhaps with an account to make.
    return start_task("apply", ["apply", "--job-id", str(job_id), "--wait", "30"])


@app.get("/job/<int:job_id>/auto-apply")
def auto_apply_state(job_id: int):
    """This job's run: going or not, what it last said, and whether the person must say if it went."""
    from backend.apply import anyform

    running = task_pid("apply") is not None
    mine = _marked() == job_id
    lines: list[str] = []
    if mine:
        try:
            lines = [ln for ln in task_log("apply").read_text(errors="replace").splitlines() if ln.strip()][-14:]
        except OSError:
            lines = []
    with get_session() as session:
        job = session.get(Job, job_id)
        status = job.status if job is not None else None
    ask = (not (running and mine)) and status in STARTABLE and anyform.unconfirmed(job_id) is not None
    return jsonify(ok=True, running=running and mine, other=running and not mine, lines=lines,
                   status=status, applied=status in APPLIED, ask=ask,
                   exit=task_exit_code("apply") if mine and not running else None)


@app.post("/job/<int:job_id>/auto-apply/went")
def auto_apply_went(job_id: int):
    """The person's word on a form the app filled but could not see sent."""
    from backend.apply import anyform
    from backend.apply.runner import record_application

    went = bool((request.get_json(silent=True) or {}).get("went"))
    entry = anyform.unconfirmed(job_id)
    if went:
        if entry:
            record_application(job_id, {k: v for k, v in entry.items() if k != "kept_at"})
        else:
            with get_session() as session:
                job = session.get(Job, job_id)
                if job is None:
                    return _refuse("No such job.", 404)
                job.status, job.applied_at = "Applied", job.applied_at or datetime.now(timezone.utc)
    anyform.forget_unconfirmed(job_id)
    return jsonify(ok=True, applied=went)
