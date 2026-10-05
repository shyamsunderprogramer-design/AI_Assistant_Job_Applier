"""Missing-skill suggestions: found by rules, drafted under a real job, added only when kept."""

from pathlib import Path

import pytest

from ml.resume import suggest
from ml.resume.parser import parse_resume

RESUME = """Jordan Example
jordan@example.com | +1 555 010 0199
EXPERIENCE
Senior Platform Engineer, Acme Corp     Jan 2020 – Present
- Ran Kubernetes clusters on EKS for 40 services.
- Wrote Terraform modules for every AWS account.
Platform Engineer, Beta Inc     Jan 2016 – Dec 2019
- Built CI/CD pipelines in Jenkins for each release.
SKILLS
Python, Datadog
"""

JD = """Senior SRE
Requirements:
- Experience with Kubernetes and Terraform.
- Experience with Kafka and Rust.
- Datadog dashboards.
"""


@pytest.fixture
def resume(tmp_path):
    path = tmp_path / "resume.txt"
    path.write_text(RESUME)
    return parse_resume(path)


def test_only_what_the_resume_does_not_show_is_listed(resume):
    found = {m["skill"].lower(): m for m in suggest.missing(resume, JD)}
    assert "kafka" in found and "rust" in found
    assert "kubernetes" not in found and "terraform" not in found
    assert found["datadog"]["why"] == "only in your skills list"


def test_lines_go_under_a_real_job_even_without_a_model(resume, monkeypatch):
    monkeypatch.setattr("ml.resume.llm.complete", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    blocks = suggest.role_blocks(resume)
    assert blocks[0][0].startswith("Senior Platform Engineer") and blocks[0][1]
    items = suggest.draft([{"skill": "Kafka", "required": True, "why": "not on your resume"}], blocks)
    assert items[0]["role"] == blocks[0][0] and "Kafka" in items[0]["line"] and not items[0]["drafted"]


def test_a_model_line_that_drops_the_skill_is_not_used(resume, monkeypatch):
    class Reply:
        text = '{"items": [{"skill": "Kafka", "job": 1, "line": "Built event pipelines for releases."}]}'
    monkeypatch.setattr("ml.resume.llm.complete", lambda *a, **k: Reply())
    items = suggest.draft([{"skill": "Kafka", "required": True, "why": ""}], suggest.role_blocks(resume))
    assert "Kafka" in items[0]["line"] and not items[0]["drafted"]


def test_kept_lines_are_added_and_unticked_ones_are_not(resume, monkeypatch, tmp_path):
    from ml.resume import additions
    monkeypatch.setattr(additions, "ADDITIONS_PATH", tmp_path / "additions.yaml")
    monkeypatch.setattr("ml.resume.pipeline.load_base_resume", lambda cfg: resume)
    monkeypatch.setattr(suggest, "FOLDER", tmp_path / "sugg")
    role = suggest.role_blocks(resume)[0][0]
    added, problems = suggest.accept(None, 1, [
        {"skill": "Kafka", "role": role, "line": "Built Kafka consumers for deployment events across services."},
        {"skill": "Rust", "role": role, "line": "too short"},
    ])
    assert added == 1 and len(problems) == 1 and "Rust" in problems[0]
    assert [e["tool"] for e in additions.load()] == ["Kafka"]


def test_a_worse_rerun_never_replaces_a_better_resume(tmp_path, monkeypatch):
    from ml.tailoring import pipeline
    monkeypatch.setattr(pipeline, "JOBS", tmp_path / "jobs")
    def run(name, text):
        folder = tmp_path / name; folder.mkdir()
        (folder / "tailored-resume.docx").write_text(text); (folder / "tailored-resume.pdf").write_text(text)
        return folder
    target, packet = tmp_path / "out" / "acme.docx", tmp_path / "Applied" / "acme"
    packet.mkdir(parents=True)
    assert pipeline.finish_job_run(7, run("r1", "good"), target, 82.0, packet)
    assert not pipeline.finish_job_run(7, run("r2", "worse"), target, 60.0, packet)
    assert target.read_text() == "good" and (packet / "resume.docx").read_text() == "good"
    assert pipeline.job_best(7) == ("r1", 82.0) and pipeline.job_run(7) == "r2"
