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
import os
import subprocess
import sys
import time
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
        # None means "not scored yet", which is not the same as scoring zero.
        # `or 0` collapsed the two, so 995 postings that arrived in tonight's
        # scrape and had simply never been through the scorer all displayed
        # "Fit 0%" — which reads as "this job is a terrible match for you".
        "score": (None if job.ats_match_score is None
                  else round(job.ats_match_score * 100)),
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

    resume = current_resume()
    return render_template(
        "index.html",
        rows=rows,
        statuses=STATUS_VALUES,
        resume=resume.name if resume else None,
        resume_dir=str(resume_dir()),
        strong=sum(1 for r in rows if r["score"] is not None and r["score"] >= 70),
        unscored=sum(1 for r in rows if r["score"] is None),
        **last_run_stats(),
    )


def last_run_stats() -> dict:
    """What the last full scrape actually did.

    The page showed "BOARDS 4" — a count of DISTINCT Job.source, of which there
    are only ever four — beside 413 open roles gathered from fourteen hundred
    boards. Worse, it showed nothing at all about the size of the haystack:
    413 matches reads very differently once you know they came out of 59,284
    postings. `scrape_log` has recorded all of this per board since the
    beginning; none of it was being read.
    """
    from sqlalchemy import func

    from db.models import Company, ScrapeLog

    with get_session() as session:
        boards = session.query(Company).filter(Company.active.is_(True)).count()
        by_source = dict(
            session.query(Company.source, func.count(Company.id))
            .filter(Company.active.is_(True)).group_by(Company.source)
            .order_by(func.count(Company.id).desc()).all()
        )

        # The most recent run, by its own last log line.
        latest = (
            session.query(ScrapeLog.run_id)
            .order_by(ScrapeLog.created_at.desc()).limit(1).scalar()
        )
        scanned = failed = walked = 0
        finished_at = None
        if latest:
            scanned, walked, finished_at = session.query(
                func.coalesce(func.sum(ScrapeLog.jobs_seen), 0),
                func.count(ScrapeLog.id),
                func.max(ScrapeLog.created_at),
            ).filter(ScrapeLog.run_id == latest).one()
            # Counted with a filter, not SUM(ok == False): SQLite hands that
            # expression back as a bool, so "3 boards failed" printed "True".
            failed = (
                session.query(func.count(ScrapeLog.id))
                .filter(ScrapeLog.run_id == latest, ScrapeLog.ok.is_(False))
                .scalar() or 0
            )

    return {
        "boards": boards,
        "by_source": by_source,
        "scanned": scanned,
        "walked": walked,
        "failed": failed,
        "last_run": finished_at,
    }


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
    packet = {
        "exists": folder is not None,
        "path": str(folder) if folder else "",
        "has_resume": bool(folder and (folder / "resume.docx").exists()),
        "has_letter": bool(folder and (folder / "cover-letter.md").exists()),
    }
    return render_template(
        "job.html",
        job=row, description=description, requirements=requirements,
        statuses=STATUS_VALUES, packet=packet,
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
    return jsonify(ok=ok, file=target.name, output=output, search=derived_search())


def derived_search() -> dict:
    """What the uploaded resume will actually be searched for.

    Shown back straight after an upload, because the alternative is finding out
    from an empty results table a scrape later. Someone whose career this tool
    does not recognise needs to know that at the moment they upload, while the
    fix -- editing the profile YAML -- is still obviously the next thing to do.
    """
    from config.loader import PROJECT_ROOT, load_config
    from resume.profile import PROFILE_FILENAME, load_profile

    cfg = load_config()
    profile = load_profile(
        PROJECT_ROOT / cfg.get("filters.profile_path", PROFILE_FILENAME))
    if profile is None:
        return {"ok": False, "reason": "No search profile was written."}

    return {
        "ok": bool(profile.titles),
        "titles": profile.titles[:12],
        "title_count": len(profile.titles),
        "families": profile.families,
        "seniority": profile.seniority,
        "years": profile.years_experience,
        "derived_from": profile.derived_from,
        "reason": (
            "" if profile.titles else
            "Nothing in this resume matched a known role family, and no job "
            "titles could be read from it. Rather than search for somebody "
            "else's job, the search is empty. Add the titles you want to "
            "config/search_profile.yaml."
        ),
    }


@app.post("/job/<int:job_id>/<action>")
def act(job_id: int, action: str):
    """packet / brief / letter — each one a command, each one reporting back."""
    commands = {"packet": ["packet"], "brief": ["brief"], "letter": ["letter"]}
    if action not in commands:
        return jsonify(ok=False, error=f"Unknown action {action!r}"), 400

    ok, output = run_command(*commands[action], str(job_id))
    return jsonify(ok=ok, output=output)


@app.post("/job/<int:job_id>/reveal")
def reveal(job_id: int):
    """Open the packet folder in Finder."""
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return jsonify(ok=False, error="No such job."), 404
        row = job_row(job)
    folder = packet_folder(row)
    if not folder:
        return jsonify(ok=False, error="Build the packet first."), 400
    subprocess.run(["open", str(folder)], check=False)
    return jsonify(ok=True, path=str(folder))


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


# A scrape walks 1,455 boards at a polite pace and takes about half an hour.
# Running it inside the request meant the browser gave up long before the work
# finished, so the button looked broken and the postings never arrived. Start
# it, return at once, and let the page watch the log.
# `score` alone only scores jobs that have never been scored. The button is
# labelled "Re-score", and it is pressed after a resume upload or a filter
# change — exactly when every existing score is the stale one. Without
# --rescore it printed "Nothing to score" and changed nothing.
TASKS = {"scrape": ["scrape"], "score": ["score", "--rescore"], "export": ["export"],
         "daily": ["daily"], "discover": ["discover", "--names",
                                          "data/mailbox_names.txt", "--max-slugs", "2"]}
def task_log(task: str) -> Path:
    return PROJECT_ROOT / "data" / f"webapp_{task}.log"


def task_pid_file(task: str) -> Path:
    return PROJECT_ROOT / "data" / f"webapp_{task}.pid"


def task_started_at(task: str) -> float | None:
    try:
        return float(task_pid_file(task).read_text().splitlines()[1])
    except (OSError, ValueError, IndexError):
        return None


def task_pid(task: str) -> int | None:
    """The pid of a run in flight, or None.

    Read from disk rather than memory so restarting the app does not lose
    track of work that is still going.
    """
    path = task_pid_file(task)
    try:
        pid = int(path.read_text().splitlines()[0].strip())
    except (OSError, ValueError, IndexError):
        return None
    try:
        os.kill(pid, 0)          # signal 0 just asks "are you there?"
    except OSError:
        return None

    # A zombie answers signal 0 for as long as nobody reaps it, and this app
    # never reaps: tasks are started with Popen and start_new_session, the
    # request that started them returns immediately, and no later request
    # holds the handle to wait() on. So a score that exited at 10:45 was still
    # "running" at 16:54 — six hours of a button reading "Re-scoring against
    # your resume" for a process that had been dead all day.
    #
    # Reap it if it is ours to reap, and either way report it as finished.
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return None
    except ChildProcessError:
        pass                     # not our child — an app restart loses that
    except OSError:
        return None

    return None if _is_zombie(pid) else pid


def _is_zombie(pid: int) -> bool:
    """Has this process exited and simply not been cleaned up?"""
    try:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return state.startswith("Z")


@app.post("/run/<task>")
def run_task(task: str):
    """Start a pipeline command and return immediately."""
    if task not in TASKS:
        return jsonify(ok=False, error=f"Unknown task {task!r}"), 400

    if task_pid(task):
        return jsonify(ok=True, started=False, note="already running")

    # The 6am run does the same work from launchd, and it takes hours. Without
    # this check, pressing the button during it started a SECOND full scrape:
    # every board fetched twice, and two processes writing one SQLite file
    # that is not in WAL mode. The scheduled run already holds a lock dir; the
    # button has to respect it rather than race it.
    blocking = _scheduled_run_holding(task)
    if blocking:
        return jsonify(
            ok=False,
            error=(f"The scheduled {blocking} run is going right now — it does "
                   f"the same work, and started at {_lock_started(blocking)}.\n"
                   f"Starting another would fetch every board twice. Watch this "
                   f"one instead, or wait for it to finish."),
        ), 409

    log_path = task_log(task)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w")          # each run starts a fresh log
    process = subprocess.Popen(
        [sys.executable, "-u", "main.py", *TASKS[task]],
        cwd=PROJECT_ROOT, stdout=handle, stderr=subprocess.STDOUT,
        # Its own session, so restarting this app does not kill half an hour
        # of scraping along with it.
        start_new_session=True,
    )
    task_pid_file(task).write_text(f"{process.pid}\n{time.time()}")
    return jsonify(ok=True, started=True)


def _scheduled_run_holding(task: str) -> str | None:
    """The launchd task whose work overlaps this one, if it is running now.

    tools/daily_pipeline.sh takes a lock directory per task, and `daily` chains
    scrape -> score -> export -> digest. So a manual scrape or score during a
    daily run is duplicated work, not parallel work.
    """
    if task not in ("scrape", "score", "daily"):
        return None
    for held in ("daily", task):
        lock = PROJECT_ROOT / "data" / f"{held}.lock"
        if lock.is_dir():
            return held
    return None


def _lock_started(task: str) -> str:
    """When the holding run began, read off the lock directory itself."""
    from datetime import datetime

    lock = PROJECT_ROOT / "data" / f"{task}.lock"
    try:
        return datetime.fromtimestamp(lock.stat().st_mtime).strftime("%H:%M")
    except OSError:
        return "unknown"


def _first_log_time(lines: list[str]) -> float | None:
    """When this run actually began, from its own first timestamp."""
    from datetime import datetime

    for line in lines:
        stamp = line.split(" ", 1)[0]
        try:
            clock = datetime.strptime(stamp, "%H:%M:%S").time()
        except ValueError:
            continue
        today = datetime.now()
        started = today.replace(hour=clock.hour, minute=clock.minute,
                                second=clock.second, microsecond=0)
        if started > today:          # ran before midnight
            started = started.replace(day=today.day - 1) if today.day > 1 else started
        return started.timestamp()
    return None


def board_count() -> int:
    """How many boards a scrape will walk — the denominator for progress."""
    from db.models import Company
    with get_session() as session:
        return session.query(Company).filter(Company.active.is_(True)).count()


@app.get("/run/<task>/status")
def run_status(task: str):
    """How far along, how much longer, and the last thing it said."""
    if task not in TASKS:
        return jsonify(ok=False, error=f"Unknown task {task!r}"), 400

    running = task_pid(task) is not None
    path = task_log(task)
    tail, progress, done, total, eta, current = "", None, 0, 0, None, None
    if path.exists():
        lines = [ln for ln in path.read_text(errors="replace").splitlines() if ln.strip()]
        tail = "\n".join(lines[-12:])
        for line in reversed(lines):
            if "seen" in line and "match" in line:
                progress = " ".join(line.split()[-9:])
                break

        if task in ("scrape", "daily"):
            # One line per board finished. Counting them is honest progress;
            # a timer would only be guessing.
            done = sum(1 for ln in lines if "scraper.runner" in ln and " seen " in ln)
            total = board_count()
            parts = [ln for ln in lines if "scraper.runner" in ln and " seen " in ln]
            if parts:
                fields = parts[-1].split()
                current = fields[4] if len(fields) > 4 else None
            # A run started before the pid file carried a timestamp still has
            # one: its first log line. The file's mtime is useless here — it is
            # rewritten every second, so it always reads as "just now" and the
            # rate comes out absurd.
            started = task_started_at(task) or _first_log_time(lines)
            if running and started and done > 2:
                rate = done / max(time.time() - started, 1)
                eta = int((total - done) / rate) if rate > 0 else None
    # Without a live pid the run is over; the log's own last word says whether
    # it got there, which survives an app restart where an exit code does not.
    finished = not running and bool(tail)
    # Every way a run can end well, not just the ones with something to report.
    # "Nothing to score" is a completed run, and leaving it out left the button
    # spinning on "Re-scoring against your resume" for hours after the process
    # had exited.
    ok_finish = finished and any(marker in tail for marker in (
        "Companies scraped", "Scored", "Exported", "Saved to",
        "Nothing to score", "No new jobs", "done:", "Run finished",
    ))
    return jsonify(
        ok=True, running=running, tail=tail, progress=progress,
        done=done, total=total, current=current, eta=eta,
        percent=round(done / total * 100, 1) if total else None,
        exit=None if running else (0 if ok_finish else 1),
    )


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
