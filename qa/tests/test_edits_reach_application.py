"""Edits made on the tailoring page are what the application attaches."""

import json
from types import SimpleNamespace as NS

from ml.tailoring import pipeline


def test_an_edited_job_run_becomes_the_jobs_resume(tmp_path, monkeypatch):
    run = tmp_path / "run"; run.mkdir()
    (run / "tailored-resume.docx").write_text("edited docx")
    (run / "tailored-resume.pdf").write_text("edited pdf")
    target = tmp_path / "output" / "acme_engineer.docx"
    target.parent.mkdir(); target.write_text("old docx"); target.with_suffix(".pdf").write_text("old pdf")
    (run / "job.json").write_text(json.dumps({"job_id": 7, "docx": str(target), "packet": None}))
    (run / "result.json").write_text("{}")
    monkeypatch.setattr(pipeline, "JOBS", tmp_path / "jobs")
    draft = NS(result=NS(overrides={}))
    monkeypatch.setattr(pipeline, "load_draft", lambda folder: (draft, {}))
    monkeypatch.setattr(pipeline, "_draft_json", lambda d: {})
    from ml.tailoring import documents
    monkeypatch.setattr(documents, "produce", lambda *a, **k: NS(
        docx=run / "tailored-resume.docx", pdf=run / "tailored-resume.pdf", pages={}, checks=[], ok=True))

    data = pipeline.apply_edits(run, {"Built pipelines.": "Built CI/CD pipelines."})

    assert data["placed_for_job"] is True
    assert target.read_text() == "edited docx"
    assert target.with_suffix(".pdf").read_text() == "edited pdf"
    assert pipeline.job_best(7)[0] == "run"
