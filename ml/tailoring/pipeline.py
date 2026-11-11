"""Resume Tailoring, start to finish: the six stages the page shows.

    1 reading documents            2 extracting resume information
    3 identifying requirements     4 matching evidence
    5 creating the draft           6 checking the final files

Each run lives in data/tailoring/runs/<id>/: the uploaded files (never
modified), the tailored DOCX and PDF, page images, and result.json, which
holds everything the results tabs show. `progress(stage, note)` is called as
each stage starts so the page can follow along.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import asdict
from pathlib import Path

log = logging.getLogger(__name__)

from backend.config.loader import PROJECT_ROOT

RUNS = PROJECT_ROOT / "data" / "tailoring" / "runs"
# The longest the finished resume waits for its skim test (advice, never a blocker).
SKIM_WAIT_S = 120

STAGES = ("reading documents", "extracting resume information", "identifying requirements",
          "matching evidence", "creating the draft", "checking the final files")


class StageError(Exception):
    """A stage that could not finish, with what the person should do."""

    def __init__(self, stage: str, message: str, file: str | None = None):
        super().__init__(message)
        self.stage, self.file = stage, file


def new_run() -> Path:
    folder = RUNS / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def run(folder: Path, resume_file: Path, jd_text: str = "", jd_file: Path | None = None, cfg=None,
        progress=lambda stage, note="": None, use_model: bool = True, company: str | None = None,
        length: str | None = None, template: str | None = None, look: dict | None = None) -> dict:
    from ml.tailoring import documents, draft as drafting, matching, requirements, versions
    from ml.tailoring.reader import ReadError, read_document
    from ml.tailoring.structure import build

    # 1 -- reading documents
    progress(STAGES[0])
    try:
        extracted = read_document(resume_file)
    except ReadError as exc:
        raise StageError(STAGES[0], str(exc), "resume") from exc
    if jd_file is not None:
        try:
            jd_text = read_document(jd_file).text
        except ReadError as exc:
            raise StageError(STAGES[0], str(exc), "job") from exc
    if len((jd_text or "").split()) < 15:
        raise StageError(STAGES[0], "The job description is empty or too short to analyse. Paste the "
                                    "full posting or upload it as a file.", "job")
    # Kept with the run: the keyword tracker reads it again after every edit.
    (folder / "inputs").mkdir(parents=True, exist_ok=True)
    (folder / "inputs" / "job.txt").write_text(jd_text)
    if company:
        (folder / "inputs" / "company.txt").write_text(company)

    # 2 -- extracting resume information
    progress(STAGES[1])
    model = build(extracted)
    if not model.facts or not model.entries:
        raise StageError(STAGES[1], "No work history could be found in the resume. Check that it is the "
                                    "right file, or upload a .docx.", "resume")
    # How long: the person's choice, or by their experience (ml/resume/fit.py).
    from ml.resume.fit import page_range, years_of_experience
    years = years_of_experience(model.entries)
    fewest, most = page_range(length, years, cfg)

    # The recruiter's read of the original (review.py) runs beside stages 3-4:
    # its own model call, so it costs no extra wait. Advice; never a blocker.
    from concurrent.futures import ThreadPoolExecutor
    from ml.tailoring import review as reviewing
    pool = ThreadPoolExecutor(max_workers=1)
    pending_review = pool.submit(reviewing.recruiter_review, extracted.text, jd_text, company, cfg) \
        if use_model else None

    # 3 -- identifying requirements
    progress(STAGES[2])
    reqs, req_notes = requirements.extract(jd_text, cfg, use_model=use_model)
    if not reqs:
        raise StageError(STAGES[2], "No requirements could be found in the job description.", "job")

    # 4 -- matching evidence
    progress(STAGES[3], f"{len(reqs)} requirements")
    report = matching.match(reqs, model, cfg, use_model=use_model)
    version_checks = versions.check(model, jd_text)

    # 5 -- creating the draft (from the uploaded file; it is never modified)
    progress(STAGES[4])
    recruiter, review_note = {}, ""
    if pending_review is not None:
        try:
            recruiter = pending_review.result(timeout=600)
        except __import__('ml.resume.cost', fromlist=['SpendCapReached']).SpendCapReached:
            raise                      # the recruiter's read hit the hard limit: the run stops (A1.1)
        except Exception as exc:
            review_note = f"The recruiter review could not be made ({str(exc)[:100]})."
    pool.shutdown(wait=False)
    review_extra = reviewing.guidance(recruiter)
    if resume_file.suffix.lower() not in (".docx", ".pdf", ".txt", ".md"):
        raise StageError(STAGES[4], "Unsupported resume file.", "resume")
    # Drafted again until it matches the posting's keywords well (rounds.py);
    # without a model every round would be the same, so one is enough.
    from ml.tailoring import rounds
    draft, ats, rounds_used = rounds.best_draft(
        lambda extra, focus: drafting.create(resume_file, jd_text, report, cfg, use_model=use_model,
                                             extra="\n\n".join(x for x in (review_extra, extra) if x),
                                             focus=focus, max_pages=most, min_pages=fewest),
        jd_text, progress=lambda note: progress(STAGES[4], note), rounds=rounds.ROUNDS if use_model else 1,
        company=company, refit=lambda d, focus: drafting.refit(d, jd_text, report, focus))
    # A header that says "LinkedIn" as a linked word keeps the link: the
    # writer draws a LinkedIn address as that word, linked.
    linkedin = next((l for l in model.contact.get("links", []) if "linkedin.com" in l.lower()), None)
    if linkedin:
        draft.header = [h.replace("LinkedIn", linkedin) if "LinkedIn" in h and "linkedin.com" not in h.lower()
                        else h for h in draft.header]

    # The posting's keywords, bold where the resume says them.
    from ml.resume import emphasis
    draft.result.emphasis = emphasis.terms(jd_text, extracted.text, company)
    # The look: the page's choice, else Settings (ml/resume/style.py).
    draft.result.template = template or (cfg.get("resume.template", "") if cfg is not None else "") or ""
    from ml.resume.style import clean_look
    draft.result.look = clean_look(look)

    # 6 -- checking the final files
    progress(STAGES[5])
    rendered = documents.produce(draft, folder, model=model, jd_text=jd_text)
    draft.changes = drafting.changes(draft.base, draft.result, draft.header)
    tailored_text = read_document(rendered.docx).text
    skim, skim_note = {}, ""
    if use_model:
        progress(STAGES[5], "a recruiter's six-second read of the finished resume")
        skim, skim_note = skim_within(tailored_text, jd_text, cfg)
    from pypdf import PdfReader
    before_after = {
        "ats": [ats["before"], ats["after"]],
        "requirements": [round(rounds.requirements_shown(reqs, resume_file), 1),
                         round(rounds.requirements_shown(reqs, rendered.docx), 1)],
        "rewritten": sum(1 for c in draft.changes if c["kind"] == "Rewrote a bullet"),
        "rounds": rounds_used,
        "target": max(rounds.TARGET, ats["before"] or 0),
        "pages": [rounds.pages_of_original(draft), len(PdfReader(str(rendered.pdf)).pages)],
    }

    result = {
        "folder": str(folder),
        "files": {"docx": rendered.docx.name, "pdf": rendered.pdf.name},
        "pages": {k: [Path(p).name for p in v] for k, v in rendered.pages.items()},
        "score": report.score,
        "before_after": before_after,
        "length": {"choice": length or (cfg.get("resume.length", "auto") if cfg is not None else "auto"),
                   "years": years, "pages": [fewest, most]},
        "score_note": ("The builder's estimate: required items weigh 3 and preferred 1; a direct match "
                       "counts 1.0 and a related one 0.5. 100% means every identified requirement is "
                       "directly shown under these rules — not a guaranteed result from an employer's ATS."),
        "report": report.to_dict(),
        "alignment": report.alignment,
        "versions": versions.as_dicts(version_checks),
        "changes": draft.changes,
        "draft_note": draft.problem,
        "draft_state": getattr(draft, "state", "tailored"),
        "gaps": [asdict(m) for m in report.gaps],
        "review": {"recruiter": recruiter, "skim": skim,
                   "needs_numbers": reviewing.needs_numbers(_bullets(rendered.docx)),
                   "notes": [n for n in (review_note, skim_note) if n]},
        "checks": [asdict(c) for c in rendered.checks],
        "checks_ok": rendered.ok,
        "word_rendered": rendered.word,
        "attempts": rendered.attempts,
        "extraction": {"warnings": model.warnings, "ocr": model.ocr,
                       "text": extracted.text if (model.warnings or model.ocr) else ""},
        "notes": req_notes + report.notes + [f"Title used for this posting: {t} (from your titles list)."
                                             for t in draft.titles],
        "draft": _draft_json(draft),
        "title": draft.title,
    }
    _write_json(folder / "result.json", result, indent=1, default=str)
    return result


def skim_within(tailored_text: str, jd_text: str, cfg, wait_s: float | None = None) -> tuple[dict, str]:
    """(skim, note): the six-second skim test of a finished resume, waited for a little, never long.

    It is advice on a resume that is already made. A busy plan once timed out and fell back to a
    slower model, and the bar stood at 99% for thirteen minutes while the resume sat finished.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as StillRunning
    from ml.tailoring import review as reviewing
    wait_s = SKIM_WAIT_S if wait_s is None else wait_s
    pool = ThreadPoolExecutor(max_workers=1)
    pending = pool.submit(reviewing.skim_test, tailored_text, jd_text, cfg)
    try:
        return pending.result(timeout=wait_s), ""
    except StillRunning:
        return {}, (f"The six-second skim test was still running after {max(1, round(wait_s / 60))} minute(s), "
                    "so the resume was finished without it.")
    except Exception as exc:
        return {}, f"The skim test could not be made ({str(exc)[:100]})."
    finally:
        pool.shutdown(wait=False)


def _bullets(docx: Path) -> list[str]:
    """The tailored resume's experience lines (not the role headers), as they will be read."""
    from ml.tailoring.reader import read_document
    from ml.tailoring.structure import build
    try:
        model = build(read_document(docx))
    except Exception:
        return []
    facts = {f.id: f.text for f in model.facts}
    return [facts[i] for e in model.entries if e.section == "experience"
            for i in e.facts if i in facts and len(facts[i].split()) >= 6]


def _draft_json(draft) -> dict:
    keep = ("summary", "bullets", "skills_order", "omitted", "gaps", "fitted", "overrides", "emphasis", "template",
            "look")
    return {"result": {k: getattr(draft.result, k, None) for k in keep}, "header": draft.header}


def save_inputs(folder: Path, resume_upload, jd_upload=None) -> tuple[Path, Path | None]:
    """Copy the uploaded files into the run; the originals are never touched again."""
    inputs = folder / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    resume = None
    if resume_upload is not None:
        resume = inputs / ("resume" + Path(resume_upload.filename or "resume.docx").suffix.lower())
        resume_upload.save(resume)
    jd = None
    if jd_upload is not None and jd_upload.filename:
        jd = inputs / ("job" + Path(jd_upload.filename).suffix.lower())
        jd_upload.save(jd)
    return resume, jd


def save_saved_resume(folder: Path, source: Path) -> Path:
    """The person's saved resume for a run started from a job page.

    With lines added from job pages it is written out with them under their
    jobs, so the match and the draft count what the person has added.
    """
    from ml.resume.additions import load as load_additions
    inputs = folder / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    if not load_additions():
        resume = inputs / ("resume" + source.suffix.lower())
        shutil.copy2(source, resume)
        return resume
    from ml.resume.additions import apply
    from ml.resume.parser import parse_resume
    from ml.resume.tailor import TailorResult
    from ml.resume.writer import write_tailored_resume
    resume = inputs / "resume.docx"
    write_tailored_resume(apply(parse_resume(source)), TailorResult(summary=None), resume)
    return resume


# -- runs the page drives ------------------------------------------------------------

def folder_of(run_id: str) -> Path | None:
    """The run's folder, or None for an id that is not a run (never a path outside RUNS)."""
    import re
    if not re.fullmatch(r"[\w-]{6,64}", run_id or ""):
        return None
    folder = RUNS / run_id
    return folder if folder.is_dir() else None


def _progress(folder: Path, **state) -> None:
    path = folder / "progress.json"
    try:
        current = json.loads(path.read_text())
    except (OSError, ValueError):
        current = {"stages": list(STAGES), "stage": 0, "done": False, "error": None, "note": ""}
    current.update(state)
    current["pid"] = os.getpid()           # whose run this is, so a run that died can be told apart
    path.write_text(json.dumps(current))


STALE_S = 30 * 60


def interrupted(folder: Path, state: dict) -> bool:
    """A run the page would wait on forever: not done, and its process is gone.

    Runs go in a thread of the app (or of a daily job). Restarting the app ends the thread
    mid-round and nothing writes "done", so the bar stood at its last step. A run from before
    the pid was kept counts as gone after half an hour without a step.
    """
    if state.get("done"):
        return False
    pid = state.get("pid")
    if pid:
        try:
            os.kill(int(pid), 0)
            return False
        except ProcessLookupError:
            return True
        except (PermissionError, ValueError, OSError):
            return False
    try:
        return time.time() - (folder / "progress.json").stat().st_mtime > STALE_S
    except OSError:
        return False


def start(folder: Path, resume_file: Path, jd_text: str, jd_file: Path | None, cfg,
          after=None, company: str | None = None, length: str | None = None,
          template: str | None = None, look: dict | None = None) -> None:
    """Run in the background; the page follows progress.json.

    `after(result)` runs once the files are made and before the run reads as
    done, so a job page finds its resume in place when the bar reaches 100%.
    """
    import threading

    def follow(stage, note=""):
        _progress(folder, stage=STAGES.index(stage), note=note)

    def work():
        try:
            result = run(folder, resume_file, jd_text, jd_file, cfg, progress=follow, company=company,
                         length=length, template=template, look=look)
            if after is not None:
                after(result)
            _progress(folder, stage=len(STAGES), done=True, note="")
        except StageError as exc:
            _progress(folder, done=True, error={"stage": exc.stage, "message": str(exc), "file": exc.file})
        except Exception as exc:              # never leave the page waiting forever
            _progress(folder, done=True, error={"stage": "", "file": None,
                                                "message": f"Something went wrong: {type(exc).__name__}: {exc}"})

    _progress(folder, stage=0, done=False, error=None, note="")
    threading.Thread(target=work, daemon=True).start()


def load_draft(folder: Path):
    from ml.resume.parser import parse_resume
    from ml.resume.tailor import TailorResult
    from ml.tailoring.draft import Draft
    data = json.loads((folder / "result.json").read_text())
    resume = next((folder / "inputs").glob("resume.*"))
    base = parse_resume(resume)
    saved = data["draft"]["result"]
    result = TailorResult(summary=saved.get("summary"))
    for key, value in saved.items():
        if key != "summary" and value is not None:
            setattr(result, key, value)
    # The header is drawn fresh, not as saved: contact details the person
    # updated since belong on the next copy of this resume too.
    from ml.tailoring.draft import header_of
    return Draft(base, result, header_of(base) or data["draft"]["header"], data.get("title", "")), data


def editable_lines(folder: Path) -> list[dict]:
    from ml.resume.writer import plan_document
    draft, _ = load_draft(folder)
    return [{"kind": k, "text": t} for k, t in plan_document(draft.base, draft.result, draft.header)]


def _emphasis_for(folder: Path, draft) -> list[str]:
    """Keywords to bold for a run saved before they were kept: from its job's posting."""
    job = job_of_run(folder)
    if not job:
        return []
    try:
        import sqlite3
        from backend.config.loader import PROJECT_ROOT as root
        from ml.resume import emphasis
        row = sqlite3.connect(f"file:{root / 'data' / 'jobs.db'}?mode=ro", uri=True).execute(
            "select title, description, company from jobs where id = ?", (job["job_id"],)).fetchone()
        if not row:
            return []
        return emphasis.terms(f"{row[0]}\n\n{row[1] or ''}", draft.base.text(), row[2])
    except Exception as exc:
        log.info("Keywords to bold for %s: %s", folder.name, exc)
        return []


# -- keywords ------------------------------------------------------------------------

def run_posting(folder: Path) -> tuple[str, str | None]:
    """(posting text, company) for a run: saved with it, else its saved job's."""
    inputs = folder / "inputs"
    company = (inputs / "company.txt").read_text().strip() if (inputs / "company.txt").exists() else None
    if (inputs / "job.txt").exists():
        return (inputs / "job.txt").read_text(), company
    for other in inputs.glob("job.*"):
        try:
            from ml.tailoring.reader import read_document
            return read_document(other).text, company
        except Exception:
            pass
    job = job_of_run(folder)
    if job:
        try:
            import sqlite3
            from backend.config.loader import PROJECT_ROOT as root
            row = sqlite3.connect(f"file:{root / 'data' / 'jobs.db'}?mode=ro", uri=True).execute(
                "select title, description, company from jobs where id = ?", (job["job_id"],)).fetchone()
            if row:
                return f"{row[0]}\n\n{row[1] or ''}", company or row[2]
        except Exception as exc:
            log.info("Posting for %s: %s", folder.name, exc)
    return "", company


def _texts(folder: Path) -> tuple[str, str]:
    """(master resume text, tailored resume text) for a run."""
    from ml.tailoring.reader import read_document
    master = next((folder / "inputs").glob("resume.*"))
    return read_document(master).text, read_document(folder / "tailored-resume.docx").text


def keywords_for(folder: Path) -> dict:
    from ml.tailoring import keywords
    jd, company = run_posting(folder)
    if not jd:
        return {"keywords": [], "counts": {}, "coverage": None,
                "note": "This run's job posting was not kept, so its keywords cannot be listed."}
    try:
        master, tailored = _texts(folder)
    except (StopIteration, FileNotFoundError):
        return {"keywords": [], "counts": {}, "coverage": None,
                "note": "This run did not keep a copy of the resume it started from, so lost "
                        "keywords cannot be found. Tailor again from this page to see them."}
    return keywords.track(master, tailored, jd, company, keywords.person_fields())


def check_keyword_edits(folder: Path, edits: dict[str, str]) -> dict:
    from ml.tailoring import keywords
    jd, company = run_posting(folder)
    if not jd:
        return {"lost": [], "gained": [], "by_line": {}}
    return keywords.check_edits(_texts(folder)[1], edits, jd, company, keywords.person_fields())


def _rescore(folder: Path, data: dict) -> None:
    """The ATS match of the resume as it now is, after the person's change."""
    jd, company = run_posting(folder)
    if not jd:
        return
    from ml.resume.ats import compare
    master, tailored = _texts(folder)
    ats = compare(master, tailored, jd, company=company)
    ba = data.setdefault("before_after", {})
    ba["ats"] = [ats["before"], ats["after"]]
    ba["pages"] = [(ba.get("pages") or [None])[0], len(data.get("pages", {}).get("pdf", []))]


def restore_line(folder: Path, line: str) -> dict:
    """Put one of the master's lines back: undo the cut, or the rewrite, that removed it.

    For a keyword the tailoring lost. The line is the person's own, word for
    word, so nothing new is claimed; the resume may grow by a line.
    """
    from ml.resume.parser import strip_bullet
    from ml.tailoring import documents
    draft, data = load_draft(folder)
    want = strip_bullet(line.strip())
    same = lambda text: strip_bullet((text or "").strip()) == want
    omitted = [e for e in (draft.result.omitted or [])
               if not (isinstance(e, dict) and same(e.get("original")))]
    bullets = [b for b in (draft.result.bullets or []) if not same(b.get("original"))]
    if len(omitted) == len(draft.result.omitted or []) and len(bullets) == len(draft.result.bullets or []):
        raise ValueError("That line is not one the tailoring removed or rewrote.")
    draft.result.omitted, draft.result.bullets = omitted, bullets
    rendered = documents.produce(draft, folder, attempts=1)
    data["files"] = {"docx": rendered.docx.name, "pdf": rendered.pdf.name}
    data["pages"] = {k: [Path(p).name for p in v] for k, v in rendered.pages.items()}
    data["checks"] = [asdict(c) for c in rendered.checks]
    data["checks_ok"] = rendered.ok
    data["draft"] = _draft_json(draft)
    data.setdefault("restored", []).append(line.strip())
    _rescore(folder, data)
    _write_json(folder / "result.json", data, indent=1, default=str)
    _place_if_job(folder, data)
    return data


def roadmap_for(folder: Path, cfg=None) -> dict:
    """Build the skill roadmap for a run's gaps and keep it with the run."""
    from datetime import datetime
    from ml.tailoring import roadmap
    data = json.loads((folder / "result.json").read_text())
    jd, _ = run_posting(folder)
    if not jd:
        raise ValueError("This run's job posting was not kept, so there is nothing to plan against.")
    try:
        master = _texts(folder)[0]
    except (StopIteration, FileNotFoundError):
        master = data.get("extraction", {}).get("text") or ""
    missing = ((data.get("review") or {}).get("recruiter") or {}).get("missing_keywords") or []
    gaps = roadmap.gaps_of((data.get("report") or {}).get("matches") or [], missing)
    if not gaps:
        raise ValueError("No gaps: the resume shows everything this posting asks for.")
    if cfg is None:
        from backend.config.loader import load_config
        cfg = load_config()
    skills = roadmap.build(master, jd, gaps, cfg)
    data["roadmap"] = {"skills": skills, "made": datetime.now().isoformat(timespec="minutes")}
    _write_json(folder / "result.json", data, indent=1, default=str)
    return data["roadmap"]


def _place_if_job(folder: Path, data: dict) -> None:
    """A job's run, once the person changes it, is what that job's application attaches: published as a
    new version of its resume (Track A1), so the edited files are what goes -- and, being different files,
    they wait for the person's approval again."""
    job = job_of_run(folder)
    if job and job.get("docx"):
        publish_edit(int(job["job_id"]), folder, note="edited by you")
        data["placed_for_job"] = True


def change_template(folder: Path, template: str, look: dict | None = None) -> dict:
    """Draw the same resume in another template. No model; the words do not change.

    The page count can: compact is shorter, modern a little longer. Reported,
    and warned about when it passes the length the run was made for.
    """
    from ml.resume.style import TEMPLATES, clean_look
    from ml.tailoring import documents
    if template not in TEMPLATES:
        raise ValueError(f"No template called {template!r}.")
    draft, data = load_draft(folder)
    draft.result.template = template
    if look is not None:
        draft.result.look = clean_look(look)
    rendered = documents.produce(draft, folder, attempts=1)
    data["files"] = {"docx": rendered.docx.name, "pdf": rendered.pdf.name}
    data["pages"] = {k: [Path(p).name for p in v] for k, v in rendered.pages.items()}
    data["checks"] = [asdict(c) for c in rendered.checks]
    data["checks_ok"] = rendered.ok
    data["draft"] = _draft_json(draft)
    _rescore(folder, data)
    most = ((data.get("length") or {}).get("pages") or [None, None])[1]
    pages = len(data["pages"].get("pdf", []))
    data["template_note"] = (f"In this template the resume runs to {pages} pages, past the {most} it was "
                             f"made for. Choose a shorter length or the Compact template."
                             if most and pages > most else "")
    _write_json(folder / "result.json", data, indent=1, default=str)
    _place_if_job(folder, data)
    return data


def apply_edits(folder: Path, edits: dict[str, str]) -> dict:
    """The person's edits to the draft: saved, the files written again, and checked again."""
    from ml.tailoring import documents
    draft, data = load_draft(folder)
    overrides = dict(draft.result.overrides or {})
    for before, after in edits.items():
        # Edits chain: a line edited twice maps from the original text.
        source = next((k for k, v in overrides.items() if v == before), before)
        overrides[source] = (after or "").strip()
    draft.result.overrides = overrides
    if not getattr(draft.result, "emphasis", None):
        draft.result.emphasis = _emphasis_for(folder, draft)
    rendered = documents.produce(draft, folder, attempts=1)
    data["files"] = {"docx": rendered.docx.name, "pdf": rendered.pdf.name}
    data["pages"] = {k: [Path(p).name for p in v] for k, v in rendered.pages.items()}
    data["checks"] = [asdict(c) for c in rendered.checks]
    data["checks_ok"] = rendered.ok
    data["draft"] = _draft_json(draft)
    data["edited"] = [{"before": k, "after": v} for k, v in overrides.items()]
    # The score shown is the score of the resume as edited, not as first drafted.
    try:
        _rescore(folder, data)
    except Exception as exc:
        log.info("Rescore after edit, %s: %s", folder.name, exc)
    _write_json(folder / "result.json", data, indent=1, default=str)
    # The person's edits are the resume they mean to send: a job's run, once
    # edited, becomes what the application attaches, whatever its score.
    # Without this the edits stayed in the run and an older file went out.
    _place_if_job(folder, data)
    return data


SCORES = PROJECT_ROOT / "data" / "tailoring" / "scores"


def job_score(job_id: int, resume_file: Path, jd_text: str, cfg=None, use_model: bool = False) -> dict:
    """The requirement-based score for a saved job, against the person's resume.

    Saved per job and recomputed only when the resume or the posting changes.
    Rules only by default (fast, no model); the Tailor page's full run uses
    the model as well.
    """
    import hashlib

    from ml.tailoring import matching, requirements
    from ml.tailoring.reader import read_document
    from ml.tailoring.structure import build

    key = hashlib.sha256(resume_file.read_bytes() + (jd_text or "").encode()).hexdigest()[:16]
    saved = SCORES / f"{job_id}.json"
    if saved.exists():
        try:
            old = json.loads(saved.read_text())
            if old.get("key") == key:
                return old
        except ValueError:
            pass
    if len((jd_text or "").split()) < 15:
        return {"key": key, "score": None, "note": "The posting has no description to score against."}
    model = build(read_document(resume_file))
    reqs, _ = requirements.extract(jd_text, cfg, use_model=use_model)
    if len(reqs) < 5:
        # A score from one or two items says little: alert snippets carry no real list.
        out = {"key": key, "score": None, "note": "Too few requirements in this posting to score."}
    else:
        report = matching.match(reqs, model, cfg, use_model=use_model)
        count = {s: sum(m.status == s for m in report.matches) for s in matching.VALUE}
        out = {"key": key, "score": report.score, "counts": count,
               "gaps": [m.requirement["original"] for m in report.gaps
                        if m.requirement["priority"] == "required"][:8],
               "note": "Estimate: required items weigh 3, preferred 1; direct counts 1.0, related 0.5. "
                       "Not an employer's ATS result."}
    SCORES.mkdir(parents=True, exist_ok=True)
    saved.write_text(json.dumps(out, indent=1))
    return out


# -- a run for one saved job ----------------------------------------------------------

JOBS = PROJECT_ROOT / "data" / "tailoring" / "jobs"


def place_for_job(folder: Path, target_docx: Path, packet: Path | None = None) -> None:
    """The run's files become the job's tailored resume, where the job page,
    its keyword check and the application packet look for them -- and, when
    the job has a folder, in that folder too, so it holds what was sent."""
    target_docx.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(folder / "tailored-resume.docx", target_docx)
    shutil.copy2(folder / "tailored-resume.pdf", target_docx.with_suffix(".pdf"))
    if packet is not None and packet.is_dir():
        from ml.resume.packet import put_resume      # under the person's name: Jordan_Example_Resume_Acme
        put_resume(packet, folder / "tailored-resume.docx", folder / "tailored-resume.pdf")


def _write_json(path: Path, data, indent=None, default=None) -> None:
    """Write a record whole or not at all: a reader never finds half of one (Track A1)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    temp.write_text(json.dumps(data, indent=indent, default=default))
    os.replace(temp, path)


def _job_record(job_id: int) -> dict:
    try:
        return json.loads((JOBS / f"{job_id}.json").read_text())
    except (OSError, ValueError):
        return {}


def remember_job_run(job_id: int, folder: Path, target_docx: Path, packet: Path | None = None) -> None:
    JOBS.mkdir(parents=True, exist_ok=True)
    record = _job_record(job_id)
    record["run"] = folder.name                      # the latest, which the page follows
    _write_json(JOBS / f"{job_id}.json", record)
    (folder / "job.json").write_text(json.dumps({"job_id": job_id, "docx": str(target_docx),
                                                 "packet": str(packet) if packet else None}))


def source_of(folder: Path | None) -> str | None:
    """What a run was tailored from -- the master with the lines added to it -- as a fingerprint
    of its words, so two runs from the same resume match however the file was written."""
    import hashlib
    import re
    from ml.tailoring.reader import read_document
    files = sorted((folder / "inputs").glob("resume.*")) if folder else []
    if not files:
        return None
    try:
        text = read_document(files[0]).text
    except Exception:
        return None
    return hashlib.sha1(re.sub(r"\s+", " ", text).strip().encode()).hexdigest()[:16]


def finish_job_run(job_id: int, folder: Path, target_docx: Path, score: float,
                   packet: Path | None = None) -> bool:
    """Make this run the job's resume only if it matches at least as well as the best so far.

    `score` is the tailored resume's ATS keyword match -- the outcome. The
    requirement score was used first, but it measures the original resume
    and moved a point between runs by chance, so a better resume was passed
    over. A record from then (no "measure") is replaced, not compared.
    Returns whether it did. A rewrite that does worse never replaces a better one.
    """
    record = _job_record(job_id)
    best = record.get("best_score") if record.get("measure") == "ats" else None
    record["measure"] = "ats"
    # Scores compare only rewrites of the same resume. A best drawn from an older master -- one
    # since corrected, or before a line was added to it -- loses to the newer one whatever it scored:
    # an 82% rewrite of text read wrongly from a PDF ("Cloud Bui ld") kept beating a clean 80%.
    source = source_of(folder)
    best_folder = folder_of(record["best"]) if record.get("best") else None
    best_source = record.get("best_source") or (source_of(best_folder) if best_folder else None)
    newer_resume = bool(source and best_source and source != best_source)
    better = best is None or score >= best or not target_docx.exists() or newer_resume
    if better:
        place_for_job(folder, target_docx, packet)
        record.update(best=folder.name, best_score=score, best_source=source)
        redo_done(job_id)
    record["run"] = folder.name
    JOBS.mkdir(parents=True, exist_ok=True)
    _write_json(JOBS / f"{job_id}.json", record)
    return better


# Jobs whose tailored resume must be made again before it is sent, and why: {"11127": "made from…"}.
# Beside JOBS, not in it: everything in JOBS is one job's record.
REDO = PROJECT_ROOT / "data" / "tailoring" / "redo.json"


def needs_redo(job_id: int) -> str | None:
    """Why this job's tailored resume is to be made again before it goes out, or None."""
    try:
        reason = json.loads(REDO.read_text()).get(str(job_id))
    except (OSError, ValueError, AttributeError):
        return None
    return str(reason) if reason else None


def mark_redo(job_ids, reason: str) -> None:
    """Set these jobs' tailored resumes to be made again when each is next tailored or applied to."""
    try:
        marked = json.loads(REDO.read_text())
    except (OSError, ValueError):
        marked = {}
    marked.update({str(j): reason for j in job_ids})
    REDO.parent.mkdir(parents=True, exist_ok=True)
    _write_json(REDO, marked, indent=1)


def redo_done(job_id: int) -> None:
    try:
        marked = json.loads(REDO.read_text())
    except (OSError, ValueError):
        return
    if marked.pop(str(job_id), None) is not None:
        _write_json(REDO, marked, indent=1)


def job_run(job_id: int) -> str | None:
    return _job_record(job_id).get("run")


def job_best(job_id: int) -> tuple[str | None, float | None]:
    """The job's best FINISHED run and its score, or (None, None). Until Nov 2026 a job with no best yet
    answered with its latest run -- one still running, crashed or failed -- and Prepare now took that
    for done (Track A1). The latest run, finished or not, is `job_run`."""
    record = _job_record(job_id)
    best = record.get("best")
    folder = folder_of(best) if best else None
    if folder is None or not (folder / "result.json").is_file():
        return None, None
    return best, record.get("best_score")


def job_of_run(folder: Path) -> dict | None:
    try:
        return json.loads((folder / "job.json").read_text())
    except (OSError, ValueError):
        return None


# -- one canonical preparation for a job's resume (Track A1, Nov 2026) --------------------------------------
#
# Every normal way a job gets its resume -- Prepare now, Prepare these, Apply to these, a morning pick's
# Prepare, the job page's Tailor, the assistant -- comes here. Here, and only here:
#
#   * one preparation per job at a time (ml/tailoring/coordination: a claim, kept alive, fenced);
#   * the same inputs are not tailored twice (`repeat_key`): the resume and the lines added to it, the
#     titles kept for each job, the posting, the template and length, the writer, and these rules;
#   * only a TAILORED draft is published. A draft the writer could not write, or whose rewrite the
#     fabrication guard rejected, is the resume's own words: never published as if tailored. The job
#     fails with that reason, and the person may choose their own resume instead (`use_own_resume`);
#   * the DOCX and the PDF are published together, as a new version, and only while the claim is held;
#   * cancelling stops it at the next stage; nothing made after a cancel is published;
#   * a writer that bills is used only with the person's yes and a spending limit, which every call of
#     the preparation counts against (ml/resume/cost.budget).
#
# The older engine (ml/resume/pipeline.tailor_jobs) is reached only by `main.py tailor --legacy`.

RULES_VERSION = "a1.1-2026-11-08"
OWN = PROJECT_ROOT / "data" / "tailoring" / "own"          # the person's own resume, as chosen for a job


class TailoringFailed(Exception):
    """Why a job's resume was not made, as {kind, message}: shown to the person, never hidden behind a
    substitute. Kinds: no_resume, no_posting, paid_not_allowed, no_spend_limit, busy, claim_unreadable,
    claim_lost, cancelled, spend_limit, stage, not_tailored, not_published, error, earlier_outdated, no_earlier,
    writer_not_approved, choice_not_saved (A1.1.1)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


class JobBusy(ValueError):
    """The job's resume is being made or changed elsewhere right now: try again when it is done."""


class TailoringCancelled(TailoringFailed):
    def __init__(self):
        super().__init__("cancelled", "Cancelled by you: nothing was made for it.")


from dataclasses import dataclass as _dataclass   # noqa: E402 -- kept beside what uses it


@_dataclass
class PreparedResume:
    publication: object               # coordination.Publication
    reused: bool
    note: str
    run_id: str | None
    score: float | None = None

    @property
    def pdf(self) -> Path:
        return self.publication.pdf

    @property
    def docx(self) -> Path:
        return self.publication.docx


def _sha_file(path: Path | None) -> str:
    import hashlib
    if path is None or not Path(path).is_file():
        return ""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _contact_fingerprint() -> str:
    """The contact line the profile puts on every tailored resume (ml/resume/writer.applicant_contact_line)."""
    import hashlib
    try:
        from ml.resume.writer import applicant_contact_line
        line = applicant_contact_line()
        text = "" if line is None else f"{line(False)}|{line(True)}"
    except Exception:                         # noqa: BLE001 -- no profile: the resume's own line is used
        text = ""
    return hashlib.sha256(text.encode()).hexdigest()


def repeat_key(cfg, job, resume_file: Path, *, source: str = "tailored", template: str | None = None,
               look: dict | None = None) -> str:
    """What makes two preparations of a job the same: the same inputs, the same rules, the same writer.
    A change to any of them is a new preparation, and its new files wait for approval again. The inputs:
    the resume, the lines added to it, the titles kept, the profile's contact line, the posting and its
    requirements, the company, the template (chosen, or the configured default) and its look, the length
    and page limits, the writer and its model, and these rules (A1.1 added the template default, the look,
    the contact line and the requirements)."""
    import hashlib
    from ml.resume import llm
    from ml.resume.additions import ADDITIONS_PATH
    from ml.resume.titles import TITLES_PATH as titles
    get = (lambda k, d=None: cfg.get(k, d)) if cfg is not None else (lambda k, d=None: d)
    parts = {
        "rules": RULES_VERSION, "source": source, "job": int(job.id),
        "resume": _sha_file(resume_file), "additions": _sha_file(ADDITIONS_PATH), "titles": _sha_file(titles),
        "posting": hashlib.sha256(f"{job.title}\n\n{job.description or ''}".encode()).hexdigest(),
        "company": job.company or "",
        "requirements": hashlib.sha256((job.requirements or "").encode()).hexdigest(),
        "template": template or str(get("resume.template", "") or ""),
        "look": json.dumps(look or {}, sort_keys=True), "contact": _contact_fingerprint(),
        "length": str(get("resume.length", "auto")), "max_pages": str(get("resume.max_pages", "")),
        "writer": llm.provider_name(cfg), "model": llm.model_name(cfg) if source == "tailored" else "",
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def job_template(cfg, job) -> str:
    """The template a job's resume is made in unless the person chooses another: the one picked for the job
    (ml/resume/style.for_job), the job page's own default -- every entry point the same (A1.1 review)."""
    from ml.resume.style import DEFAULT, for_job
    default = ((cfg.get("resume.template", DEFAULT) if cfg is not None else DEFAULT) or DEFAULT)
    try:
        return for_job(job.source or "", job.company or "", job.title or "", job.description or "",
                       default=default, clearance=getattr(job, "clearance", None) or "")[0]
    except Exception:                          # noqa: BLE001 -- the configured default, as the run would use
        return default


def _posting_problem(job) -> str | None:
    from ml.resume.scorer import score_basis
    text = (job.description or "").strip()
    if not text:
        return "the job has no posting text to tailor to"
    if score_basis(text) != "full":
        return (f"the posting is only a {len(text.split())}-word teaser from the job board; paste the full posting "
                "on the job's page first")
    return None


def _writer_allowed(cfg, paid_ok: bool):
    """(budget or None). A writer that bills needs the person's yes AND a spending limit above zero."""
    from backend.apply import runner
    from ml.resume import writers
    from ml.resume.cost import Ledger
    problem = writers.primary_problem(cfg)          # not verified to cost nothing per use (A1.1.1): fail closed
    if problem:
        raise TailoringFailed("writer_not_approved", problem)
    _, label, free = runner.writer(cfg)
    if free:
        return None
    if not paid_ok:
        raise TailoringFailed("paid_not_allowed", f"Tailoring would use {label}, which bills per use, and you have "
                                                  "not said yes to that for this run.")
    cap = float((cfg.get("resume.max_spend_per_run_usd", 0) if cfg is not None else 0) or 0)
    if cap <= 0:
        raise TailoringFailed("no_spend_limit", f"Tailoring would use {label}, which bills per use, and no spending "
                                                "limit is set (resume.max_spend_per_run_usd): it is not used without one.")
    return Ledger(cap_usd=cap)


def _better(job_id: int, folder: Path, score: float | None) -> bool:
    """Should this run replace the job's current tailored version? As finish_job_run decided it: a rewrite
    that does worse never replaces a better one, unless it was made from a newer resume."""
    from ml.tailoring import coordination
    current = coordination.current(job_id)
    if current is None or current.source != "tailored":
        return True
    record = _job_record(job_id)
    best = record.get("best_score") if record.get("measure") == "ats" else None
    source, best_source = source_of(folder), record.get("best_source")
    newer_resume = bool(source and best_source and source != best_source)
    return best is None or (score or 0) >= best or newer_resume


def _settle(cfg, job, folder: Path, lease, *, key: str, score: float | None, note: str,
            export: tuple[Path, Path | None] | None = None) -> object:
    """Publish a finished run as the job's newest version, then keep the job's record and its folder
    copies (exports, never read to send) in step."""
    from ml.resume.packet import packet_dir, packet_root
    from ml.resume.writer import output_filename
    from ml.tailoring import coordination
    published = coordination.publish(lease, docx=folder / "tailored-resume.docx", pdf=folder / "tailored-resume.pdf",
                                     source="tailored", repeat_key=key, run_id=folder.name, note=note)
    record = _job_record(job.id)
    record.update(best=folder.name, best_score=score, best_source=source_of(folder), measure="ats", run=folder.name)
    _write_json(JOBS / f"{job.id}.json", record)
    redo_done(job.id)
    try:                                    # the job page's and the folder's copies: shown, never sent
        if export is not None:              # where the run itself says its job's copies go (job.json)
            target, packet = export
        else:
            target = packet_root(cfg) / output_filename(job.company, job.title, job.external_id or "")
            packet = packet_dir(cfg, job.company, job.title, job.external_id or "", job_id=job.id)
        place_for_job(folder, target, packet)
    except Exception as exc:                # noqa: BLE001 -- a copy for show never fails a preparation
        log.warning("Could not refresh the job folder's copy for job %s: %s", job.id, exc)
    return published


def earlier_resume(job, resume_file: Path) -> tuple[Path | None, str | None]:
    """The job's resume tailored before Track A1 -- its best finished, really tailored run with both files --
    and why it cannot be used as it is (None when it can: made from the resume as it reads now, for the
    posting as it reads now). (None, None) when there is none. Never changed or deleted here (A1.1: the
    person's 141 earlier resumes are kept; one made from other inputs is used only on their word)."""
    best, _ = job_best(job.id)
    folder = folder_of(best) if best else None
    if folder is None:
        return None, None
    try:
        result = json.loads((folder / "result.json").read_text())
    except (OSError, ValueError):
        return None, None
    if result.get("draft_note") or result.get("draft_state", "tailored") != "tailored":
        return None, None
    if not (folder / "tailored-resume.docx").is_file() or not (folder / "tailored-resume.pdf").is_file():
        return None, None
    posting = folder / "inputs" / "job.txt"
    if not posting.is_file() or posting.read_text() != f"{job.title}\n\n{job.description or ''}":
        return folder, "was made for an earlier version of this posting"
    probe = new_run()
    try:
        save_saved_resume(probe, resume_file)
        if source_of(probe) != source_of(folder):
            return folder, "was made from an earlier version of your resume"
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    return folder, None


EARLIER_CHOICE = ("It is kept as it was. Press Tailor it again for a new one (about 9 minutes), or Use the earlier "
                  "one to send it as it is.")


def use_earlier_resume(cfg, job) -> object:
    """The person's word: send the job's earlier tailored resume as it is, though made from other inputs. Published
    as the job's current version, with that said; never chosen by the app (A1.1)."""
    from ml.resume.pipeline import base_resume_path
    from ml.tailoring import coordination
    folder, why = earlier_resume(job, base_resume_path(cfg))
    if folder is None:
        raise TailoringFailed("no_earlier", "This job has no earlier tailored resume to use.")
    try:
        lease = coordination.acquire(job.id, "Use the earlier one")
    except coordination.CoordinationError as exc:
        raise TailoringFailed("busy", str(exc)) from None
    try:
        # The key as every Prepare computes it -- the job's own template, not the configured default (A1.1.1:
        # without it a startup or government posting's next Prepare made a new one over the person's choice).
        published = _settle(cfg, job, folder, lease,
                            key=repeat_key(cfg, job, base_resume_path(cfg), template=job_template(cfg, job)),
                            score=_job_record(job.id).get("best_score"),
                            note=CHOSEN_NOTE + (f" ({why})" if why else ""))
        # And the choice itself, by its version: kept by every Prepare until the person asks for a new one
        # (Tailor it again, Re-tailor), whatever changed meanwhile -- the template, the look, the resume.
        try:
            record = _job_record(job.id)
            record["chosen_version"] = published.version
            _write_json(JOBS / f"{job.id}.json", record)
        except OSError as exc:              # published, but the choice not kept: said so, never "not published"
            raise TailoringFailed("choice_not_saved", f"Your earlier resume is now the job's version "
                                  f"{published.version}, but your choice could not be written down ({exc}). Press "
                                  "Use the earlier one again so that a later Prepare keeps it.") from None
        return published
    except coordination.CoordinationError as exc:
        raise TailoringFailed("not_published", str(exc)) from None
    except OSError as exc:
        raise TailoringFailed("not_published", f"The earlier resume could not be published: {exc}") from None
    finally:
        coordination.release(lease)


CHOSEN_NOTE = "your earlier tailored resume, chosen by you"


def chosen_by_person(job_id: int, current) -> bool:
    """Is the job's current version the earlier one the person chose (Use the earlier one)? By version, so a
    newer version -- tailored again, edited, their own resume -- ends the choice by itself (A1.1.1)."""
    return current is not None and _job_record(job_id).get("chosen_version") == current.version


def prepare_for_job(cfg, job, *, purpose: str, again: bool = False, paid_ok: bool = False,
                    cancelled=None, lease=None, folder: Path | None = None, template: str | None = None,
                    look: dict | None = None) -> PreparedResume:
    """The job's resume, tailored and published -- or TailoringFailed with why. `again` makes it anew even
    when nothing changed; `cancelled()` is asked at every stage; a caller that took the job's claim itself
    (to answer at once that it is busy) passes it as `lease`, and its own run folder as `folder`."""
    from ml.resume import cost
    from ml.resume.pipeline import base_resume_path
    from ml.tailoring import coordination

    cancelled = cancelled or (lambda: False)
    try:
        resume_file = base_resume_path(cfg)
    except FileNotFoundError as exc:
        raise TailoringFailed("no_resume", str(exc)) from None
    problem = _posting_problem(job)
    if problem:
        raise TailoringFailed("no_posting", problem[0].upper() + problem[1:] + ".")
    ledger = _writer_allowed(cfg, paid_ok)
    template = template or job_template(cfg, job)       # the same template, so the same key, from every entry point
    own_lease = lease is None
    if own_lease:
        try:
            lease = coordination.acquire(job.id, purpose)
        except coordination.LeaseBusy as exc:
            raise TailoringFailed("busy", str(exc)) from None
        except coordination.LeaseUnreadable as exc:
            raise TailoringFailed("claim_unreadable", str(exc)) from None
    beating = coordination.keep_alive(lease)
    try:
        key = repeat_key(cfg, job, resume_file, template=template, look=look)
        redo = bool(needs_redo(job.id))
        current = coordination.current(job.id)
        if not again and not redo and current is not None:
            if current.source == "own_resume":
                return PreparedResume(current, True, "your own resume, chosen by you", current.run_id)
            if chosen_by_person(job.id, current):        # never made again over the person's own choice
                return PreparedResume(current, True, CHOSEN_NOTE, current.run_id,
                                      _job_record(job.id).get("best_score"))
            if current.repeat_key == key:
                return PreparedResume(current, True, "tailored before for this posting, unchanged since",
                                      current.run_id, _job_record(job.id).get("best_score"))
        if not again and not redo and current is None:
            earlier, why = earlier_resume(job, resume_file)
            if earlier is not None and why is None:       # the very same inputs: published as it is
                try:
                    adopted = _settle(cfg, job, earlier, lease, key=key, score=_job_record(job.id).get("best_score"),
                                      note="tailored before Nov 2026 from the same resume and posting")
                except coordination.LeaseLost as exc:
                    raise TailoringFailed("claim_lost", str(exc)) from None
                except (coordination.CoordinationError, OSError) as exc:
                    raise TailoringFailed("not_published", str(exc)) from None
                return PreparedResume(adopted, True, "tailored before for this posting", adopted.run_id,
                                      _job_record(job.id).get("best_score"))
            if earlier is not None:                       # other inputs: kept, and the person chooses
                raise TailoringFailed("earlier_outdated", f"its earlier tailored resume {why}. {EARLIER_CHOICE}")
        if cancelled():
            raise TailoringCancelled()
        from ml.resume.packet import packet_dir, packet_root
        from ml.resume.writer import output_filename
        folder = folder or new_run()
        resume = save_saved_resume(folder, resume_file)
        target = packet_root(cfg) / output_filename(job.company, job.title, job.external_id or "")
        remember_job_run(job.id, folder, target,
                         packet_dir(cfg, job.company, job.title, job.external_id or "", job_id=job.id))
        _progress(folder, stage=0, done=False, error=None, note="")

        def follow(stage, note=""):
            if lease.lost:
                raise TailoringFailed("claim_lost", "Another preparation took this job over; this one stopped.")
            if cancelled():
                raise TailoringCancelled()
            _progress(folder, stage=STAGES.index(stage), note=note)

        try:
            if ledger is not None and not cost.active_budgets():   # a run's own budget, when it has one, counts it
                with cost.budget(ledger):
                    result = run(folder, resume, f"{job.title}\n\n{job.description or ''}", None, cfg,
                                 progress=follow, company=job.company, template=template, look=look)
            else:
                result = run(folder, resume, f"{job.title}\n\n{job.description or ''}", None, cfg,
                             progress=follow, company=job.company, template=template, look=look)
        except TailoringFailed as exc:
            _progress(folder, done=True, error={"stage": "", "file": None, "message": exc.message})
            raise
        except cost.SpendCapReached as exc:
            _progress(folder, done=True, error={"stage": "", "file": None, "message": str(exc)})
            raise TailoringFailed("spend_limit", str(exc)) from None
        except StageError as exc:
            _progress(folder, done=True, error={"stage": exc.stage, "file": exc.file, "message": str(exc)})
            raise TailoringFailed("stage", f"{exc.stage}: {exc}") from None
        except Exception as exc:              # noqa: BLE001 -- reported, never swallowed into a substitute
            message = f"{type(exc).__name__}: {str(exc)[:200]}"
            _progress(folder, done=True, error={"stage": "", "file": None, "message": message})
            raise TailoringFailed("error", f"Tailoring stopped: {message}") from None
        if cancelled():
            _progress(folder, done=True, error={"stage": "", "file": None, "message": "cancelled"})
            raise TailoringCancelled()
        state = result.get("draft_state", "tailored")
        if state != "tailored" or result.get("draft_note"):
            _progress(folder, done=True, error=None, note="not tailored")
            raise TailoringFailed("not_tailored", (result.get("draft_note") or "The draft was not tailored.")
                                  + " Nothing was published: retry, or choose your own resume for this job.")
        score = (result.get("before_after") or {}).get("ats", [None, result.get("score")])[1]
        if not redo and not _better(job.id, folder, score):     # one set to be made again is replaced
            _progress(folder, stage=len(STAGES), done=True, error=None, note="kept the earlier, better one")
            return PreparedResume(current, True, "tailored again, but the earlier one matches better: kept",
                                  current.run_id, _job_record(job.id).get("best_score"))
        try:
            published = _settle(cfg, job, folder, lease, key=key, score=score, note=purpose)
        except coordination.LeaseLost as exc:
            raise TailoringFailed("claim_lost", str(exc)) from None
        except coordination.CoordinationError as exc:
            raise TailoringFailed("not_published", str(exc)) from None
        except OSError as exc:              # a full disk, a file gone: its version is marked failed, none shown
            raise TailoringFailed("not_published", f"The new resume could not be published: {exc}") from None
        _progress(folder, stage=len(STAGES), done=True, error=None, note="")
        return PreparedResume(published, False, "tailored just now", folder.name, score)
    finally:
        if own_lease:
            coordination.release(lease)
        else:
            lease._stop.set()
        beating.join(timeout=1)


def publish_edit(job_id: int, folder: Path, *, note: str) -> object:
    """A job's run, edited by the person: its files published as a new version, under the job's claim. Its
    copies for show go where the run itself recorded (job.json), as they always did."""
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session
    from ml.tailoring import coordination
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise ValueError("That run's job is no longer in your jobs list.")
        session.expunge(job)
    try:
        lease = coordination.acquire(job_id, "your edit")
    except coordination.CoordinationError as exc:
        raise JobBusy(str(exc)) from None
    try:
        owner = job_of_run(folder) or {}
        export = ((Path(owner["docx"]), Path(owner["packet"]) if owner.get("packet") else None)
                  if owner.get("docx") else None)
        if export is None:
            raise ValueError("This run does not say where its job's files go.")
        # The key of the version this run was published under: an edit is the person's improvement of that
        # preparation, so it stays the job's resume -- Prepare now reuses it -- until an input changes (A1.1).
        made_as = coordination.latest_for_run(job_id, folder.name)
        key = made_as.repeat_key if made_as is not None else f"edit:{folder.name}:{_sha_file(folder / 'tailored-resume.pdf')}"
        return _settle(None, job, folder, lease, key=key, score=_job_record(job_id).get("best_score"), note=note,
                       export=export)
    finally:
        coordination.release(lease)


def use_own_resume(cfg, job) -> object:
    """The person's own resume for this job, chosen by them: their own lines, selected for the posting
    and laid out to its pages -- nothing reworded, no model -- published as the job's newest version.
    Only ever on the person's press (Track A1): a failed tailoring never falls back to it by itself."""
    from ml.resume.fit import fit_to_pages
    from ml.resume.pdf import write_pdf
    from ml.resume.pipeline import base_resume_path, load_base_resume
    from ml.resume.tailor import TailorResult
    from ml.resume.writer import write_tailored_resume
    from ml.tailoring import coordination
    try:
        resume_file = base_resume_path(cfg)
        base = load_base_resume(cfg)
    except FileNotFoundError as exc:
        raise TailoringFailed("no_resume", str(exc)) from None
    try:
        lease = coordination.acquire(job.id, "Use my own resume")
    except coordination.LeaseBusy as exc:
        raise TailoringFailed("busy", str(exc)) from None
    except coordination.LeaseUnreadable as exc:
        raise TailoringFailed("claim_unreadable", str(exc)) from None
    try:
        result = TailorResult(summary=None)
        limit = int((cfg.get("resume.max_pages", 4) if cfg is not None else 4) or 0)
        if limit > 0:
            fit_to_pages(base, result, job.description or job.title, requirements=job.requirements,
                         company=job.company, max_pages=limit)
        folder = OWN / str(job.id) / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
        folder.mkdir(parents=True, exist_ok=False)
        write_tailored_resume(base, result, folder / "resume.docx")
        write_pdf(base, result, folder / "resume.pdf")
        return coordination.publish(lease, docx=folder / "resume.docx", pdf=folder / "resume.pdf",
                                    source="own_resume", repeat_key=repeat_key(cfg, job, resume_file, source="own_resume"),
                                    run_id=None, note="your own resume, chosen by you")
    except coordination.CoordinationError as exc:
        raise TailoringFailed("not_published", str(exc)) from None
    finally:
        coordination.release(lease)
