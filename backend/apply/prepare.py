"""Get the day's best matches ready to apply to, before the person sits down.

After the morning scrape, for the top matches (fit at or above
`apply.auto.min_fit`, at most `apply.auto.per_day`, one per company, freshest
first -- the apply queue's own order):

  1. the application folder, where the person chose to keep them;
  2. the resume, tailored for the posting (Resume Tailoring, in rounds);
  3. a cover letter, written and checked like one written from the job page;
  4. the form's questions no saved answer covers, listed for the person.

Nothing is sent. The Apply page shows what is ready; the person answers the
questions once and presses Start applying, and every form stops before
Submit for them to check. Kept in data/apply/prepared.json.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import requests

from backend.config.loader import PROJECT_ROOT

log = logging.getLogger(__name__)

PREPARED = PROJECT_ROOT / "data" / "apply" / "prepared.json"
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job}?questions=true"


def load() -> dict:
    try:
        return json.loads(PREPARED.read_text())
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    PREPARED.parent.mkdir(parents=True, exist_ok=True)
    PREPARED.write_text(json.dumps(data, indent=1, default=str))


def pending() -> list[dict]:
    """Prepared and not yet applied to or skipped, in the order they were chosen."""
    return [e for e in load().values() if e.get("state") == "ready"]


def mark(job_id: int, state: str) -> None:
    data = load()
    if str(job_id) in data:
        data[str(job_id)]["state"] = state
        _save(data)


def save_answers(job_id: int, answers: dict[str, str]) -> None:
    """The person's answers to this form's open questions; general ones join the bank."""
    from backend.apply.answers import remember
    data = load()
    entry = data.get(str(job_id))
    if entry is None:
        return
    given = {k: v.strip() for k, v in answers.items() if (v or "").strip()}
    entry.setdefault("answers", {}).update(given)
    entry["questions"] = [q for q in entry.get("questions", []) if q["label"] not in entry["answers"]]
    _save(data)
    remember(given, company=entry.get("company", ""))      # reusable ones help every later form


# -- one questionnaire for every ready application ----------------------------------

def _still_open(entry: dict, applicant) -> list[dict]:
    """This form's questions that nothing answers yet: the profile and the answer bank
    may have learned some since the morning, and this form's own answers cover others."""
    from backend.apply.answers import normalise
    from backend.apply import salary
    own = {normalise(k) for k in (entry.get("answers") or {})}
    if applicant is None:
        return [q for q in entry.get("questions") or [] if normalise(q["label"]) not in own]
    # Answered for this job: salary by the person's rule, general questions by their
    # general answers (checked against this employer), the rest by profile and bank.
    before, applicant.job = applicant.job, salary.job_by_id(entry["job_id"])
    try:
        return [q for q in entry.get("questions") or []
                if normalise(q["label"]) not in own and applicant.answer_for(q["label"]) is None]
    finally:
        applicant.job = before


def questionnaire(applicant=None, exclude: set[int] | frozenset = frozenset()) -> dict:
    """Every ready application's open questions, with the ones several employers ask merged.

    A question merges only when both its wording and its choices match: the same
    words with different choices are different questions to answer.
    """
    from backend.apply.answers import normalise
    groups: dict[tuple, dict] = {}
    jobs = []
    for e in pending():
        if e["job_id"] in exclude:
            continue
        open_now = _still_open(e, applicant)
        jobs.append({"job_id": e["job_id"], "company": e["company"], "title": e["title"],
                     "open": len(open_now), "required": sum(1 for q in open_now if q.get("required"))})
        for q in open_now:
            key = (normalise(q["label"]), tuple(q.get("options") or ()))
            g = groups.setdefault(key, {"label": q["label"], "options": list(key[1]),
                                        "required": False, "jobs": [],
                                        "draft": (e.get("drafts") or {}).get(q["label"])})
            g["required"] = g["required"] or bool(q.get("required"))
            g["jobs"].append({"job_id": e["job_id"], "company": e["company"]})
    shared = [g for g in groups.values() if len(g["jobs"]) > 1]
    own = [g for g in groups.values() if len(g["jobs"]) == 1]
    shared.sort(key=lambda g: (not g["required"], -len(g["jobs"])))
    by_job = {j["job_id"]: [] for j in jobs}
    for g in own:
        by_job[g["jobs"][0]["job_id"]].append(g)
    for j in jobs:
        j["questions"] = sorted(by_job[j["job_id"]], key=lambda g: not g["required"])
    return {"shared": shared, "jobs": jobs}


def answer_all(answers: list[dict]) -> int:
    """Save each answer to every ready form it belongs to: [{label, value, job_ids}]."""
    per_job: dict[int, dict[str, str]] = {}
    for a in answers:
        value = str(a.get("value") or "").strip()
        if not value:
            continue
        for job_id in a.get("job_ids") or []:
            per_job.setdefault(int(job_id), {})[str(a["label"])] = value
    for job_id, given in per_job.items():
        save_answers(job_id, given)
    return sum(len(v) for v in per_job.values())


# -- the questions a form asks ------------------------------------------------------

def open_questions(job, applicant) -> list[dict]:
    """The Greenhouse form's questions no profile or saved answer covers."""
    from backend.apply.greenhouse import HANDLED
    if job.source != "greenhouse" or not job.company_slug or not job.external_id:
        return []
    try:
        body = requests.get(GREENHOUSE_API.format(slug=job.company_slug, job=job.external_id),
                            timeout=20).json()
    except (requests.RequestException, ValueError):
        return []
    out = []
    for q in body.get("questions") or []:
        fields = q.get("fields") or []
        if any(f.get("name") in HANDLED for f in fields):
            continue                                  # name, email, resume: the filler's own
        label = (q.get("label") or "").strip()
        if not label or applicant.answer_for(label) is not None:
            continue
        options = [v.get("label") for f in fields for v in (f.get("values") or []) if v.get("label")]
        out.append({"label": label, "required": bool(q.get("required")), "options": options[:30]})
    return out


# -- getting one job ready ----------------------------------------------------------

def _tailor(cfg, job) -> dict | None:
    """Resume Tailoring for this job with the saved resume, unless it is done already."""
    from ml.resume.packet import packet_dir, packet_root
    from ml.resume.pipeline import base_resume_path
    from ml.resume.writer import output_filename
    from ml.tailoring import pipeline as tailoring

    best, score = tailoring.job_best(job.id)
    if best:
        return {"run": best, "ats": score}
    folder = tailoring.new_run()
    resume = tailoring.save_saved_resume(folder, base_resume_path(cfg))
    target = packet_root(cfg) / output_filename(job.company, job.title, job.external_id or "")
    packet = packet_dir(cfg, job.company, job.title, job.external_id or "", job_id=job.id)
    tailoring.remember_job_run(job.id, folder, target, packet)
    tailoring._progress(folder, stage=0, done=False, error=None, note="")
    try:
        result = tailoring.run(folder, resume, f"{job.title}\n\n{job.description or ''}", None, cfg,
                               company=job.company)
    except Exception as exc:
        tailoring._progress(folder, done=True, error={"stage": "", "file": None, "message": str(exc)[:300]})
        raise
    ats = (result.get("before_after") or {}).get("ats", [None, result["score"]])[1]
    tailoring.finish_job_run(job.id, folder, target, ats, packet)
    tailoring._progress(folder, stage=len(tailoring.STAGES), done=True, error=None, note="")
    return {"run": folder.name, "ats": ats}


def prepare_one(cfg, job, applicant, say=print) -> dict:
    from ml.resume.packet import LETTER_FILE, build_packet
    entry = {"job_id": job.id, "company": job.company, "title": job.title, "source": job.source,
             "score": round((job.ats_match_score or 0) * 100), "state": "ready", "notes": [],
             "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    packet = build_packet(cfg, job)
    entry["folder"] = str(packet.path)
    try:
        entry["tailored"] = _tailor(cfg, job)
        build_packet(cfg, job)                        # adopt the tailored resume into the folder
    except Exception as exc:
        entry["notes"].append(f"Tailoring did not finish ({str(exc)[:120]}); your own resume will be sent.")
    if not (packet.path / LETTER_FILE).exists():
        try:
            from ml.resume.pipeline import write_letter
            write_letter(cfg, job.id)
        except Exception as exc:
            entry["notes"].append(f"No cover letter ({str(exc)[:120]}).")
    entry["letter"] = (packet.path / LETTER_FILE).exists()
    applicant.job = job                           # so desired salary is answered by the rule
    try:
        entry["questions"] = open_questions(job, applicant)
    finally:
        applicant.job = None
    asked = len(entry["questions"])
    say(f"  ready: {job.company[:30]} — {job.title[:50]}"
        + (f" · {asked} question(s) for you" if asked else ""))
    return entry


def prepare(cfg, top: int | None = None, min_fit: float | None = None, say=print) -> list[dict]:
    """Get the day's top matches ready. Already-prepared and applied jobs are skipped."""
    from backend.apply.profile import load as load_profile
    from backend.apply.queue import build_queue
    from backend.apply.runner import _last_applied_by_company
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session

    top = int(top if top is not None else cfg.get("apply.auto.per_day", 10))
    min_fit = float(min_fit if min_fit is not None else cfg.get("apply.auto.min_fit", 0.80))
    applicant = load_profile(PROJECT_ROOT / "backend" / "config" / "applicant.yaml")
    data = load()
    with get_session() as session:
        from backend.core.eligibility import eligible
        auth = getattr(applicant, "authorisation", {}) or {}
        # Not a job the person can take (clearance, citizenship, sponsorship): not prepared.
        jobs = [j for j in session.query(Job).filter(Job.is_open.is_(True)).all()
                if str(j.id) not in data and j.score_basis == "full" and eligible(j, auth)[0]]
        queue = build_queue(jobs, limit=top, min_score=min_fit,
                            last_applied=_last_applied_by_company(session))
        chosen = [session.get(Job, c.job_id) for c in queue]
        for job in chosen:
            session.expunge(job)
    if not chosen:
        say(f"Nothing new to prepare: no open Greenhouse or Workday posting at {min_fit:.0%} fit or more.")
        return []
    say(f"Preparing {len(chosen)} application(s): tailoring, cover letter, questions…")
    done = []
    for job in chosen:
        try:
            entry = prepare_one(cfg, job, applicant, say)
        except Exception as exc:
            say(f"  skipped {job.company} — {job.title[:40]}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        data = load()
        data[str(job.id)] = entry
        _save(data)
        done.append(entry)
    return done
