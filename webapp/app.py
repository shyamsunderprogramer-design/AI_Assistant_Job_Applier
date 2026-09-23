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

import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

from config.loader import PROJECT_ROOT, load_config
from db.models import Job, STATUS_VALUES
from db.session import get_session, init_engine
from jobage import age_days, age_label
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
    }


# -- pages ------------------------------------------------------------------

@app.route("/")
def index():
    with get_session() as session:
        jobs = (
            session.query(Job).filter(Job.is_open.is_(True))
            .order_by(Job.ats_match_score.desc()).all()
        )
        rows = collapse_repeats([job_row(j) for j in jobs])

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

    These lived only in config/applicant.yaml. Asking someone to open a text
    editor and change `null` to `true` on the right line, in a file they have
    never seen, is a worse experience than the thing it unlocks -- and it was
    the only step left between having the tool and being able to apply with it.
    """
    from apply.profile import Applicant, ProfileIncomplete, check_ready, load

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

    from apply.profile import Applicant, check_ready, load

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
    })

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(existing, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")

    person = load(path)
    problems = check_ready(person)
    return jsonify(ok=not problems, problems=problems, saved=str(path.name))


def applicant_path() -> Path:
    return PROJECT_ROOT / "config" / "applicant.yaml"


def applicant_state() -> dict:
    """Whether the apply step can run, for the banner on the main page."""
    from apply.profile import Applicant, ProfileIncomplete, check_ready, load

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
        from config.loader import PROJECT_ROOT
        from resume.profile import PROFILE_FILENAME, load_profile

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

    from resume.llm import (DEFAULT_OLLAMA_HOST, PROVIDERS, model_name,
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

    return {
        "provider": provider,
        "needs_starting": bool(local_url) and not ready,
        "model": model_name(cfg()),
        "ready": ready,
        "free": free,
        "label": ("Or let this Mac write it" if provider == "ollama"
                  else "Or write it here, free" if free
                  else "Or pay the API to do it"),
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

    import os

    from resume.llm import model_name, provider_name

    writer = writer_state()
    # The template's older name. It means "the second button can run", not
    # "the API is configured" -- a local model makes it free.
    api_ready = writer["ready"]

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
        api_ready=api_ready, api_model=writer["model"], writer=writer,
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


def resume_candidates() -> list[Path]:
    """Every file in resume/ that could become the active resume.

    The same rule `find_base_resume` selects from, so nothing can be left
    behind that would silently take over once the current one is gone.
    """
    from resume.parser import find_base_resume

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


def env_state() -> list[dict]:
    """Each setting, and whether it is set -- never the secret values themselves."""
    from config.envfile import read_env

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
    from config.envfile import read_env
    from resume.llm import (DEFAULT_OLLAMA_HOST, PROVIDERS,
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
    from config.envfile import write_env

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

    from resume.llm import PROVIDERS

    provider = (request.form.get("RESUME_PROVIDER") or "").strip().lower()
    if provider:
        if provider not in PROVIDERS:
            return jsonify(ok=False, error=f"Unknown writing model {provider!r}."), 400
        updates["RESUME_PROVIDER"] = provider
    if "RESUME_MODEL" in request.form:
        # Blank means "whatever that provider's default is", not a blank model.
        updates["RESUME_MODEL"] = (request.form.get("RESUME_MODEL") or "").strip() or None

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
    from config.loader import PROJECT_ROOT, load_config
    from resume.profile import PROFILE_FILENAME

    # Every readable file in resume/ is a candidate, and the newest one wins.
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
    """packet / brief / letter / tailor — each one a command, reporting back.

    `tailor` is the only one that spends money, and it takes its job id as a
    flag rather than positionally, which is why the table holds argument lists
    rather than command names. `?estimate=1` prices the call without making it.
    """
    commands = {
        "packet": ["packet"],
        "brief": ["brief"],
        "letter": ["letter"],
        "tailor": ["tailor", "--job-id"],
    }
    if action not in commands:
        return jsonify(ok=False, error=f"Unknown action {action!r}"), 400

    args = list(commands[action])
    if action == "tailor" and request.args.get("estimate"):
        args.insert(1, "--estimate")

    ok, output = run_command(*args, str(job_id))
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
                       [sys.executable, "-u", "main.py", *TASKS[task]])
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

    Read from tools/probe_runner's own state file rather than tracked here:
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
    return jsonify(
        ok=True, ever_run=True, running=alive,
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
                                       f"with: python -m companies.refine"), 400
    # Its own session, because this run is measured in days and must not die
    # with the app, the terminal, or this request.
    subprocess.Popen(
        [sys.executable, "-m", "tools.probe_runner", "--names", str(names)],
        cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
    )
    return jsonify(ok=True, note="started")


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
