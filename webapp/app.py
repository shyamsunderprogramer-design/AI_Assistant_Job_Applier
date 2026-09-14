"""A local web app for the job search — the terminal, without the terminal.

Everything here already exists as a command; this only removes the need to
remember which one. It runs on this machine, against this machine's database
and this machine's resume, and it holds nobody else's data: one install is one
person, which is why there is no login and nothing to secure.

    python -m webapp            # then open http://127.0.0.1:8770

Paths come from config rather than being hardcoded, so a second person runs
their own copy rather than sharing yours.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

from config.loader import PROJECT_ROOT, load_config
from db.models import Job, STATUS_VALUES
from db.session import get_session, init_engine
from jobage import age_label
from jobfields import UNSTATED, experience_label, salary_label, workplace_label

log = logging.getLogger(__name__)

RESUME_SUFFIXES = (".docx", ".pdf", ".txt", ".md")
MAX_UPLOAD_MB = 10

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

_cfg = None


def cfg():
    global _cfg
    if _cfg is None:
        _cfg = load_config()
        init_engine(_cfg.database_url)
    return _cfg


def resume_dir() -> Path:
    configured = cfg().get("resume.base_path") or "resume"
    path = Path(configured)
    path = path if path.is_absolute() else PROJECT_ROOT / path
    return path if path.is_dir() else path.parent


def output_dir() -> Path:
    path = Path(cfg().get("resume.output_dir", "resume/output"))
    return path if path.is_absolute() else PROJECT_ROOT / path


def run_command(*args: str) -> tuple[bool, str]:
    """Run one main.py command and hand back what it printed.

    Shelling out rather than importing keeps one definition of what a command
    does: the web app cannot drift from the CLI because it IS the CLI.
    """
    result = subprocess.run(
        [sys.executable, "main.py", *args],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=900,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()


def job_row(job: Job) -> dict:
    """One posting, shaped for display. Unstated facts stay blank, never zero."""
    def stated(value: str) -> str:
        return "" if value == UNSTATED else value

    return {
        "id": job.id,
        "company": job.company,
        "title": job.title,
        "location": job.location or "",
        "url": job.application_url,
        "board": job.source,
        "score": round((job.ats_match_score or 0) * 100),
        "age": age_label(job),
        "workplace": stated(workplace_label(job.workplace)),
        "experience": stated(experience_label(job.experience_min_years,
                                              job.experience_max_years)),
        "salary": stated(salary_label(job.salary_min, job.salary_max,
                                      job.salary_currency, job.salary_period)),
        "status": job.status,
    }


# -- pages ------------------------------------------------------------------

@app.route("/")
def index():
    with get_session() as session:
        jobs = (
            session.query(Job).filter(Job.is_open.is_(True))
            .order_by(Job.ats_match_score.desc()).all()
        )
        rows = [job_row(j) for j in jobs]
        boards = session.query(Job.source).distinct().count()

    resume = current_resume()
    return render_template(
        "index.html",
        rows=rows,
        statuses=STATUS_VALUES,
        resume=resume.name if resume else None,
        resume_dir=str(resume_dir()),
        strong=sum(1 for r in rows if r["score"] >= 70),
        boards=boards,
    )


@app.route("/job/<int:job_id>")
def job_detail(job_id: int):
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return "No such job", 404
        row = job_row(job)
        description = job.description or ""
        requirements = job.requirements or ""

    folder = packet_folder(row)
    return render_template(
        "job.html",
        job=row, description=description, requirements=requirements,
        statuses=STATUS_VALUES,
        packet=folder and {
            "path": str(folder),
            "has_resume": (folder / "resume.docx").exists(),
            "has_letter": (folder / "cover-letter.md").exists(),
        },
    )


def packet_folder(row: dict) -> Path | None:
    from resume.writer import output_filename

    with get_session() as session:
        job = session.get(Job, row["id"])
        external = job.external_id or "" if job else ""
    stem = output_filename(row["company"], row["title"], external)[:-5]
    folder = output_dir() / stem
    return folder if folder.exists() else None


def current_resume() -> Path | None:
    from resume.parser import find_base_resume
    return find_base_resume(resume_dir())


# -- actions ----------------------------------------------------------------

@app.post("/upload")
def upload():
    """Take a resume and re-derive the search from it.

    Re-deriving is not optional: the whole search comes from the resume, so a
    new resume with the old profile would hunt for the previous person's jobs.
    """
    uploaded = request.files.get("resume")
    if not uploaded or not uploaded.filename:
        return jsonify(ok=False, error="No file chosen."), 400

    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in RESUME_SUFFIXES:
        return jsonify(
            ok=False,
            error=f"{suffix or 'That file'} cannot be read. Use .docx, .pdf, .txt or .md.",
        ), 400

    # "base" in the name wins over every other candidate, so an upload is
    # unambiguous rather than relying on which file was touched last.
    target = resume_dir() / f"base_resume{suffix}"
    resume_dir().mkdir(parents=True, exist_ok=True)
    uploaded.save(target)

    ok, output = run_command("profile", "--force")
    return jsonify(ok=ok, file=target.name, output=output)


@app.post("/job/<int:job_id>/<action>")
def act(job_id: int, action: str):
    """packet / brief / letter — each one a command, each one reporting back."""
    commands = {"packet": ["packet"], "brief": ["brief"], "letter": ["letter"]}
    if action not in commands:
        return jsonify(ok=False, error=f"Unknown action {action!r}"), 400

    ok, output = run_command(*commands[action], str(job_id))
    return jsonify(ok=ok, output=output)


@app.post("/job/<int:job_id>/status")
def set_status(job_id: int):
    """Mark a job Applied, Interviewing, and so on.

    This column belongs to the user: the pipeline may set it only while it is
    still in a system-owned state, and never overwrites a real decision.
    """
    value = (request.form.get("status") or request.json.get("status", "")).strip()
    if value not in STATUS_VALUES:
        return jsonify(ok=False, error=f"{value!r} is not a status."), 400

    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return jsonify(ok=False, error="No such job."), 404
        job.status = value
        job.exported_to_excel = False        # push the change to the sheet
    return jsonify(ok=True, status=value)


@app.post("/job/<int:job_id>/accept")
def accept(job_id: int):
    """Take a model's reply back in, guard it, and write the file."""
    reply = (request.form.get("reply") or "").strip()
    kind = request.form.get("kind", "resume")
    if not reply:
        return jsonify(ok=False, error="Paste the model's reply first."), 400

    scratch = output_dir() / f".reply-{job_id}.json"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text(reply, encoding="utf-8")
    try:
        args = ["accept", str(job_id), "--file", str(scratch)]
        if kind == "letter":
            args.append("--letter")
        ok, output = run_command(*args)
    finally:
        scratch.unlink(missing_ok=True)
    return jsonify(ok=ok, output=output)


@app.get("/job/<int:job_id>/brief-text")
def brief_text(job_id: int):
    """The part of the brief meant for pasting, without the instructions."""
    kind = request.args.get("kind", "resume")
    ok, output = run_command("letter" if kind == "letter" else "brief", str(job_id))
    if not ok:
        return jsonify(ok=False, error=output), 400

    path = next((Path(line.strip()) for line in output.splitlines()
                 if line.strip().endswith(".txt")), None)
    if not path or not path.exists():
        return jsonify(ok=False, error="The brief file could not be found."), 500

    text = path.read_text(encoding="utf-8")
    # "COPY FROM HERE" appears twice: once quoted in the how-to, once as the
    # real marker. Splitting on the first hands over the instructions instead
    # of the prompt, so take the LAST occurrence.
    marker = "COPY FROM HERE"
    if marker in text:
        text = text.rsplit(marker, 1)[1].lstrip("-\n ")
    return jsonify(ok=True, text=text, path=str(path))


@app.post("/run/<task>")
def run_task(task: str):
    """Kick off a pipeline command from the browser."""
    allowed = {"scrape": ["scrape"], "score": ["score"], "export": ["export"],
               "daily": ["daily"]}
    if task not in allowed:
        return jsonify(ok=False, error=f"Unknown task {task!r}"), 400
    ok, output = run_command(*allowed[task])
    return jsonify(ok=ok, output=output[-4000:])


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg()
    port = int(cfg().get("webapp.port", 8770) or 8770)
    print(f"\n  Job search console -> http://127.0.0.1:{port}\n")
    # Bound to localhost: this holds a resume and a job history, and it has no
    # login because it was never meant to be reachable from anywhere else.
    app.run(host="127.0.0.1", port=port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
