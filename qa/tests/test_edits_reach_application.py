"""Edits made on the tailoring page are what the application attaches: since Track A1 (Nov 2026) as a new
published version of the job's resume, under the job's claim -- the job folder's copies follow it."""

import json
from types import SimpleNamespace as NS

import pytest

from data_engineering.db import session as session_mod
from data_engineering.db.models import Job
from data_engineering.db.session import get_session, init_engine
from ml.tailoring import coordination, pipeline


@pytest.fixture
def db(tmp_path):
    before = (session_mod._engine, session_mod._SessionFactory)
    init_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    with get_session() as s:
        s.add(Job(id=7, source="greenhouse", company="Acme", company_slug="acme", external_id="7", title="Engineer",
                  location="Remote", description="x " * 400, requirements="", application_url="https://example.com/7",
                  content_hash="h7", is_open=True, status="Not Applied"))
    yield
    session_mod._engine, session_mod._SessionFactory = before


def test_an_edited_job_run_becomes_the_jobs_resume(db, tmp_path, monkeypatch):
    run = pipeline.RUNS / "run-edited"
    run.mkdir(parents=True)
    (run / "tailored-resume.docx").write_text("edited docx")
    (run / "tailored-resume.pdf").write_text("edited pdf")
    target = tmp_path / "output" / "acme_engineer.docx"
    target.parent.mkdir(); target.write_text("old docx"); target.with_suffix(".pdf").write_text("old pdf")
    (run / "job.json").write_text(json.dumps({"job_id": 7, "docx": str(target), "packet": None}))
    (run / "result.json").write_text("{}")
    draft = NS(result=NS(overrides={}))
    monkeypatch.setattr(pipeline, "load_draft", lambda folder: (draft, {}))
    monkeypatch.setattr(pipeline, "_draft_json", lambda d: {})
    from ml.tailoring import documents
    monkeypatch.setattr(documents, "produce", lambda *a, **k: NS(
        docx=run / "tailored-resume.docx", pdf=run / "tailored-resume.pdf", pages={}, checks=[], ok=True))

    data = pipeline.apply_edits(run, {"Built pipelines.": "Built CI/CD pipelines."})

    assert data["placed_for_job"] is True
    published = coordination.current(7)
    assert published.pdf.read_text() == "edited pdf" and published.docx.read_text() == "edited docx"
    assert published.run_id == "run-edited" and published.note == "edited by you"
    assert target.read_text() == "edited docx"                    # the job folder's copy for show follows
    assert target.with_suffix(".pdf").read_text() == "edited pdf"
    assert pipeline.job_best(7)[0] == "run-edited"
