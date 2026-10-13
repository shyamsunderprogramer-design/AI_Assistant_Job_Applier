"""The Resume pages: the health check, the builder and the history of tailored resumes.

Registered on the one app (backend/api/app.py imports this at its end), so
they share its config, its database and its localhost-only binding.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from flask import jsonify, render_template, request, send_file

from backend.api.app import PROJECT_ROOT, app, cfg

CHECK_UPLOADS = PROJECT_ROOT / "data" / "health" / "uploads"
CHECK_TYPES = (".docx", ".pdf")


# -- the health check -----------------------------------------------------------------------

def _check_sources() -> list[dict]:
    """What can be checked: the master resume, the built one, and recent tailored resumes."""
    from ml.tailoring.pipeline import RUNS
    out = []
    try:
        from ml.resume.pipeline import base_resume_path
        out.append({"key": "master", "label": f"Your master resume ({base_resume_path(cfg()).name})"})
    except Exception:
        pass
    from ml.resume import builder
    if builder.DOCX.exists():
        out.append({"key": "builder", "label": "The resume you built here"})
    if RUNS.exists():
        for folder in sorted(RUNS.iterdir(), reverse=True)[:30]:
            docx = folder / "tailored-resume.docx"
            if not docx.exists():
                continue
            try:
                title = json.loads((folder / "result.json").read_text()).get("title") or ""
            except (OSError, ValueError):
                title = ""
            company = (folder / "inputs" / "company.txt")
            company = company.read_text().strip() if company.exists() else ""
            when = f"{folder.name[:4]}-{folder.name[4:6]}-{folder.name[6:8]}"
            out.append({"key": f"run:{folder.name}",
                        "label": f"Tailored · {company + ' · ' if company else ''}{title or folder.name} · {when}"})
    return out


def _check_path(source: str) -> Path | None:
    from ml.resume import builder
    from ml.tailoring.pipeline import folder_of
    if source == "master":
        from ml.resume.pipeline import base_resume_path
        return base_resume_path(cfg())
    if source == "builder":
        return builder.DOCX if builder.DOCX.exists() else None
    if source.startswith("run:"):
        folder = folder_of(source[4:])
        path = folder / "tailored-resume.docx" if folder else None
        return path if path and path.exists() else None
    return None


@app.get("/resume/check")
def resume_check_page():
    """How well the resume is written and built, with no job posting."""
    return render_template("resume_check.html", sources=_check_sources(),
                           source=request.args.get("source") or "master")


@app.get("/resume/check/run")
def resume_check_run():
    from ml.resume import health
    source = request.args.get("source") or "master"
    try:
        path = _check_path(source)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 404
    if path is None:
        return jsonify(ok=False, error="That resume is not there any more."), 404
    try:
        report = health.check_file(path)
    except Exception as exc:
        return jsonify(ok=False, error=f"The resume could not be read: {type(exc).__name__}: {exc}"), 500
    return jsonify(ok=True, file=path.name, **report.to_dict())


@app.post("/resume/check/upload")
def resume_check_upload():
    """Check a file the person picks. Read, checked, and deleted: nothing is kept."""
    from ml.resume import health
    uploaded = request.files.get("resume")
    if not uploaded or not uploaded.filename:
        return jsonify(ok=False, error="No file chosen."), 400
    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in CHECK_TYPES:
        return jsonify(ok=False, error="Use a .docx or .pdf file."), 400
    CHECK_UPLOADS.mkdir(parents=True, exist_ok=True)
    path = CHECK_UPLOADS / f"{uuid.uuid4().hex}{suffix}"
    uploaded.save(path)
    try:
        report = health.check_file(path)
    except Exception as exc:
        return jsonify(ok=False, error=f"The file could not be read: {type(exc).__name__}: {exc}"), 400
    finally:
        path.unlink(missing_ok=True)
    return jsonify(ok=True, file=Path(uploaded.filename).name, **report.to_dict())


@app.post("/resume/check/rewrite")
def resume_check_rewrite():
    """Better versions of the weak bullets, from each bullet's own facts; numbers and tools checked."""
    from ml.resume import health
    from ml.tailoring.reader import read_document
    body = request.get_json(silent=True) or {}
    bullets = [str(b) for b in body.get("bullets") or [] if str(b).strip()][:12]
    if not bullets:
        return jsonify(ok=False, error="No bullets given."), 400
    context = ""
    try:
        path = _check_path(str(body.get("source") or "master"))
        context = read_document(path).text if path else ""
    except Exception:
        context = ""
    try:
        return jsonify(ok=True, bullets=health.rewrite_bullets(bullets, context, cfg()))
    except Exception as exc:
        return jsonify(ok=False, error=f"The rewrite could not be made: {type(exc).__name__}: {exc}"), 500


# -- the builder ------------------------------------------------------------------------------

def _builder_state(data: dict) -> dict:
    from ml.resume import builder
    return {"data": data, "gaps": builder.gaps(data), "job_checks": builder.job_checks(data),
            "suggest": builder.suggest_skills(data), "years": builder.years_of(data)}


@app.get("/resume/build")
def builder_page():
    """Build a resume from scratch, or from the one you have, in the same templates as a tailored one."""
    from ml.resume.style import choices, look_choices
    return render_template("resume_build.html", templates=choices(), looks=look_choices())


@app.get("/resume/build/data")
def builder_data():
    from ml.resume import builder
    data = builder.load()
    return jsonify(ok=True, made=builder.DOCX.exists(), **_builder_state(data))


@app.post("/resume/build/save")
def builder_save():
    from ml.resume import builder
    body = request.get_json(silent=True) or {}
    data = builder.save(body.get("data") or {})
    return jsonify(ok=True, **_builder_state(data))


@app.post("/resume/build/preview")
def builder_preview():
    """Save, draw the .docx and PDF, picture each page, and run the health check on the form."""
    from ml.resume import builder
    body = request.get_json(silent=True) or {}
    data = builder.save(body["data"]) if body.get("data") else builder.load()
    try:
        drawn = builder.render(data)
    except Exception as exc:
        return jsonify(ok=False, error=f"The resume could not be drawn: {type(exc).__name__}: {exc}"), 500
    report = builder.health(data)
    return jsonify(ok=True, stamp=data["updated"], health=report.to_dict(), **drawn, **_builder_state(data))


@app.get("/resume/build/page/<name>")
def builder_page_image(name):
    from ml.resume import builder
    import re
    if not re.fullmatch(r"page[\w-]*\.png", name or ""):
        return ("", 404)
    path = builder.OUT / name
    return send_file(path, mimetype="image/png", max_age=0) if path.exists() else ("", 404)


@app.get("/resume/build/download/<kind>")
def builder_download(kind):
    from ml.resume import builder
    path = {"docx": builder.DOCX, "pdf": builder.PDF}.get(kind)
    if path is None or not path.exists():
        return ("Make the resume first: press Preview.", 404)
    name = builder.load()["contact"]["name"] or "Resume"
    stem = "-".join(name.split()) + "-Resume"
    return send_file(path, as_attachment=True, download_name=f"{stem}.{kind}")


@app.post("/resume/build/import")
def builder_import():
    """The form filled from a resume: the master, or a file picked now. Shown to the person; saved when they keep it."""
    from ml.resume import builder
    uploaded = request.files.get("resume")
    if uploaded and uploaded.filename:
        suffix = Path(uploaded.filename).suffix.lower()
        if suffix not in CHECK_TYPES:
            return jsonify(ok=False, error="Use a .docx or .pdf file."), 400
        CHECK_UPLOADS.mkdir(parents=True, exist_ok=True)
        path = CHECK_UPLOADS / f"{uuid.uuid4().hex}{suffix}"
        uploaded.save(path)
        try:
            out = builder.from_file(path)
        except Exception as exc:
            return jsonify(ok=False, error=f"The file could not be read: {type(exc).__name__}: {exc}"), 400
        finally:
            path.unlink(missing_ok=True)
    else:
        try:
            from ml.resume.pipeline import base_resume_path
            out = builder.from_file(base_resume_path(cfg()))
        except Exception as exc:
            return jsonify(ok=False, error=f"Your resume could not be read: {type(exc).__name__}: {exc}"), 400
    return jsonify(ok=True, **out)


def _ai(fn):
    try:
        return jsonify(ok=True, **fn())
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, error=f"The model could not be used: {type(exc).__name__}: {exc}"), 500


@app.post("/resume/build/bullets")
def builder_bullets():
    """Bullets from one job's notes. Every number and tool checked against what the person wrote."""
    from ml.resume import builder
    body = request.get_json(silent=True) or {}
    data = builder.save(body["data"]) if body.get("data") else builder.load()
    kind = "projects" if body.get("kind") == "projects" else "experience"
    return _ai(lambda: builder.write_bullets(data, kind, str(body.get("id") or ""), cfg()))


@app.post("/resume/build/improve")
def builder_improve():
    from ml.resume import builder
    body = request.get_json(silent=True) or {}
    data = builder.load()
    kind = "projects" if body.get("kind") == "projects" else "experience"
    return _ai(lambda: builder.improve_bullet(data, kind, str(body.get("id") or ""),
                                              str(body.get("bullet") or ""), cfg()))


@app.post("/resume/build/summary")
def builder_summary():
    from ml.resume import builder
    body = request.get_json(silent=True) or {}
    data = builder.save(body["data"]) if body.get("data") else builder.load()
    return _ai(lambda: builder.write_summary(data, cfg()))


@app.post("/resume/build/master")
def builder_master():
    """Make the built resume the master: the old one kept, the job search re-derived from the new one."""
    from backend.api.app import remember_upload, resume_dir, run_command
    from ml.resume import builder
    if cfg().get("resume.base_path") and Path(str(cfg().get("resume.base_path"))).suffix:
        return jsonify(ok=False, error="Settings point resume.base_path at one file, so the master cannot be "
                                       "swapped here. Clear it in config.yaml first."), 400
    try:
        builder.render(builder.load())
        target, kept = builder.make_master(resume_dir())
    except FileNotFoundError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    remember_upload("Built resume.docx", target)
    ok, output = run_command("profile", "--force")
    return jsonify(ok=True, stored=target.name, kept=kept.name if kept else None, profile_ok=ok,
                   output=output[-1500:])


# -- every tailored version ----------------------------------------------------------------------

@app.get("/resume/history")
def history_page():
    """Every tailored resume: open, download, check, compare two, or clear out."""
    return render_template("resume_history.html")


@app.get("/resume/history/list")
def history_list():
    from ml.tailoring import history
    return jsonify(ok=True, runs=history.runs())


@app.get("/resume/history/compare")
def history_compare():
    from ml.tailoring import history
    try:
        return jsonify(ok=True, **history.compare(request.args.get("a", ""), request.args.get("b", "")))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 404
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


@app.post("/resume/history/delete")
def history_delete():
    from ml.tailoring import history
    ids = [str(i) for i in (request.get_json(silent=True) or {}).get("ids") or []][:200]
    deleted, kept = [], []
    for run_id in ids:
        try:
            history.delete(run_id)
            deleted.append(run_id)
        except (ValueError, PermissionError) as exc:
            kept.append({"id": run_id, "why": str(exc)})
    return jsonify(ok=True, deleted=deleted, kept=kept)


# -- the cover letter, edited in the browser --------------------------------------------------

def _letter_job(job_id: int) -> dict | None:
    """The job's letter folder and what checking its letter needs; None for an unknown job."""
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session
    from ml.resume.packet import packet_dir
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return None
        info = {"company": job.company or "", "title": job.title or "",
                "jd": f"{job.title}\n\n{job.description or ''}", "external": job.external_id or ""}
    info["folder"] = packet_dir(cfg(), info["company"], info["title"], info["external"], job_id=job_id)
    return info


def _letter_resume(info: dict) -> str:
    """The resume that goes with this letter: the job's tailored copy, else the master."""
    from ml.tailoring.reader import read_document
    for name in ("resume.docx", "resume.pdf"):
        path = info["folder"] / name
        if path.exists():
            try:
                return read_document(path).text
            except Exception:
                pass
    from ml.resume.pipeline import load_base_resume
    return load_base_resume(cfg()).text()


def _letter_look(job_id: int) -> tuple[list[str], str | None, dict | None]:
    """(header lines, template, look) for the letter's documents: the job's tailored resume's, else the default."""
    from ml.resume.parser import parse_resume
    from ml.resume.pipeline import base_resume_path
    from ml.tailoring.draft import header_of
    try:
        header = header_of(parse_resume(base_resume_path(cfg())))
    except Exception:
        header = []
    template, look = cfg().get("resume.template") or None, None
    try:
        from ml.tailoring.pipeline import JOBS, RUNS
        record = json.loads((JOBS / f"{job_id}.json").read_text())
        run = record.get("best") or record.get("run")
        drawn = json.loads((RUNS / run / "result.json").read_text())["draft"]["result"]
        template, look = drawn.get("template") or template, drawn.get("look") or None
    except Exception:
        pass
    return header, template, look


def _letter_state(job_id: int, info: dict, text: str | None = None) -> dict:
    from ml.resume import letter_edit
    from ml.resume.packet import LETTER_FILE
    path = info["folder"] / LETTER_FILE
    if text is None:
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    return {"exists": path.exists(), "text": text, "parts": letter_edit.parse(text),
            "check": letter_edit.check(text, _letter_resume(info), info["jd"], info["company"]) if text.strip() else None,
            "versions": letter_edit.versions(info["folder"]), "company": info["company"], "title": info["title"],
            "files": {k: (info["folder"] / f).exists() for k, f in (("docx", letter_edit.DOCX_FILE),
                                                                    ("pdf", letter_edit.PDF_FILE))}}


@app.get("/job/<int:job_id>/letter")
def letter_get(job_id: int):
    info = _letter_job(job_id)
    if info is None:
        return jsonify(ok=False, error="No such job."), 404
    return jsonify(ok=True, **_letter_state(job_id, info))


@app.post("/job/<int:job_id>/letter/check")
def letter_check(job_id: int):
    from ml.resume import letter_edit
    info = _letter_job(job_id)
    if info is None:
        return jsonify(ok=False, error="No such job."), 404
    text = str((request.get_json(silent=True) or {}).get("text") or "")
    return jsonify(ok=True, check=letter_edit.check(text, _letter_resume(info), info["jd"], info["company"]))


@app.post("/job/<int:job_id>/letter/save")
def letter_save(job_id: int):
    """Save the person's letter: the version it replaces is kept, the .docx and .pdf written again."""
    from ml.resume import letter_edit
    info = _letter_job(job_id)
    if info is None:
        return jsonify(ok=False, error="No such job."), 404
    text = str((request.get_json(silent=True) or {}).get("text") or "").strip()
    if len(text.split()) < 20:
        return jsonify(ok=False, error="That is too short to be a letter."), 400
    header, template, look = _letter_look(job_id)
    out = letter_edit.save(info["folder"], text, header, template, look)
    return jsonify(ok=True, saved=out["saved"], **_letter_state(job_id, info))


@app.post("/job/<int:job_id>/letter/restore")
def letter_restore(job_id: int):
    from ml.resume import letter_edit
    info = _letter_job(job_id)
    if info is None:
        return jsonify(ok=False, error="No such job."), 404
    header, template, look = _letter_look(job_id)
    try:
        letter_edit.restore(info["folder"], str((request.get_json(silent=True) or {}).get("id") or ""),
                            header, template, look)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, **_letter_state(job_id, info))


@app.post("/job/<int:job_id>/letter/rewrite")
def letter_rewrite(job_id: int):
    """One paragraph rewritten (shorter, plainer, more specific, or as a story); claims checked."""
    from ml.resume import letter_edit
    info = _letter_job(job_id)
    if info is None:
        return jsonify(ok=False, error="No such job."), 404
    body = request.get_json(silent=True) or {}
    try:
        out = letter_edit.rewrite_paragraph(str(body.get("paragraph") or ""), str(body.get("how") or ""),
                                            _letter_resume(info), info["jd"], info["company"], info["title"],
                                            cfg(), whole=str(body.get("text") or ""))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, error=f"The model could not be used: {type(exc).__name__}: {exc}"), 500
    return jsonify(ok=True, **out)


@app.get("/job/<int:job_id>/letter/download/<kind>")
def letter_download(job_id: int, kind: str):
    from ml.resume import letter_edit
    info = _letter_job(job_id)
    name = {"docx": letter_edit.DOCX_FILE, "pdf": letter_edit.PDF_FILE}.get(kind)
    if info is None or name is None:
        return ("No such file.", 404)
    path = info["folder"] / name
    if not path.exists():
        header, template, look = _letter_look(job_id)
        from ml.resume.packet import LETTER_FILE
        letter = info["folder"] / LETTER_FILE
        if not letter.exists():
            return ("No cover letter yet for this job.", 404)
        letter_edit._documents(info["folder"], letter.read_text(encoding="utf-8"), header, template, look)
    if not path.exists():
        return ("The document could not be made.", 500)
    company = re.sub(r"[^\w -]+", "", info["company"])[:40] or "job"
    return send_file(path, as_attachment=True, download_name=f"Cover letter - {company}.{kind}")
