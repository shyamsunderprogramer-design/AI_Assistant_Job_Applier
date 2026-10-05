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
import shutil
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

RUNS = PROJECT_ROOT / "data" / "tailoring" / "runs"
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
        progress=lambda stage, note="": None, use_model: bool = True) -> dict:
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

    # 2 -- extracting resume information
    progress(STAGES[1])
    model = build(extracted)
    if not model.facts or not model.entries:
        raise StageError(STAGES[1], "No work history could be found in the resume. Check that it is the "
                                    "right file, or upload a .docx.", "resume")

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
    if resume_file.suffix.lower() not in (".docx", ".pdf", ".txt", ".md"):
        raise StageError(STAGES[4], "Unsupported resume file.", "resume")
    draft = drafting.create(resume_file, jd_text, report, cfg, use_model=use_model)
    # A header that says "LinkedIn" as a linked word keeps the link: the
    # writer draws a LinkedIn address as that word, linked.
    linkedin = next((l for l in model.contact.get("links", []) if "linkedin.com" in l.lower()), None)
    if linkedin:
        draft.header = [h.replace("LinkedIn", linkedin) if "LinkedIn" in h and "linkedin.com" not in h.lower()
                        else h for h in draft.header]

    # 6 -- checking the final files
    progress(STAGES[5])
    rendered = documents.produce(draft, folder, model=model, jd_text=jd_text)
    draft.changes = drafting.changes(draft.base, draft.result, draft.header)

    result = {
        "folder": str(folder),
        "files": {"docx": rendered.docx.name, "pdf": rendered.pdf.name},
        "pages": {k: [Path(p).name for p in v] for k, v in rendered.pages.items()},
        "score": report.score,
        "score_note": ("The builder's estimate: required items weigh 3 and preferred 1; a direct match "
                       "counts 1.0 and a related one 0.5. 100% means every identified requirement is "
                       "directly shown under these rules — not a guaranteed result from an employer's ATS."),
        "report": report.to_dict(),
        "alignment": report.alignment,
        "versions": versions.as_dicts(version_checks),
        "changes": draft.changes,
        "draft_note": draft.problem,
        "gaps": [asdict(m) for m in report.gaps],
        "checks": [asdict(c) for c in rendered.checks],
        "checks_ok": rendered.ok,
        "word_rendered": rendered.word,
        "attempts": rendered.attempts,
        "extraction": {"warnings": model.warnings, "ocr": model.ocr,
                       "text": extracted.text if (model.warnings or model.ocr) else ""},
        "notes": req_notes + report.notes,
        "draft": _draft_json(draft),
        "title": draft.title,
    }
    (folder / "result.json").write_text(json.dumps(result, indent=1, default=str))
    return result


def _draft_json(draft) -> dict:
    keep = ("summary", "bullets", "skills_order", "omitted", "gaps", "fitted", "overrides")
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
    path.write_text(json.dumps(current))


def start(folder: Path, resume_file: Path, jd_text: str, jd_file: Path | None, cfg,
          after=None) -> None:
    """Run in the background; the page follows progress.json.

    `after(result)` runs once the files are made and before the run reads as
    done, so a job page finds its resume in place when the bar reaches 100%.
    """
    import threading

    def follow(stage, note=""):
        _progress(folder, stage=STAGES.index(stage), note=note)

    def work():
        try:
            result = run(folder, resume_file, jd_text, jd_file, cfg, progress=follow)
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
    return Draft(base, result, data["draft"]["header"], data.get("title", "")), data


def editable_lines(folder: Path) -> list[dict]:
    from ml.resume.writer import plan_document
    draft, _ = load_draft(folder)
    return [{"kind": k, "text": t} for k, t in plan_document(draft.base, draft.result, draft.header)]


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
    rendered = documents.produce(draft, folder, attempts=1)
    data["files"] = {"docx": rendered.docx.name, "pdf": rendered.pdf.name}
    data["pages"] = {k: [Path(p).name for p in v] for k, v in rendered.pages.items()}
    data["checks"] = [asdict(c) for c in rendered.checks]
    data["checks_ok"] = rendered.ok
    data["draft"] = _draft_json(draft)
    data["edited"] = [{"before": k, "after": v} for k, v in overrides.items()]
    (folder / "result.json").write_text(json.dumps(data, indent=1, default=str))
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
        shutil.copy2(folder / "tailored-resume.docx", packet / "resume.docx")
        shutil.copy2(folder / "tailored-resume.pdf", packet / "resume.pdf")


def _job_record(job_id: int) -> dict:
    try:
        return json.loads((JOBS / f"{job_id}.json").read_text())
    except (OSError, ValueError):
        return {}


def remember_job_run(job_id: int, folder: Path, target_docx: Path, packet: Path | None = None) -> None:
    JOBS.mkdir(parents=True, exist_ok=True)
    record = _job_record(job_id)
    record["run"] = folder.name                      # the latest, which the page follows
    (JOBS / f"{job_id}.json").write_text(json.dumps(record))
    (folder / "job.json").write_text(json.dumps({"job_id": job_id, "docx": str(target_docx),
                                                 "packet": str(packet) if packet else None}))


def finish_job_run(job_id: int, folder: Path, target_docx: Path, score: float,
                   packet: Path | None = None) -> bool:
    """Make this run the job's resume only if it matches at least as well as the best so far.

    Returns whether it did. A rewrite that does worse never replaces a better one.
    """
    record = _job_record(job_id)
    best = record.get("best_score")
    better = best is None or score >= best or not target_docx.exists()
    if better:
        place_for_job(folder, target_docx, packet)
        record.update(best=folder.name, best_score=score)
    record["run"] = folder.name
    JOBS.mkdir(parents=True, exist_ok=True)
    (JOBS / f"{job_id}.json").write_text(json.dumps(record))
    return better


def job_run(job_id: int) -> str | None:
    return _job_record(job_id).get("run")


def job_best(job_id: int) -> tuple[str | None, float | None]:
    record = _job_record(job_id)
    return record.get("best") or record.get("run"), record.get("best_score")


def job_of_run(folder: Path) -> dict | None:
    try:
        return json.loads((folder / "job.json").read_text())
    except (OSError, ValueError):
        return None
