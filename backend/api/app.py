"""A local web app for the job search — the terminal, without the terminal.

Everything here already exists as a command; this only removes the need to
remember which one. It runs on this machine, against this machine's database
and this machine's resume, and it holds nobody else's data: one install is one
person, which is why there is no login and nothing to secure.

    python -m backend.api            # then open http://127.0.0.1:8770

Paths come from config rather than being hardcoded, so a second person runs
their own copy rather than sharing yours.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for

from backend.config.loader import PROJECT_ROOT, load_config
from data_engineering.db.models import Job, STATUS_VALUES
from data_engineering.db.session import get_session, init_engine
from backend.core.jobage import age_days, age_label
from backend.core.jobfields import UNSTATED, experience_label, salary_label, workplace_label

log = logging.getLogger(__name__)

RESUME_SUFFIXES = (".docx", ".pdf", ".txt", ".md")
MAX_UPLOAD_MB = 10

app = Flask(__name__, template_folder=str(PROJECT_ROOT / "frontend" / "templates"),
            static_folder=str(PROJECT_ROOT / "frontend" / "static"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

_cfg = None


def cfg():
    global _cfg
    if _cfg is None:
        _cfg = load_config()
        init_engine(_cfg.database_url)
    return _cfg


def resume_dir() -> Path:
    configured = cfg().get("resume.base_path") or "ml/resume"
    path = Path(configured)
    path = path if path.is_absolute() else PROJECT_ROOT / path
    return path if path.is_dir() else path.parent


def output_dir() -> Path:
    path = Path(cfg().get("resume.output_dir", "ml/resume/output"))
    return path if path.is_absolute() else PROJECT_ROOT / path


def run_command(*args: str) -> tuple[bool, str]:
    """Run one main.py command and hand back what it printed.

    Shelling out rather than importing keeps one definition of what a command
    does: the web app cannot drift from the CLI because it IS the CLI.
    """
    result = subprocess.run(
        [sys.executable, "main.py", *args],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
        timeout=1500,          # tailoring one job may take three rounds
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
        # A score measured against a title alone is not on the same scale as
        # one measured against a full posting. Zoom scoring 23% never meant it
        # was a weak match; it meant there were 217 characters to judge it by
        # instead of 6,758. The page has to show the difference or the number
        # misleads in the one direction that matters: burying a good job.
        "basis": job.score_basis or "full",
        "age": age_label(job),
        # The exact age, not the label. The page used to parse "3 days" back
        # into a number to sort by, which lost the precision and could not
        # read "just now" at all -- so a posting thirty seconds old sorted as
        # though its age were unknown.
        "ageDays": age_days(job),
        "workplace": stated(workplace_label(job.workplace)),
        "experience": stated(experience_label(job.experience_min_years,
                                              job.experience_max_years)),
        "salary": stated(salary_label(job.salary_min, job.salary_max,
                                      job.salary_currency, job.salary_period)),
        "status": job.status,
        "tags": eligibility_tags(job),
        # The same facts as columns, to read across and sort by.
        **eligibility_columns(job),
    }


CLEARANCE_SHORT = {"Public Trust": "Trust", "Secret": "Secret", "Top Secret": "TS",
                   "TS/SCI": "TS/SCI", "Clearance": "Yes"}
CLEARANCE_RANK = {"Clearance": 1, "Public Trust": 1, "Secret": 2, "Top Secret": 3, "TS/SCI": 4}


def eligibility_columns(job) -> dict:
    """Clearance, citizenship and visa as short column values, with a sort key for clearance."""
    clearance, rank = "", None
    if job.clearance:
        # Short: the colour says must-hold (orange) or can-obtain (grey); the tooltip says it in words.
        clearance = CLEARANCE_SHORT.get(job.clearance, job.clearance) + ("+poly" if job.polygraph else "")
        clearance += "" if job.clearance_active else " (obtain)"
        # Must-hold above can-get, and a higher level above a lower one.
        rank = CLEARANCE_RANK.get(job.clearance, 1) + (10 if job.clearance_active else 0)
    citizen = {"US citizen": "US only", "US citizen or green card": "US or GC"}.get(job.citizenship or "", "")
    visa = {"no": "No sponsor", "yes": "Sponsors"}.get(job.sponsorship or "", "")
    return {"clr": clearance, "clrRank": rank, "citizen": citizen, "visa": visa}


def my_authorisation() -> dict:
    """The person's own answers on work authorisation, from their profile (may be empty)."""
    import yaml
    try:
        data = yaml.safe_load((PROJECT_ROOT / "backend" / "config" / "applicant.yaml").read_text()) or {}
        return data.get("authorisation") or {}
    except (OSError, yaml.YAMLError):
        return {}


def with_eligibility(row: dict, job, auth: dict) -> dict:
    from backend.core.eligibility import eligible
    ok, reasons = eligible(job, auth)
    row["eligible"], row["not_eligible_because"] = ok, reasons
    return row


def eligibility_tags(job) -> list[dict]:
    """Short tags for who can take the job: a bar ("bar") or worth knowing ("info")."""
    tags = []
    if job.clearance:
        level = job.clearance if job.clearance != "Clearance" else "Security clearance"
        if job.clearance_active:
            tags.append({"t": f"🔒 {level}{' + poly' if job.polygraph else ''}, must hold", "k": "bar"})
        else:
            tags.append({"t": f"🔓 {level} (can obtain)", "k": "info"})
    if job.citizenship == "US citizen":
        tags.append({"t": "🇺🇸 US citizens only", "k": "bar"})
    elif job.citizenship:
        tags.append({"t": "🇺🇸 Citizen or green card", "k": "bar"})
    if job.sponsorship == "no":
        tags.append({"t": "🛂 No visa sponsorship", "k": "bar"})
    elif job.sponsorship == "yes":
        tags.append({"t": "✅ Sponsors visas", "k": "good"})
    if getattr(job, "eligibility_manual", None):
        tags.append({"t": "✎ set by you", "k": "info"})
    return tags


# -- pages ------------------------------------------------------------------

@app.route("/")
def index():
    with get_session() as session:
        jobs = (
            session.query(Job).filter(Job.is_open.is_(True))
            .order_by(Job.ats_match_score.desc()).all()
        )
        auth = my_authorisation()
        rows = collapse_repeats([with_eligibility(job_row(j), j, auth) for j in jobs])

    resume = current_resume()
    return render_template(
        "index.html",
        rows=rows,
        statuses=STATUS_VALUES,
        resume=resume_label(),
        resume_dir=str(resume_dir()),
        # The filename alone does not say whether the search matches the
        # person: a supply chain resume that silently derived a software
        # search looks identical on the page. Show the derived search.
        search=derived_search() if resume else None,
        strong=sum(1 for r in rows if r["score"] is not None and r["score"] >= 70),
        my_years=derived_years(),
        applicant=applicant_state(),
        unscored=sum(1 for r in rows if r["score"] is None),
        **last_run_stats(),
    )


@app.get("/applicant")
def applicant_form():
    """The answers every application form asks for, as a form.

    These lived only in backend/config/applicant.yaml. Asking someone to open a text
    editor and change `null` to `true` on the right line, in a file they have
    never seen, is a worse experience than the thing it unlocks -- and it was
    the only step left between having the tool and being able to apply with it.
    """
    from backend.apply.profile import Applicant, ProfileIncomplete, check_ready, load

    try:
        person = load(applicant_path())
    except ProfileIncomplete:
        person = Applicant()

    return render_template("applicant.html", a=person,
                           problems=check_ready(person))


@app.post("/applicant")
def applicant_save():
    """Write the form back, keeping everything the form does not ask about."""
    import yaml

    from backend.apply.profile import Applicant, check_ready, load

    path = applicant_path()
    # Read what is there first: the file carries demographics, employment
    # preferences and apply settings this form deliberately does not show, and
    # overwriting them because they were not on screen would be theft.
    existing = {}
    if path.exists():
        try:
            existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            existing = {}

    def field(name: str) -> str:
        return " ".join((request.form.get(name) or "").split())

    def tri(name: str):
        """Yes, no, or still unanswered. A legal declaration has no default."""
        raw = (request.form.get(name) or "").strip().lower()
        return True if raw == "yes" else False if raw == "no" else None

    existing.setdefault("personal", {}).update({
        "first_name": field("first_name"), "last_name": field("last_name"),
        "email": field("email"), "phone": field("phone"),
    })
    existing.setdefault("location", {}).update({
        "city": field("city"), "state": field("state"),
        "postal_code": field("postal_code"),
        "country": field("country") or "United States",
    })
    existing.setdefault("links", {}).update({
        "linkedin": field("linkedin"), "github": field("github"),
    })
    existing.setdefault("authorisation", {}).update({
        "authorised_to_work": tri("authorised_to_work"),
        "requires_sponsorship": tri("requires_sponsorship"),
        "us_citizen": tri("us_citizen"),
        "green_card": tri("green_card"),
        "security_clearance": field("security_clearance") or None,
    })
    existing.setdefault("employment", {}).update({
        "current_employer": field("current_employer"),
        "current_title": field("current_title"),
    })
    # An unticked box is absent from the form, so absent means "stop".
    existing.setdefault("apply", {})["stop_before_submit"] = \
        request.form.get("auto_submit") != "yes"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(existing, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")

    person = load(path)
    problems = check_ready(person)
    return jsonify(ok=not problems, problems=problems, saved=str(path.name))


def applicant_path() -> Path:
    return PROJECT_ROOT / "backend" / "config" / "applicant.yaml"


def applicant_state() -> dict:
    """Whether the apply step can run, for the banner on the main page."""
    from backend.apply.profile import Applicant, ProfileIncomplete, check_ready, load

    try:
        person = load(applicant_path())
    except ProfileIncomplete:
        return {"exists": False, "ready": False, "missing": 6}
    problems = check_ready(person)
    return {"exists": True, "ready": not problems, "missing": len(problems)}


def derived_years() -> int | None:
    """Years of experience the resume claims, for the "within my experience"
    filter. None when it could not be read, and then the filter does nothing
    rather than guessing a number and hiding jobs on the strength of it."""
    try:
        from backend.config.loader import PROJECT_ROOT
        from ml.resume.profile import PROFILE_FILENAME, load_profile

        profile = load_profile(
            PROJECT_ROOT / cfg().get("filters.profile_path", PROFILE_FILENAME))
        years = getattr(profile, "years_experience", None) if profile else None
        return int(years) if years else None
    except Exception:
        return None


def collapse_repeats(rows: list[dict]) -> list[dict]:
    """Fold a company's identical postings into one row, counted.

    Employers repost. Bluelight Consulting had one role listed twenty times,
    Accenture Federal six, and Xometry four -- each a separate posting id, so
    each a separate row. 286 of 1,983 open postings were repeats, and they
    cluster at the top because a good match repeats as a good match. The top
    hundred held four copies of one Xometry role.

    Folded rather than deleted: a repost can be a real second location, and
    the job page for each still exists. The row says how many, so nothing is
    hidden -- it just stops one employer filling the screen.
    """
    seen: dict[tuple[str, str], dict] = {}
    ordered: list[dict] = []
    for row in rows:
        key = ((row.get("company") or "").strip().lower(),
               (row.get("title") or "").strip().lower())
        first = seen.get(key)
        if first is None:
            row["repeats"] = 1
            seen[key] = row
            ordered.append(row)
            continue
        first["repeats"] += 1
        # Keep the freshest of the set on screen: the same role posted twice
        # is worth applying to at its newest, not its oldest.
        if (row.get("ageDays") is not None and first.get("ageDays") is not None
                and row["ageDays"] < first["ageDays"]):
            first["id"], first["age"], first["url"] = row["id"], row["age"], row["url"]
    return ordered


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

    from data_engineering.db.models import Company, ScrapeLog

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


def service_up(base_url: str, timeout: float = 0.4) -> bool:
    """Is something listening at this address right now?

    A keyless local provider is always "configured", so without this the page
    would call a gateway ready whether or not it had ever been started, and
    the button would fail on press with nothing on the page having warned
    about it. A TCP connect is enough and costs under half a second.
    """
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(base_url or "")
    host, port = parsed.hostname, parsed.port
    if not host:
        return False
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def writer_state() -> dict:
    """The one-click writer: who it is, whether it can run, and what it costs.

    This button used to be hard-wired to the paid API, so it appeared only
    when an Anthropic key was set and always said "pay". A local model does
    the same job for nothing -- measured at ~31s for a full tailoring -- so
    the button now follows whatever the settings page chose and says plainly
    which of the two it is. Nobody is asked to pay for what their own machine
    can already do.

    Copy-and-paste stays the primary button regardless. It costs nothing and
    uses a chat subscription the person is probably already paying for.
    """
    import os

    from ml.resume.llm import (DEFAULT_OLLAMA_HOST, PROVIDERS, model_name,
                            provider_name)

    provider = provider_name(cfg())
    spec = PROVIDERS.get(provider, {})
    env_key = spec.get("env_key")
    # A provider needing a key it has not got cannot run; one that needs no
    # key -- a local model, a self-hosted gateway -- always can.
    ready = bool(spec) and (not env_key or spec.get("key_optional")
                            or bool(os.getenv(env_key)))
    free = bool(spec.get("free"))

    # A service on this machine also has to be running, not merely chosen.
    local_url = spec.get("base_url") or (
        DEFAULT_OLLAMA_HOST if provider == "ollama" else "")
    if ready and local_url and "localhost" in local_url:
        ready = service_up(local_url)

    model = model_name(cfg())
    # Where it runs and how long it takes, as measured on one resume
    # (27-28 Sep 2026): the page used to say "on this machine, about half a
    # minute" of a cloud model that took two and a half.
    if provider == "claude-code":
        label, where, takes = "Or let Claude write it", "on your Claude plan", "about a minute"
    elif provider == "chatgpt":
        label, where, takes = "Or let ChatGPT write it", "on your ChatGPT plan", "about a minute"
    elif provider == "gemini":
        label, where, takes = "Or let Gemini write it", "on your Google account", "about a minute"
    elif provider == "ollama" and model.endswith(":cloud"):
        label, where, takes = "Or let Ollama Cloud write it", "on Ollama Cloud", "two or three minutes"
    elif provider == "ollama":
        label, where, takes = "Or let this Mac write it", "on this Mac", "several minutes"
    elif free:
        label, where, takes = "Or write it here, free", "", "a minute or two"
    else:
        label, where, takes = "Or pay the API to do it", "", "a minute or two"
    return {
        "provider": provider,
        "needs_starting": bool(local_url) and not ready,
        "model": model or "Claude",
        "ready": ready,
        "free": free,
        "label": label,
        "where": where,
        "takes": takes,
    }


@app.route("/job/<int:job_id>")
def job_detail(job_id: int):
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return "No such job", 404
        row = with_eligibility(job_row(job), job, my_authorisation())
        row["elig"] = {"clearance": job.clearance or "", "clearance_active": bool(job.clearance_active),
                       "polygraph": bool(job.polygraph), "citizenship": job.citizenship or "",
                       "sponsorship": job.sponsorship or "", "manual": bool(job.eligibility_manual)}
        # Where the posting is silent on visas, the company's own record says something.
        if job.sponsorship is None:
            from backend.core.eligibility import sponsor_history
            program = sponsor_history(job.company)
            if program:
                row["tags"].append({"t": f"✅ Has sponsored {program} before", "k": "good"})
        description = job.description or ""
        requirements = job.requirements or ""
    # Many boards give no separate requirements, and the field holds the
    # whole posting again: shown twice, the page opened on a wall of text.
    if " ".join(requirements.split()) == " ".join((description or "").split()):
        requirements = ""

    import os

    from ml.resume.llm import model_name, provider_name

    writer = writer_state()
    # The template's older name. It means "the second button can run", not
    # "the API is configured" -- a local model makes it free.
    api_ready = writer["ready"]

    folder = packet_folder(row)
    docs = {kind: path is not None for kind, path in job_documents(job_id).items()}
    packet = {
        "exists": folder is not None,
        "path": str(folder) if folder else "",
        # The tailored resume is written beside the folder, not into it, so
        # looking only inside said "not done" for a resume that existed.
        "has_resume": docs["resume_pdf"] or docs["resume_docx"],
        "has_letter": docs["letter"],
    }
    return render_template(
        "job.html",
        job=row, description=description, requirements=requirements,
        statuses=STATUS_VALUES, packet=packet, docs=docs,
        api_ready=api_ready, api_model=writer["model"], writer=writer,
    )


def packet_folder(row: dict) -> Path | None:
    from ml.resume.writer import output_filename

    with get_session() as session:
        job = session.get(Job, row["id"])
        external = job.external_id or "" if job else ""
    from ml.resume.packet import packet_dir
    folder = packet_dir(cfg(), row["company"], row["title"], external, job_id=row["id"])
    return folder if folder.exists() else None


def current_resume() -> Path | None:
    from ml.resume.parser import find_base_resume
    return find_base_resume(resume_dir())


def resume_candidates() -> list[Path]:
    """Every file in ml/resume/ that could become the active resume.

    The same rule `find_base_resume` selects from, so nothing can be left
    behind that would silently take over once the current one is gone.
    """
    from ml.resume.parser import find_base_resume

    directory = resume_dir()
    if not directory.exists():
        return []
    return [
        p for p in directory.iterdir()
        if p.is_file()
        and p.suffix.lower() in RESUME_SUFFIXES
        and not p.name.startswith("~$")
    ] if find_base_resume(directory) else []


UPLOAD_NOTE = ".uploaded.json"     # a dot-file, and not a resume suffix,
                                   # so it can never be mistaken for a resume


def upload_note_path() -> Path:
    return resume_dir() / UPLOAD_NOTE


def remember_upload(original: str, stored: Path) -> None:
    """Write down the filename the user actually chose.

    Storing every upload as `base_resume.<ext>` is what makes the active
    resume unambiguous -- "base" beats every other candidate, so the choice
    never depends on which file was touched last. The cost is that the name
    the person recognises is thrown away, and the page then shows everybody
    the same meaningless `base_resume.pdf`. This keeps the name beside the
    file instead of in it.
    """
    try:
        upload_note_path().write_text(json.dumps({
            "original": original,
            "stored": stored.name,
            "at": datetime.now().isoformat(timespec="seconds"),
        }), encoding="utf-8")
    except OSError:
        pass          # a missing note falls back to the stored name; not fatal


def resume_label() -> dict | None:
    """What to call the active resume on the page, and what it is stored as.

    Falls back to the stored filename when there is no note, or when the note
    describes a different file -- somebody can drop a resume into the folder
    by hand, and then the note is about a file that is no longer there.
    """
    resume = current_resume()
    if resume is None:
        return None
    try:
        note = json.loads(upload_note_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        note = {}
    original = note.get("original") if note.get("stored") == resume.name else None
    return {
        "name": original or resume.name,
        "stored": resume.name,
        "renamed": bool(original) and original != resume.name,
        "at": note.get("at") if original else None,
    }


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
    remember_upload(Path(uploaded.filename).name, target)

    ok, output = run_command("profile", "--force")
    return jsonify(ok=ok, file=Path(uploaded.filename).name, stored=target.name,
                   output=output, search=derived_search())


# Everything a fresh clone needs that is deliberately not in the repo. The
# repo is public, so these are gitignored -- which means a person who clones
# it gets a working tool with no way to see what is missing except by reading
# .env.example. This is that list, as a form.
#
# `secret` fields are never sent back to the browser. The page reports only
# whether one is set, because a page that redisplays a key is a page that can
# leak it -- to a screenshot, a shoulder, a cached tab.
ENV_FIELDS = [
    {
        "key": "ANTHROPIC_API_KEY", "secret": True,
        "label": "Anthropic API key",
        "needed_for": "Writing tailored resumes and cover letters with the API",
        "help": "Optional. Everything else -- scraping, scoring, ranking -- "
                "works without it, and the app can hand you a prompt to paste "
                "into a Claude subscription instead of spending on API calls.",
        "placeholder": "sk-ant-...",
        "link": "https://console.anthropic.com/settings/keys",
        "link_label": "Create a key in the Anthropic Console",
    },
    {
        "key": "OPENAI_API_KEY", "secret": True,
        "label": "OpenAI API key",
        "needed_for": "Writing with OpenAI instead of Anthropic",
        "help": "Only if you pick OpenAI as the writing model below.",
        "placeholder": "sk-...",
        "link": "https://platform.openai.com/api-keys",
        "link_label": "Create an OpenAI key",
    },
    {
        "key": "OMNIROUTE_API_KEY", "secret": True,
        "label": "OmniRoute gateway key",
        "needed_for": "Reaching your own OmniRoute gateway, if you gave it a key",
        "help": "Optional. A gateway on this machine usually needs none — "
                "paste one only if you generated a server key in the "
                "OmniRoute dashboard. This is NOT a provider key: the keys "
                "OmniRoute uses to reach OpenAI, Anthropic and the rest are "
                "added inside its own dashboard, not here.",
        "placeholder": "leave blank unless you made one",
        "link": "http://localhost:20128",
        "link_label": "Open the OmniRoute dashboard",
    },
    {
        "key": "OPENROUTER_API_KEY", "secret": True,
        "label": "OpenRouter key",
        "needed_for": "Reaching ~450 models from most vendors with one key",
        "help": "About 25 of those models cost nothing — any id ending "
                "':free'. Pay-as-you-go for the rest, no subscription.",
        "placeholder": "sk-or-...",
        "link": "https://openrouter.ai/keys",
        "link_label": "Create an OpenRouter key",
    },
    {
        "key": "XAI_API_KEY", "secret": True,
        "label": "xAI (Grok) API key",
        "needed_for": "Writing with Grok instead of Anthropic",
        "help": "Only if you pick Grok as the writing model below.",
        "placeholder": "xai-...",
        "link": "https://console.x.ai",
        "link_label": "Create an xAI key",
    },
    {
        "key": "ADZUNA_APP_ID", "secret": False,
        "label": "Adzuna app ID",
        "needed_for": "Finding postings from employers on no board this tool scrapes",
        "help": "Free. Adzuna collects postings from thousands of job sites; "
                "the daily run searches it for your job titles. Needs the key below too.",
        "placeholder": "8-character id",
        "link": "https://developer.adzuna.com/signup",
        "link_label": "Get free Adzuna keys",
    },
    {
        "key": "ADZUNA_APP_KEY", "secret": True,
        "label": "Adzuna app key",
        "needed_for": "Adzuna searches (with the app ID above)",
        "help": "The 32-character key shown next to your app ID on the Adzuna dashboard.",
        "placeholder": "32-character key",
        "link": "https://developer.adzuna.com/admin/access_details",
        "link_label": "See your Adzuna keys",
    },
    {
        "key": "USAJOBS_API_KEY", "secret": True,
        "label": "USAJOBS API key",
        "needed_for": "Every US federal job, searched daily",
        "help": "Free. Searches use your mailbox address as the email USAJOBS asks for — register the key with that same email.",
        "placeholder": "",
        "link": "https://developer.usajobs.gov/APIRequest/Index",
        "link_label": "Request a USAJOBS key",
    },
    {
        "key": "JOOBLE_API_KEY", "secret": True,
        "label": "Jooble API key",
        "needed_for": "A second large job aggregator, searched daily",
        "help": "Free. Jooble emails the key after you fill in its form.",
        "placeholder": "",
        "link": "https://jooble.org/api/about",
        "link_label": "Get a Jooble key",
    },
    {
        "key": "CAREERJET_AFFID", "secret": True,
        "label": "Careerjet affiliate ID",
        "needed_for": "Careerjet's job index, searched daily",
        "help": "Free partner ID from Careerjet's partner program.",
        "placeholder": "",
        "link": "https://www.careerjet.com/partners/",
        "link_label": "Join Careerjet partners",
    },
    {
        "key": "FINDWORK_API_KEY", "secret": True,
        "label": "Findwork API key",
        "needed_for": "Software and DevOps jobs from Findwork.dev",
        "help": "Free after signing up.",
        "placeholder": "",
        "link": "https://findwork.dev/developers/",
        "link_label": "Get a Findwork key",
    },
    {
        "key": "MAIL_ADDRESS", "secret": False,
        "label": "Mailbox address",
        "needed_for": "Importing jobs out of job-alert emails",
        "help": "The mailbox that receives job alerts. Opened read-only -- "
                "nothing is ever sent, moved, marked or deleted.",
        "placeholder": "you@gmail.com",
        "link": "https://mail.google.com/mail/u/0/#settings/fwdandpop",
        "link_label": "Turn IMAP on in Gmail settings",
    },
    {
        "key": "MAIL_APP_PASSWORD", "secret": True,
        "label": "Mailbox app password",
        "needed_for": "Importing jobs out of job-alert emails",
        "help": "An app password, not your account password. Gmail: "
                "myaccount.google.com/apppasswords",
        "placeholder": "16 characters",
        "link": "https://myaccount.google.com/apppasswords",
        "link_label": "Create a Google app password",
        "note": "Needs 2-Step Verification switched on first, or the page "
                "will not offer app passwords.",
    },
    {
        "key": "SCRAPER_CONTACT_EMAIL", "secret": False,
        "label": "Contact address for site owners",
        "needed_for": "Politeness — advertised in the scraper's User-Agent",
        "help": "Optional but good practice: it is how a site owner reaches "
                "you instead of silently blocking you.",
        "placeholder": "you@example.com",
    },
    {
        "key": "RESULTS_PASSPHRASE", "secret": True,
        "label": "Cloud results passphrase",
        "needed_for": "Decrypting results from the GitHub Actions daily run",
        "help": "Only if you run the scrape in GitHub Actions. Must match the "
                "repository secret of the same name.",
        "placeholder": "any long phrase",
        "link_label": "Set the matching repository secret",
    },
    {
        "key": "DATABASE_URL", "secret": False,
        "label": "Database location",
        "needed_for": "Optional — defaults to data/jobs.db",
        "help": "Leave blank unless you want the database somewhere else.",
        "placeholder": "sqlite:///data/jobs.db",
    },
]


def env_path() -> Path:
    return PROJECT_ROOT / ".env"


def github_secrets_url() -> str | None:
    """This clone's own Actions-secrets page, or None if there is no GitHub remote.

    Sending someone to a generic documentation page when the exact page they
    need is one `git remote` away is a small rudeness that adds up.
    """
    import re
    import subprocess

    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?$", remote)
    if not match:
        return None
    return f"https://github.com/{match.group(1)}/{match.group(2)}/settings/secrets/actions"


# What to type into each provider's own sign-up form, shown on the Settings
# page under its link. Every answer is honest for one person's job search: the
# visitor counts and "website" questions are written for publishers, and the
# right answer to them is the smallest option and a LinkedIn page. {email},
# {phone}, {linkedin} and {name} are shown as generic prompts, never filled in.
PURPOSE_TEXT = ("Personal job search. No public website: a small tool on my own computer "
                "uses the API to find jobs in my field for myself. Nothing is republished.")
SIGNUP_GUIDES = {
    "ADZUNA_APP_ID": {
        "rows": [("Username", "Anything, e.g. your name plus _jobs"), ("Email", "{email}"),
                 ("Password", "Your own — a password manager can make one. Never share it"),
                 ("Organisation/Group Name", "Independent (personal job search)"),
                 ("Organisation/Group website", "{linkedin}"),
                 ("Your application of the Adzuna API", "Personal or academic research"),
                 ("Average Monthly Visitors", "The lowest option — you run no website"),
                 ("Primary Market", "North America"), ("Primary Industry", "Career Services")],
        "steps": ["Register, then confirm the email Adzuna sends",
                  "Sign in and open API Access Details",
                  "Copy the Application ID and Application Key into the two boxes here"],
    },
    "USAJOBS_API_KEY": {
        "rows": [("First / Last Name", "{name}"), ("Email Address", "{email}"),
                 ("Contact Phone Number", "{phone}"),
                 ("Company or Agency", "Independent (personal job search)"),
                 ("How will you use the jobs", PURPOSE_TEXT),
                 ("I agree to the Terms of Service", "Tick it — the agreement is yours")],
        "steps": ["Submit — the key arrives by email within minutes",
                  "Paste it here (or ask Claude to read it from your inbox)"],
    },
    "JOOBLE_API_KEY": {
        "rows": [("Your name", "{name}"), ("Position", "Your current or target job title — or simply: Job seeker"),
                 ("Email", "{email}"), ("Website", "{linkedin}"), ("Phone", "{phone}")],
        "steps": ["Submit — Jooble emails the key, usually within a day",
                  "Paste it here"],
    },
    "CAREERJET_AFFID": {
        "rows": [("Website address", "{linkedin}"),
                 ("Main country of activity", "United States of America"),
                 ("Average monthly visitors", "The lowest option — you run no website"),
                 ("Describe your website", PURPOSE_TEXT)],
        "steps": ["Register, then sign in to the partner area",
                  "Copy your affiliate ID (\"affid\") into the box here",
                  "Careerjet may decline a request with no website — Adzuna covers similar jobs"],
    },
    "FINDWORK_API_KEY": {
        "rows": [("Sign up", "With email and a password of your own, or GitHub")],
        "steps": ["Open the Developers page (link above)",
                  "Click the eye icon next to \"API key\" to show it — copy only the "
                  "40-character key, not the page (the dots •••••• are not the key)",
                  "Paste it here"],
    },
}


def signup_guide(key: str) -> dict | None:
    """The guide for one provider, in general terms.

    Written for whoever uses this tool, never filled with the current user's
    own details: the page can be shown, screenshotted or shared, and a guide
    is about which answer goes in which box, not about one person.
    """
    guide = SIGNUP_GUIDES.get(key)
    if not guide:
        return None
    generic = {"email": "Your email address", "phone": "Your phone number — or +1 000 000 0000 if you would rather not share it",
               "name": "Your name", "linkedin": "Your LinkedIn profile URL (or any page about you)"}
    return {"rows": [(f, v.format(**generic)) for f, v in guide["rows"]], "steps": guide["steps"]}


def env_state() -> list[dict]:
    """Each setting, and whether it is set -- never the secret values themselves."""
    from backend.config.envfile import read_env

    stored = read_env(env_path())
    state = []
    for field in ENV_FIELDS:
        value = (stored.get(field["key"]) or "").strip()
        link = field.get("link")
        if field["key"] == "RESULTS_PASSPHRASE":
            link = github_secrets_url()       # this clone's repo, not a generic one
        state.append({
            **field,
            "link": link,
            "set": bool(value),
            "guide": signup_guide(field["key"]),
            # Shown only for things that are not credentials.
            "value": "" if field["secret"] else value,
        })
    return state


def writing_model_state() -> dict:
    """Which model writes the tailoring, and whether it can actually run.

    Free and local is the default and is listed first, deliberately. Somebody
    who already pays for a chat subscription should not be nudged into buying
    API credit to use this tool -- the job page's first offer is still a
    prompt to paste into the subscription they already have, and this page
    should not contradict that.
    """
    from backend.config.envfile import read_env
    from ml.resume.llm import (DEFAULT_OLLAMA_HOST, PROVIDERS,
                            default_model_for, provider_name)

    stored = read_env(env_path())
    cfg = load_config()
    # provider_name reads os.environ, which may lag the file the user just
    # saved, so the file wins here.
    chosen = (stored.get("RESUME_PROVIDER") or "").strip().lower() or provider_name(cfg)

    options = []
    for name, spec in PROVIDERS.items():
        key = spec.get("env_key")
        # A keyless provider is always "configured". Whether it can actually
        # answer is a different question, and the one worth showing.
        url = spec.get("base_url") or (DEFAULT_OLLAMA_HOST if name == "ollama" else "")
        local = bool(url) and "localhost" in url
        has_key = (True if key is None or spec.get("key_optional")
                   else bool((stored.get(key) or "").strip()))
        options.append({
            "name": name,
            "label": spec["label"],
            "free": spec["free"],
            "env_key": key,
            "local": local,
            "url": url,
            "running": service_up(url) if local else None,
            "ready": has_key and (service_up(url) if local else True),
            "note": spec.get("note", ""),
            "default_model": default_model_for(name),
        })
    options.sort(key=lambda o: (not o["free"], o["label"]))

    return {
        "chosen": chosen,
        "model": (stored.get("RESUME_MODEL") or "").strip(),
        "model_placeholder": default_model_for(chosen),
        "options": options,
    }


@app.get("/setup")
def setup_form():
    """The settings a public repo cannot carry, filled in on this machine.

    These live in .env, which is gitignored precisely so that cloning the repo
    never hands anybody someone else's keys. The consequence is that a fresh
    clone silently lacks all of them, and the only way to find out which was
    to read .env.example. This says it out loud instead.
    """
    return render_template(
        "setup.html",
        fields=env_state(),
        writing=writing_model_state(),
        env_file=str(env_path()),
        exists=env_path().exists(),
        applicant=applicant_state(),
        resume=resume_label(),
    )


@app.post("/setup")
def setup_save():
    """Write the settings to .env. Blank leaves a secret alone; Clear removes it.

    A blank box cannot mean "delete" for a secret, because the page never
    shows the current value -- so a blank box is the normal state of a field
    that is already set, and treating that as "delete" would wipe a working
    key every time somebody saved an unrelated change.
    """
    from backend.config.envfile import write_env

    updates: dict[str, str | None] = {}
    for field in ENV_FIELDS:
        key = field["key"]
        submitted = (request.form.get(key) or "").strip()
        if request.form.get(f"clear__{key}"):
            updates[key] = None
        elif submitted:
            updates[key] = submitted
        # A blank box never deletes anything, visible or secret. It used to
        # for visible fields, on the theory that you can see what is there --
        # and MAIL_ADDRESS was silently emptied by a save of something else,
        # which broke the job-alert import with no error anywhere. Losing a
        # stored value must take the same deliberate act as losing a key.

    from ml.resume.llm import PROVIDERS

    provider = (request.form.get("RESUME_PROVIDER") or "").strip().lower()
    if provider:
        if provider not in PROVIDERS:
            return jsonify(ok=False, error=f"Unknown writing model {provider!r}."), 400
        updates["RESUME_PROVIDER"] = provider
    if "RESUME_MODEL" in request.form:
        # Blank means "whatever that provider's default is", not a blank model.
        updates["RESUME_MODEL"] = (request.form.get("RESUME_MODEL") or "").strip() or None
        # Switching provider while the model box still holds the OLD provider's
        # model saved an impossible pair (openrouter + gemma4:e4b, 26 Sep 2026):
        # a local model name means nothing to a hosted service. An unchanged
        # model across a provider change is carried over by accident, so drop
        # it and let the new provider's default apply.
        from backend.config.envfile import read_env
        stored = read_env(env_path())
        before = (stored.get("RESUME_PROVIDER") or "").strip().lower()
        if provider and before and provider != before \
                and updates["RESUME_MODEL"] == (stored.get("RESUME_MODEL") or "").strip():
            updates["RESUME_MODEL"] = None

    try:
        changed = write_env(env_path(), updates)
    except OSError as exc:
        return jsonify(ok=False, error=f"Could not write .env: {exc}"), 500

    # The names of what changed, never the values -- this response is logged
    # by the browser, the terminal, and anything watching either.
    return jsonify(ok=True, changed=changed, fields=[
        {"key": f["key"], "set": f["set"]} for f in env_state()
    ])


@app.post("/resume/remove")
def remove_resume():
    """Detach the resume and the search derived from it, together.

    Both or neither. A profile left behind without its resume is the failure
    this tool is meant to prevent -- the next person to upload would be
    searched for under the last person's job titles until they noticed.

    Stored jobs are deliberately left alone. They are evidence of what was
    already found, they cost a scrape to replace, and they are scored against
    whatever resume is current, so a stale one cannot masquerade as a match.
    """
    from backend.config.loader import PROJECT_ROOT, load_config
    from ml.resume.profile import PROFILE_FILENAME

    # Every readable file in ml/resume/ is a candidate, and the newest one wins.
    # So removing only the active file promotes whichever resume is next in
    # line -- on this machine that was a previous person's, which would have
    # made "remove" quietly mean "switch back to the last user".
    removed = []
    for path in sorted(resume_candidates()):
        path.unlink()
        removed.append(path.name)

    profile = PROJECT_ROOT / load_config().get("filters.profile_path", PROFILE_FILENAME)
    if profile.exists():
        profile.unlink()
        removed.append(profile.name)

    upload_note_path().unlink(missing_ok=True)

    if not removed:
        return jsonify(ok=False, error="There was no resume to remove."), 404
    return jsonify(ok=True, removed=removed)


def derived_search() -> dict:
    """What the uploaded resume will actually be searched for.

    Shown back straight after an upload, because the alternative is finding out
    from an empty results table a scrape later. Someone whose career this tool
    does not recognise needs to know that at the moment they upload, while the
    fix -- editing the profile YAML -- is still obviously the next thing to do.
    """
    from backend.config.loader import PROJECT_ROOT, load_config
    from ml.resume.profile import PROFILE_FILENAME, load_profile

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
            "backend/config/search_profile.yaml."
        ),
    }


@app.post("/job/<int:job_id>/<action>")
def act(job_id: int, action: str):
    """packet / brief / letter / tailor — each one a command, reporting back.

    `tailor` is the only one that spends money, and it takes its job id as a
    flag rather than positionally, which is why the table holds argument lists
    rather than command names. `?estimate=1` prices the call without making it.
    """
    commands = {
        "packet": ["packet"],
        "brief": ["brief"],
        "letter": ["letter"],
        "write-letter": ["letter", "--write"],
        "tailor": ["tailor", "--job-id"],
        "align": ["tailor", "--align", "--job-id"],
    }
    if action not in commands:
        return jsonify(ok=False, error=f"Unknown action {action!r}"), 400

    args = list(commands[action])
    if action in ("tailor", "align", "write-letter"):
        teaser = _teaser_words(job_id)
        if teaser is not None:
            return jsonify(ok=False, error=(
                f"This posting is only a {teaser}-word teaser from the job board, not the real "
                f"posting, so there is nothing to tailor to — a rewrite would stay where it is. "
                f"Open the posting, copy the full text, and paste it in “Paste the full posting” "
                f"at the top of this page. Then press this again.")), 400
    if action == "tailor" and request.args.get("estimate"):
        args.insert(1, "--estimate")
    if action == "packet":
        # From the "where should it go" popup: remember the place, then build there.
        body = request.get_json(silent=True) or {}
        if body.get("base"):
            base, problem = _usable_folder(str(body["base"]))
            if problem:
                return jsonify(ok=False, error=problem), 400
            from ml.resume.packet import choose_place
            with get_session() as session:
                job = session.get(Job, job_id)
                if job is None:
                    return jsonify(ok=False, error="No such job."), 404
                choose_place(cfg(), job, base, remember=bool(body.get("remember")))

    ok, output = run_command(*args, str(job_id))
    return jsonify(ok=ok, output=output)


def job_documents(job_id: int) -> dict[str, Path | None]:
    """The tailored resume (PDF and .docx) and cover letter for one job.

    Paths come from the job's own record, never from the request, so the file
    route below cannot be pointed at anything else on the disk.
    """
    from ml.resume.packet import LETTER_FILE, RESUME_FILE, packet_dir, packet_root
    from ml.resume.writer import output_filename

    found = {"resume_pdf": None, "resume_docx": None, "letter": None}
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return found
        company, title, external = job.company, job.title, job.external_id or ""
    config = cfg()
    flat = packet_root(config) / output_filename(company, title, external)
    folder = packet_dir(config, company, title, external, job_id=job_id)
    for key, candidates in (
        ("resume_pdf", [flat.with_suffix(".pdf")]),
        ("resume_docx", [flat, folder / RESUME_FILE]),
        ("letter", [folder / LETTER_FILE]),
    ):
        found[key] = next((p for p in candidates if p.exists()), None)
    return found


@app.get("/resume/roles")
def resume_roles():
    """The job lines of the master resume, for "I've used this"."""
    from ml.resume.additions import roles
    from ml.resume.pipeline import load_base_resume
    try:
        return jsonify(ok=True, roles=roles(load_base_resume(cfg())))
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


@app.post("/resume/additions")
def resume_add():
    """Add one line of the person's own experience under one of their jobs."""
    from ml.resume.additions import NotAdded, add
    from ml.resume.pipeline import load_base_resume
    data = request.get_json(silent=True) or {}
    try:
        entry = add(load_base_resume(cfg()), str(data.get("role") or ""), str(data.get("line") or ""),
                    str(data.get("tool") or ""), bool(data.get("confirmed")))
    except NotAdded as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, said=f"Added under {entry['role'].split('  ')[0]}. Every resume from now on "
                                 f"can use it — press Rewrite again to use it for this job.")


@app.get("/job/<int:job_id>/ats")
def job_ats(job_id: int):
    """ATS keyword match: the person's resume, and the tailored PDF, against the posting."""
    from ml.resume.ats import check
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return jsonify(ok=False, error="No such job."), 404
        session.expunge(job)
    try:
        return jsonify(ok=True, **check(cfg(), job))
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


def _teaser_words(job_id: int) -> int | None:
    """The word count when a job holds only a board's teaser, else None."""
    from ml.resume.scorer import score_basis
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None or score_basis(job.description) == "full":
            return None
        return len((job.description or "").split())


@app.post("/job/<int:job_id>/posting")
def job_posting(job_id: int):
    """The full posting, pasted by the person over a board's teaser, then scored again."""
    from ml.resume.pipeline import score_jobs
    from ml.resume.scorer import score_basis
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    # Text copied out of some pages carries apostrophes doubled ("You''ll").
    text = text.replace("''", "'")
    if score_basis(text) != "full":
        return jsonify(ok=False, error="That is still too short to be the full posting — paste the "
                                       "whole job description, responsibilities and requirements included."), 400
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return jsonify(ok=False, error="No such job."), 404
        # The content hash is left as the board's: a re-scrape of the same
        # teaser then changes nothing, and the pasted posting stays.
        job.description = text
        job.requirements = None
    try:
        score_jobs(cfg(), job_ids=[job_id], include_closed=True)
    except Exception as exc:
        return jsonify(ok=True, said=f"Saved. Scoring failed ({type(exc).__name__}); press Re-score later.")
    return jsonify(ok=True, said="Saved and scored against the full posting.")


@app.post("/job/<int:job_id>/tailor-full")
def job_tailor_full(job_id: int):
    """Resume Tailoring for this job: the saved resume against its posting.

    Runs in the background like the Tailor page; when the files are made they
    become this job's tailored resume, so View PDF and the keyword check use them.
    """
    from ml.resume.pipeline import base_resume_path
    from ml.resume.writer import output_filename
    from ml.resume.packet import packet_root
    from ml.tailoring.pipeline import finish_job_run, new_run, remember_job_run, save_saved_resume, start
    teaser = _teaser_words(job_id)
    if teaser is not None:
        return jsonify(ok=False, error=(
            f"This posting is only a {teaser}-word teaser from the job board, so there is nothing "
            f"to tailor to. Paste the full posting in the box at the top of this page first.")), 400
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return jsonify(ok=False, error="No such job."), 404
        jd = f"{job.title}\n\n{job.description or ''}"
        company = job.company
        target = packet_root(cfg()) / output_filename(job.company, job.title, job.external_id or "")
        from ml.resume.packet import packet_dir
        packet = packet_dir(cfg(), job.company, job.title, job.external_id or "", job_id=job_id)
    try:
        saved = base_resume_path(cfg())
    except FileNotFoundError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    folder = new_run()
    resume = save_saved_resume(folder, saved)
    remember_job_run(job_id, folder, target, packet)
    start(folder, resume, jd, None, cfg(), company=company,
          after=lambda result: finish_job_run(
              job_id, folder, target, (result.get("before_after") or {}).get("ats", [0, result["score"]])[1], packet))
    return jsonify(ok=True, run=folder.name)


@app.get("/job/<int:job_id>/tailored")
def job_tailored(job_id: int):
    """This job's last tailoring run, so the page can show it or follow it."""
    from ml.tailoring.pipeline import job_best, job_run
    best, best_score = job_best(job_id)
    return jsonify(ok=True, run=job_run(job_id), best=best, best_score=best_score)


@app.get("/job/<int:job_id>/suggestions")
def job_suggestions(job_id: int):
    """What the posting asks for that the resume does not show, with a drafted line each."""
    from ml.resume.suggest import for_job
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return jsonify(ok=False, error="No such job."), 404
        session.expunge(job)
    if _teaser_words(job_id) is not None:
        return jsonify(ok=True, items=[], roles=[], teaser=True)
    try:
        return jsonify(ok=True, **for_job(cfg(), job, refresh=bool(request.args.get("refresh"))))
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


@app.post("/job/<int:job_id>/suggestions")
def job_suggestions_accept(job_id: int):
    """Add the lines the person kept to their resume."""
    from ml.resume.suggest import accept
    chosen = (request.get_json(silent=True) or {}).get("items") or []
    if not isinstance(chosen, list):
        return jsonify(ok=False, error="Nothing to add."), 400
    added, problems = accept(cfg(), job_id, [c for c in chosen if isinstance(c, dict)])
    return jsonify(ok=True, added=added, problems=problems)


@app.post("/job/<int:job_id>/eligibility")
def job_eligibility(job_id: int):
    """The person's own reading of clearance, citizenship and visa, kept over re-parsing.

    {"reset": true} goes back to what the posting itself says.
    """
    from backend.core.eligibility import LEVELS
    from backend.core.jobfields import ELIGIBILITY_FIELDS, derive
    body = request.get_json(silent=True) or {}
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return jsonify(ok=False, error="No such job."), 404
        if body.get("reset"):
            parsed = derive(job.title, job.location, job.description, job.requirements)
            for field in ELIGIBILITY_FIELDS:
                setattr(job, field, parsed[field])
            job.eligibility_manual = None
        else:
            level = body.get("clearance") or None
            if level is not None and level not in (*LEVELS, "Clearance"):
                return jsonify(ok=False, error="Unknown clearance level."), 400
            citizenship = body.get("citizenship") or None
            if citizenship not in (None, "US citizen", "US citizen or green card"):
                return jsonify(ok=False, error="Unknown citizenship rule."), 400
            sponsorship = body.get("sponsorship") or None
            if sponsorship not in (None, "yes", "no"):
                return jsonify(ok=False, error="Unknown visa answer."), 400
            job.clearance = level
            job.clearance_active = bool(body.get("clearance_active")) if level else None
            job.polygraph = bool(body.get("polygraph")) if level else None
            job.citizenship, job.sponsorship = citizenship, sponsorship
            job.eligibility_manual = True
        values = {f: getattr(job, f) for f in ELIGIBILITY_FIELDS}
    return jsonify(ok=True, manual=not body.get("reset"), **values)


@app.get("/job/<int:job_id>/prep")
def job_prep(job_id: int):
    """The interview prep made for this job, if any."""
    from ml.interview.prep import load
    return jsonify(ok=True, prep=load(job_id))


@app.post("/job/<int:job_id>/prep")
def job_prep_make(job_id: int):
    """Make the interview prep (one model call, about a minute) and its links-first PDF."""
    from ml.interview.prep import SHOWN_FOR, prepare, write_pdf
    from ml.resume.packet import packet_dir
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return jsonify(ok=False, error="No such job."), 404
        if job.status not in SHOWN_FOR:
            return jsonify(ok=False, error="Interview prep is for jobs you have applied to."), 400
        session.expunge(job)
    try:
        prep = prepare(cfg(), job)
    except Exception as exc:
        return jsonify(ok=False, error=f"{str(exc)[:300]}"), 500
    folder = packet_dir(cfg(), job.company, job.title, job.external_id or "", job_id=job.id)
    try:
        pdf = write_pdf(prep, folder / "interview-prep.pdf")
        prep["pdf"] = str(pdf)
    except Exception as exc:
        prep["pdf_error"] = str(exc)[:200]
    return jsonify(ok=True, prep=prep)


@app.get("/job/<int:job_id>/prep.pdf")
def job_prep_pdf(job_id: int):
    from ml.resume.packet import packet_dir
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return jsonify(ok=False, error="No such job."), 404
        pdf = packet_dir(cfg(), job.company, job.title, job.external_id or "", job_id=job.id) / "interview-prep.pdf"
    if not pdf.exists():
        return jsonify(ok=False, error="Make the prep first."), 404
    return send_file(pdf, mimetype="application/pdf")


@app.get("/apply")
def apply_page():
    return render_template("apply.html")


@app.get("/apply/prepared")
def apply_prepared():
    """The jobs prepared for applying, with their tailoring result and open questions."""
    from backend.apply.accounts import status as account_status
    from backend.apply.prepare import pending
    from ml.tailoring.pipeline import RUNS
    items = []
    for e in pending():
        run = (e.get("tailored") or {}).get("run")
        ba = None
        if run:
            try:
                ba = json.loads((RUNS / run / "result.json").read_text()).get("before_after")
            except (OSError, ValueError):
                ba = None
        with get_session() as session:
            job = session.get(Job, e["job_id"])
            tags = eligibility_tags(job) if job else []
            ok, why = with_eligibility({}, job, my_authorisation()).values() if job else (True, [])
        items.append({**e, "before_after": ba, "tags": tags, "eligible": ok, "not_eligible_because": why})
    return jsonify(ok=True, items=items, account=account_status())


@app.post("/apply/answers")
def apply_answers():
    from backend.apply.prepare import save_answers
    body = request.get_json(silent=True) or {}
    answers = body.get("answers") or {}
    if not isinstance(answers, dict) or not str(body.get("job_id", "")).isdigit():
        return jsonify(ok=False, error="Nothing to save."), 400
    save_answers(int(body["job_id"]), {str(k): str(v) for k, v in answers.items()})
    return jsonify(ok=True)


@app.post("/apply/skip/<int:job_id>")
def apply_skip(job_id: int):
    from backend.apply.prepare import mark
    mark(job_id, "skipped")
    return jsonify(ok=True)


@app.get("/account/status")
def account_status():
    """Whether a job-site email and password are saved (never the password itself)."""
    from backend.apply.accounts import status
    return jsonify(ok=True, **status())


@app.post("/account")
def account_save():
    """Keep the job-site email and password: the password goes to the Keychain only."""
    from backend.apply.accounts import save
    body = request.get_json(silent=True) or {}
    try:
        save(str(body.get("email") or ""), str(body.get("password") or ""))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, error=f"The Keychain refused it ({type(exc).__name__})."), 500
    return jsonify(ok=True)


@app.post("/account/forget")
def account_forget():
    from backend.apply.accounts import forget
    forget()
    return jsonify(ok=True)


@app.get("/plans/status")
def plans_status():
    """Who is signed in to Claude, ChatGPT and Gemini, for the Settings page."""
    from ml.resume.plans import TOOLS, status
    return jsonify(ok=True, plans=[status(name) for name in TOOLS])


@app.post("/plans/<name>/<action>")
def plans_action(name: str, action: str):
    """Open the tool's own sign-in, sign-out or install in a Terminal window."""
    from ml.resume.plans import TOOLS, open_in_terminal
    if name not in TOOLS or action not in ("signin", "signout", "install"):
        return jsonify(ok=False, error="Unknown request."), 400
    try:
        command = open_in_terminal(name, action)
    except Exception as exc:
        return jsonify(ok=False, error=f"Could not open Terminal ({type(exc).__name__}). "
                                       f"Run it yourself: {TOOLS[name][action]}"), 500
    return jsonify(ok=True, command=command)


@app.get("/job/<int:job_id>/progress")
def job_progress(job_id: int):
    """How far the resume rewrite has got, for the progress bar."""
    from ml.resume import progress
    return jsonify(ok=True, **(progress.read(job_id) or {}))


@app.get("/job/<int:job_id>/score")
def job_score(job_id: int):
    """The requirement-based match score (the Tailor page's), saved per job."""
    from ml.resume.pipeline import base_resume_path
    from ml.tailoring.pipeline import job_score as score
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return jsonify(ok=False, error="No such job."), 404
        description = job.description or ""
    try:
        return jsonify(ok=True, **score(job_id, base_resume_path(cfg()), description, cfg()))
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


@app.get("/job/<int:job_id>/files")
def job_files(job_id: int):
    """Which documents exist now, so the page can show links without a reload."""
    return jsonify(ok=True, **{k: v is not None for k, v in job_documents(job_id).items()})


@app.get("/job/<int:job_id>/file/<kind>")
def job_file(job_id: int, kind: str):
    """Open the tailored resume or cover letter in the browser."""
    path = job_documents(job_id).get(kind)
    if path is None:
        return "Not written yet for this job.", 404
    if kind == "resume_docx":
        return send_file(path, as_attachment=True, download_name=path.name)
    if kind == "letter":
        return send_file(path, mimetype="text/plain")
    return send_file(path, mimetype="application/pdf")


def _usable_folder(raw: str) -> tuple[Path | None, str | None]:
    """A folder the person may put applications in: theirs, not the system's."""
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        return None, "Choose a full folder path, like ~/Documents/Job Applications."
    path = path.resolve()
    home = Path.home().resolve()
    if not (path == home or home in path.parents or Path("/Volumes") in path.parents):
        return None, f"{path} is outside your home folder and external drives — choose a folder of yours."
    if path.exists() and not path.is_dir():
        return None, f"{path} is a file, not a folder."
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"Cannot create {path}: {exc.strerror or exc}."
    return path, None


@app.get("/job/<int:job_id>/packet-place")
def packet_place(job_id: int):
    """Where this job's folder is, or would go, and places to choose from."""
    from ml.resume.packet import load_places, packet_dir, packet_name, packet_root
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return jsonify(ok=False, error="No such job."), 404
        company, title, external = job.company, job.title, job.external_id or ""
    config = cfg()
    folder = packet_dir(config, company, title, external, job_id=job_id)
    places = load_places(config)
    home = Path.home()
    options = [("Documents", home / "Documents" / "Job Applications"), ("Desktop", home / "Desktop"),
               ("Downloads", home / "Downloads"), ("This project", packet_root(config))]
    if places.get("default"):
        options.insert(0, ("Your default", Path(places["default"])))
    # A new folder is offered outside the project, where deleting or moving the
    # project can never take the record of what was sent with it.
    suggested = folder.parent if (folder.exists() or str(job_id) in places["jobs"] or places.get("default")) \
        else home / "Documents" / "Job Applications"
    return jsonify(ok=True, name=packet_name(company, title, external), base=str(suggested),
                   exists=folder.exists(), chosen=str(job_id) in places["jobs"],
                   default=places.get("default"),
                   options=[{"label": label, "path": str(path)} for label, path in options])


@app.post("/pick-folder")
def pick_folder():
    """The Mac's own folder picker, so the person can browse to any folder."""
    start = Path(str((request.get_json(silent=True) or {}).get("start") or Path.home())).expanduser()
    while not start.exists() and start != start.parent:
        start = start.parent
    script = f'''
with timeout of 600 seconds
  tell application "System Events"
    activate
    set chosen to choose folder with prompt "Where should the application folder go?" default location (POSIX file "{start}")
  end tell
end timeout
return POSIX path of chosen'''
    try:
        done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=620)
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="The folder picker was left open too long."), 408
    if done.returncode != 0:
        if "-128" in done.stderr:
            return jsonify(ok=False, cancelled=True)
        return jsonify(ok=False, error="The folder picker could not open here — type or paste the folder "
                                       "path instead. (" + done.stderr.strip()[-120:] + ")"), 500
    return jsonify(ok=True, path=done.stdout.strip().rstrip("/"))


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
         "apply": ["apply", "--limit", "10"],
         # Auto-apply: the morning's preparation on demand, and applying to it.
         "prepare": ["prepare-apply"],
         "apply-ready": ["apply", "--prepared"],
         "daily": ["daily"], "discover": ["discover", "--names",
                                          "data/mailbox_names.txt", "--max-slugs", "2"]}
def task_log(task: str) -> Path:
    return PROJECT_ROOT / "data" / f"webapp_{task}.log"


def task_exit_file(task: str) -> Path:
    return PROJECT_ROOT / "data" / f"webapp_{task}.exit"


def task_exit_code(task: str) -> int | None:
    """What the run actually returned, or None if it was never recorded."""
    try:
        return int(task_exit_file(task).read_text().strip())
    except (OSError, ValueError):
        return None


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
    return start_task(task, TASKS[task])


# Started only by a confirmed assistant action, never by URL: a bare
# /run/tailor would tailor every job at once.
ACTION_TASKS = {"tailor"}


def start_task(task: str, args: list[str]):
    """Run `main.py <args>` in the background as `task`, with its log and exit code."""
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
    exit_path = task_exit_file(task)
    exit_path.unlink(missing_ok=True)    # last run's verdict is not this one's

    # Wrapped in a shell purely so the exit code is written down. Guessing
    # whether a run succeeded by grepping its log for hopeful phrases has now
    # failed twice: "Nothing to score" was missing from the list, and then a
    # perfectly successful score run ended with "946 below threshold" and
    # "Run `export` ..." and matched nothing either. A process's exit code is
    # the answer and everything else is a guess, so write it to a file that
    # outlives the process -- this app cannot wait() on a detached child.
    command = " ".join(shlex.quote(part) for part in
                       [sys.executable, "-u", "main.py", *args])
    process = subprocess.Popen(
        ["/bin/sh", "-c", f"{command}; echo $? > {shlex.quote(str(exit_path))}"],
        cwd=PROJECT_ROOT, stdout=handle, stderr=subprocess.STDOUT,
        # Its own session, so restarting this app does not kill half an hour
        # of scraping along with it.
        start_new_session=True,
    )
    task_pid_file(task).write_text(f"{process.pid}\n{time.time()}")
    return jsonify(ok=True, started=True)


@app.get("/run/probe/status")
def probe_status():
    """How the company-name probe is doing, for the status box.

    Read from devops/probe_runner's own state file rather than tracked here:
    the run outlives this app by days, survives restarts of both, and may have
    been started from a terminal. A file both sides agree on is the only thing
    that stays true across all of that.
    """
    import json

    state_path = PROJECT_ROOT / "data" / "probe_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return jsonify(ok=True, running=False, ever_run=False)

    pid = state.get("pid")
    alive = False
    if pid:
        try:
            os.kill(int(pid), 0)
            alive = not _is_zombie(int(pid))
        except (OSError, TypeError, ValueError):
            alive = False

    done, total = state.get("done") or 0, state.get("total") or 0
    # Stop is honoured between batches, so the process outlives the click. Say
    # so, or the button flips back to "Stop" and reads as though it was ignored.
    stopping = alive and (PROJECT_ROOT / "data" / "probe_runner.stop").exists()
    return jsonify(
        ok=True, ever_run=True, running=alive, stopping=stopping,
        done=done, total=total,
        percent=round(done / total * 100, 2) if total else None,
        found=state.get("found_total") or 0,
        rate=state.get("names_per_hour"),
        eta_hours=state.get("eta_hours"),
        state=state.get("status"),
        updated=state.get("updated_at"),
    )


@app.post("/run/probe/<action>")
def probe_control(action: str):
    """Start or stop the probe run from the page."""
    if action not in ("start", "stop"):
        return jsonify(ok=False, error=f"Unknown action {action!r}"), 400

    if action == "stop":
        (PROJECT_ROOT / "data" / "probe_runner.stop").write_text("stop")
        try:
            pid = int((PROJECT_ROOT / "data" / "probe_runner.pid").read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        return jsonify(ok=True, note="stopping after the current batch")

    # The refined list first: it holds the 22% of companies that can plausibly
    # have a board, and the button pointed past it at the raw 1.75M file.
    names = PROJECT_ROOT / "data" / "refined_names_worthwhile.txt"
    if not names.exists():
        names = PROJECT_ROOT / "data" / "refined_names_all.txt"
    if not names.exists():
        return jsonify(ok=False, error=f"No name list at {names.name}. Build one "
                                       f"with: python -m data_engineering.companies.refine"), 400
    # Its own session, because this run is measured in days and must not die
    # with the app, the terminal, or this request.
    subprocess.Popen(
        [sys.executable, "-m", "devops.probe_runner", "--names", str(names)],
        cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
    )
    return jsonify(ok=True, note="started")


def _scheduled_run_holding(task: str) -> str | None:
    """The launchd task whose work overlaps this one, if it is running now.

    devops/daily_pipeline.sh takes a lock directory per task, and `daily` chains
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
    from data_engineering.db.models import Company
    with get_session() as session:
        return session.query(Company).filter(Company.active.is_(True)).count()


@app.get("/run/<task>/status")
def run_status(task: str):
    """How far along, how much longer, and the last thing it said."""
    if task not in TASKS and task not in ACTION_TASKS:
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

        if task == "apply":
            # The runner's own lines are written for the person watching: the
            # latest "[3/10] Company — Title" or "Waiting for you" says it all.
            for line in reversed(lines):
                if line.startswith("[") or line.strip().startswith(("Waiting", "Applied", "Skipped")):
                    progress = line.strip()[:160]
                    break

        if task in ("scrape", "daily"):
            # One line per board finished. Counting them is honest progress;
            # a timer would only be guessing.
            done = sum(1 for ln in lines if "data_engineering.scraper.runner" in ln and " seen " in ln)
            total = board_count()
            parts = [ln for ln in lines if "data_engineering.scraper.runner" in ln and " seen " in ln]
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
    recorded = task_exit_code(task)
    if recorded is not None:
        ok_finish = finished and recorded == 0
    else:
        # No recorded code: a run started before this app learned to write one,
        # or killed hard enough that the shell never got to. Fall back to the
        # old phrase-matching, which is why it is still here -- but it is the
        # fallback now, not the answer.
        ok_finish = finished and any(marker in tail for marker in (
            "Companies scraped", "Scored", "Exported", "Saved to",
            "Nothing to score", "No new jobs", "done:", "Run finished",
            "below threshold", "Run `export`",
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


# -- the assistant ---------------------------------------------------------------
# Claude Code, headless, with the job tools only (backend/assistant/chat.py).
# Its actions are proposals; these routes are where the person decides.

@app.get("/assistant")
def assistant_page():
    from backend.assistant import chat, tools
    return render_template("assistant.html", installed=chat.claude_path() is not None,
                           pending=tools.pending_actions())


@app.post("/assistant/send")
def assistant_send():
    from backend.assistant import chat
    message = ((request.get_json(silent=True) or {}).get("message") or "").strip()
    if not message:
        return jsonify(ok=False, error="Type a message first."), 400
    if len(message) > 4000:
        return jsonify(ok=False, error="That message is too long."), 400
    turn = chat.start(message, model=cfg().get("assistant.model") or None)
    return jsonify(ok=True, turn=turn.id)


@app.get("/assistant/poll/<turn_id>")
def assistant_poll(turn_id: str):
    from backend.assistant import chat
    return jsonify(chat.poll(turn_id, int(request.args.get("since", 0))))


@app.post("/assistant/new")
def assistant_new():
    from backend.assistant import chat
    chat.new_conversation()
    return jsonify(ok=True)


@app.get("/assistant/pending")
def assistant_pending():
    from backend.assistant import tools
    return jsonify(pending=tools.pending_actions())


@app.post("/assistant/action/<action_id>/<decision>")
def assistant_decide(action_id: str, decision: str):
    """Confirm or cancel one proposed action. Each can be decided once."""
    from backend.assistant import tools
    if decision not in ("confirm", "cancel"):
        return jsonify(ok=False, error="Confirm or cancel."), 400
    action = tools.take(action_id, "confirmed" if decision == "confirm" else "cancelled")
    if action is None:
        return jsonify(ok=False, error="That action was already decided, or does not exist."), 409
    if decision == "cancel":
        return jsonify(ok=True, said="Cancelled — nothing was done.")

    params = action["params"]
    if action["kind"] == "tailor":
        response = start_task("tailor", ["tailor", "--job-id", str(params["job_id"])])
        return _action_started(response, "tailor", "Tailoring started — it takes a few minutes.",
                               action_id)
    if action["kind"] == "apply":
        args = (["apply", "--job-id", str(params["job_id"])] if params.get("job_id")
                else ["apply", "--limit", str(params.get("count", 1))])
        response = start_task("apply", args)
        return _action_started(response, "apply",
                               "The browser is opening — sign in where asked, and press Submit yourself.",
                               action_id)
    try:
        return jsonify(ok=True, said=tools.run_direct(action))
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


def _action_started(response, task: str, said: str, action_id: str):
    """What starting it said. When it could not start, the action waits again."""
    from backend.assistant import tools
    body, code = (response if isinstance(response, tuple) else (response, 200))
    data = body.get_json()
    if not data.get("ok") or data.get("started") is False:
        tools.reopen(action_id)
        error = data.get("error") or f"A {task} run is already going — confirm again when it finishes."
        return jsonify(ok=False, error=error), code if code != 200 else 409
    return jsonify(ok=True, said=said, task=task)


# -- Resume Tailoring --------------------------------------------------------------
# Upload a resume and a job description; ml/tailoring runs the six stages in the
# background and this page follows them, then shows the results tabs.

TAILOR_TYPES = (".docx", ".pdf")


@app.get("/tailor")
def tailor_page():
    # From a job page: the posting is filled in for the person.
    jd = ""
    if request.args.get("job", "").isdigit():
        with get_session() as session:
            job = session.get(Job, int(request.args["job"]))
            jd = (f"{job.title}\n\n{job.description or ''}") if job else ""
    # From a job page the person's saved resume is used, so one press starts it.
    saved = ""
    if jd:
        try:
            from ml.resume.pipeline import base_resume_path
            saved = base_resume_path(cfg()).name
        except Exception:
            saved = ""
    return render_template("tailor.html", accepted=",".join(TAILOR_TYPES), jd=jd, saved=saved,
                           autostart=bool(jd and saved and request.args.get("start")))


@app.post("/tailor/start")
def tailor_start():
    from ml.tailoring.pipeline import new_run, save_inputs, start
    resume = request.files.get("resume")
    jd_file = request.files.get("jd_file")
    jd_text = (request.form.get("jd_text") or "").strip()
    use_saved = (resume is None or not resume.filename) and request.form.get("use_saved") == "1"
    if use_saved:
        resume = None
    elif resume is None or not resume.filename:
        return jsonify(ok=False, file="resume", error="Choose your resume (.docx or .pdf) first."), 400
    if resume is not None and Path(resume.filename).suffix.lower() not in TAILOR_TYPES:
        return jsonify(ok=False, file="resume",
                       error=f"{resume.filename}: use a .docx or .pdf resume."), 400
    has_jd_file = jd_file is not None and bool(jd_file.filename)
    if not jd_text and not has_jd_file:
        return jsonify(ok=False, file="job", error="Paste the job description or upload it as a file."), 400
    if has_jd_file and Path(jd_file.filename).suffix.lower() not in (".docx", ".pdf", ".txt", ".md"):
        return jsonify(ok=False, file="job",
                       error=f"{jd_file.filename}: use a .docx, .pdf or .txt job description."), 400
    folder = new_run()
    if use_saved:
        from ml.resume.pipeline import base_resume_path
        from ml.tailoring.pipeline import save_saved_resume
        try:
            saved = base_resume_path(cfg())
        except FileNotFoundError as exc:
            return jsonify(ok=False, file="resume", error=str(exc)), 400
        resume_path = save_saved_resume(folder, saved)
        _, jd_path = save_inputs(folder, None, jd_file if has_jd_file else None)
    else:
        resume_path, jd_path = save_inputs(folder, resume, jd_file if has_jd_file else None)
    start(folder, resume_path, jd_text, jd_path, cfg())
    return jsonify(ok=True, run=folder.name)


def _tailor_run(run_id):
    from ml.tailoring.pipeline import folder_of
    folder = folder_of(run_id)
    if folder is None:
        return None
    return folder


@app.get("/tailor/<run_id>/status")
def tailor_status(run_id):
    folder = _tailor_run(run_id)
    if folder is None:
        return jsonify(ok=False, error="No such run."), 404
    try:
        return jsonify(ok=True, **json.loads((folder / "progress.json").read_text()))
    except (OSError, ValueError):
        return jsonify(ok=True, stage=0, done=False, error=None, note="")


@app.get("/tailor/<run_id>/result")
def tailor_result(run_id):
    folder = _tailor_run(run_id)
    if folder is None or not (folder / "result.json").exists():
        return jsonify(ok=False, error="No result yet."), 404
    data = json.loads((folder / "result.json").read_text())
    data.pop("folder", None)
    return jsonify(ok=True, **data)


@app.get("/tailor/<run_id>/download/<kind>")
def tailor_download(run_id, kind):
    folder = _tailor_run(run_id)
    if folder is None or kind not in ("docx", "pdf"):
        return jsonify(ok=False, error="No such file."), 404
    path = folder / f"tailored-resume.{kind}"
    if not path.exists():
        return jsonify(ok=False, error="No such file."), 404
    title = re.sub(r"[^\w -]+", "", json.loads((folder / "result.json").read_text()).get("title", ""))[:50]
    return send_file(path, as_attachment=True,
                     download_name=f"Tailored resume - {title or 'job'}.{kind}")


@app.get("/tailor/<run_id>/page/<name>")
def tailor_page_image(run_id, name):
    folder = _tailor_run(run_id)
    if folder is None or not re.fullmatch(r"(pdf|docx)-page\d+\.png", name):
        return jsonify(ok=False, error="No such page."), 404
    path = folder / "pages" / name
    if not path.exists():
        return jsonify(ok=False, error="No such page."), 404
    return send_file(path, mimetype="image/png", max_age=0)


@app.get("/tailor/<run_id>/lines")
def tailor_lines(run_id):
    from ml.tailoring.pipeline import editable_lines
    folder = _tailor_run(run_id)
    if folder is None or not (folder / "result.json").exists():
        return jsonify(ok=False, error="No result yet."), 404
    return jsonify(ok=True, lines=editable_lines(folder))


@app.post("/tailor/<run_id>/edit")
def tailor_edit(run_id):
    from ml.tailoring.pipeline import apply_edits
    folder = _tailor_run(run_id)
    if folder is None or not (folder / "result.json").exists():
        return jsonify(ok=False, error="No result yet."), 404
    edits = (request.get_json(silent=True) or {}).get("edits") or {}
    edits = {str(k): str(v) for k, v in edits.items() if isinstance(k, str)}
    if not edits:
        return jsonify(ok=False, error="Nothing was changed."), 400
    try:
        data = apply_edits(folder, edits)
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500
    # A run made for a saved job: its edited files are that job's resume too.
    from ml.tailoring.pipeline import job_of_run, place_for_job
    owner = job_of_run(folder)
    if owner:
        place_for_job(folder, Path(owner["docx"]), Path(owner["packet"]) if owner.get("packet") else None)
    data.pop("folder", None)
    return jsonify(ok=True, **data)


@app.get("/health")
def health_report():
    """The newest weekly health report; ?fresh=1 makes one now (a few seconds)."""
    folder = PROJECT_ROOT / "data" / "reports"
    if request.args.get("fresh"):
        subprocess.run([sys.executable, str(PROJECT_ROOT / "devops" / "health_report.py")],
                       cwd=PROJECT_ROOT, capture_output=True, timeout=120)
    reports = sorted(folder.glob("health-*.html"))
    if not reports:
        if request.args.get("fresh"):
            return "The health report could not be made — see data/daily_run.log.", 500
        return redirect(url_for("health_report", fresh=1))
    return send_file(reports[-1], mimetype="text/html", max_age=0)
