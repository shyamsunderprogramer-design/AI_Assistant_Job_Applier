"""Auto-apply from a job's own page: the app's browser applies to this one posting, with the person.

Any board will do. Greenhouse, Workday and JazzHR forms have fillers of their own; every other
posting is followed to the employer's form and filled from the profile (backend/apply/anyform.py).
The page starts the run, shows what it says while it goes, and, when a site answered Submit in
words the app does not know, asks the person whether it went.
"""

from __future__ import annotations

from pathlib import Path

from flask import abort, jsonify, request, send_file

from backend.api.app import PROJECT_ROOT, app, applicant_path, start_task, task_exit_code, task_log, task_pid
from backend.core.joblinks import apply_link
from data_engineering.db.models import Job
from data_engineering.db.session import get_session

# Which job the app's apply run is on, so a job's page shows its own run and nobody else's.
MARKER = PROJECT_ROOT / "data" / "webapp_apply.job"
APPLY_TASKS = ("apply", "apply-ready", "apply-chosen")   # one browser profile: one apply run at a time
APPLIED = ("Applied", "Interviewing", "Offer", "Rejected")
STARTABLE = ("Not Applied", "Manual Review", "Skipped")
# What a run keeps that the person checks before saying whether it went.
RUN_FILES = {"resume.pdf": "application/pdf", "form.png": "image/png",
             "resume.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def _marked() -> int | None:
    try:
        return int(MARKER.read_text().strip())
    except (OSError, ValueError):
        return None


def _last_run(job_id: int):
    """This job's newest run folder: the resume it attached and the form as it was left."""
    from backend.apply.greenhouse import RUNS_DIR
    runs = sorted(p for p in RUNS_DIR.glob(f"{job_id}-*") if p.is_dir())
    return runs[-1] if runs else None


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
        if not apply_link(job).strip():
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
    entry = anyform.unconfirmed(job_id)
    ask = (not (running and mine)) and status in STARTABLE and entry is not None
    folder = _last_run(job_id)
    files = [n for n in RUN_FILES if folder is not None and (folder / n).exists()]
    return jsonify(ok=True, running=running and mine, other=running and not mine, lines=lines,
                   status=status, applied=status in APPLIED, ask=ask, files=files,
                   handoff=_handoff(entry) if ask else None,
                   exit=task_exit_code("apply") if mine and not running else None)


def _handoff(entry: dict | None) -> dict | None:
    """A form handed to the person's own browser: every answer, the files, what is left for them."""
    if not entry or not entry.get("handoff"):
        return None
    name = lambda key: Path(entry[key]).name if entry.get(key) else None   # noqa: E731
    return {"url": entry.get("url"), "resume": name("resume"), "letter": name("cover_letter"),
            "answers": [{"q": q, "a": a} for q, a in (entry.get("filled") or {}).items()
                        if isinstance(a, str) and a.strip()],
            "left": entry.get("left") or []}


def _handoff_file(job_id: int, which: str) -> Path | None:
    """The resume or cover letter a hand-off is to attach -- only from the app's own run folders."""
    from backend.apply import anyform
    from backend.apply.greenhouse import RUNS_DIR
    entry = anyform.unconfirmed(job_id) or {}
    raw = entry.get({"resume": "resume", "letter": "cover_letter"}.get(which, ""))
    if not entry.get("handoff") or not raw:
        return None
    path = Path(raw).resolve()
    return path if RUNS_DIR.resolve() in path.parents and path.is_file() else None


@app.get("/job/<int:job_id>/auto-apply/handoff/<which>")
def auto_apply_handoff_file(job_id: int, which: str):
    """The file to attach in the person's own browser, under the name the employer should see."""
    path = _handoff_file(job_id, which)
    if path is None:
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)


@app.post("/job/<int:job_id>/auto-apply/handoff/<which>/reveal")
def auto_apply_handoff_reveal(job_id: int, which: str):
    """Show the file in Finder, to drag onto the form."""
    import subprocess
    path = _handoff_file(job_id, which)
    if path is None:
        return _refuse("That file is not there any more.", 404)
    subprocess.run(["open", "-R", str(path)], capture_output=True, timeout=20)
    return jsonify(ok=True)


@app.get("/job/<int:job_id>/auto-apply/file/<name>")
def auto_apply_file(job_id: int, name: str):
    """The resume the last run attached, or its picture of the form -- only from that run's folder."""
    folder = _last_run(job_id)
    if name not in RUN_FILES or folder is None or not (folder / name).exists():
        abort(404)
    return send_file(folder / name, mimetype=RUN_FILES[name], as_attachment=name.endswith(".docx"))


@app.post("/job/<int:job_id>/auto-apply/went")
def auto_apply_went(job_id: int):
    """The person's word on a form the app filled but could not see sent. Taken once (25 Oct 2026):
    the page asks only while the form's entry is kept, and an answer takes it, so a second answer --
    another tab, a page left open, the Auto-apply page first -- is refused and changes nothing.
    (Until then a late "yes" with the entry gone marked the job Applied over the "no" before it.)"""
    from backend.apply import anyform
    from backend.apply.execution import QueueError, settled_elsewhere
    from backend.apply.runner import record_application

    went = bool((request.get_json(silent=True) or {}).get("went"))
    entry = anyform.take_unconfirmed(job_id)
    if entry is None:
        return _refuse("This was answered already, in another tab or on the Auto-apply page. "
                       "Reload the page to see where it stands.")
    try:
        # Its entry on the Auto-apply list, if one waits for this answer, is claimed with the job's record.
        listed = settled_elsewhere(job_id, went, entry)
    except QueueError as exc:
        return _refuse(str(exc))
    if listed is None and went:
        record_application(job_id, {k: v for k, v in entry.items() if k != "kept_at"})
    return jsonify(ok=True, applied=went)
