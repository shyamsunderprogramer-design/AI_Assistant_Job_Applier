"""Missing-skill suggestions: found by rules, drafted under a real job, added only when kept."""

import json
from pathlib import Path
from types import SimpleNamespace

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
    monkeypatch.setattr("ml.resume.packet.person_name", lambda: "Jordan Example")
    monkeypatch.setattr(pipeline, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(pipeline, "RUNS", tmp_path / "runs")
    def run(name, text):                    # a finished run: only those can be a job's best (Track A1)
        folder = tmp_path / "runs" / name; folder.mkdir(parents=True)
        (folder / "tailored-resume.docx").write_text(text); (folder / "tailored-resume.pdf").write_text(text)
        (folder / "result.json").write_text("{}")
        return folder
    target, packet = tmp_path / "out" / "acme.docx", tmp_path / "Applied" / "acme"
    packet.mkdir(parents=True)
    assert pipeline.finish_job_run(7, run("run-r1", "good"), target, 82.0, packet)
    assert not pipeline.finish_job_run(7, run("run-r2", "worse"), target, 60.0, packet)
    from ml.resume.packet import resume_in
    assert target.read_text() == "good" and resume_in(packet).read_text() == "good"
    assert pipeline.job_best(7) == ("run-r1", 82.0) and pipeline.job_run(7) == "run-r2"


def test_a_rewrite_of_a_corrected_resume_replaces_the_best_whatever_it_scored(tmp_path, monkeypatch):
    """82% from text read wrongly out of a PDF is no measure against 80% from the corrected master."""
    from docx import Document
    from ml.tailoring import pipeline
    monkeypatch.setattr("ml.resume.packet.person_name", lambda: "Jordan Example")
    monkeypatch.setattr(pipeline, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(pipeline, "RUNS", tmp_path / "runs")
    def run(name, made_from, text):
        folder = tmp_path / "runs" / name
        (folder / "inputs").mkdir(parents=True)
        doc = Document(); doc.add_paragraph(made_from); doc.save(folder / "inputs" / "resume.docx")
        (folder / "tailored-resume.docx").write_text(text); (folder / "tailored-resume.pdf").write_text(text)
        (folder / "result.json").write_text("{}")         # finished: only those can be a job's best (Track A1)
        return folder
    target, packet = tmp_path / "out" / "acme.docx", tmp_path / "Applied" / "acme"
    packet.mkdir(parents=True)
    assert pipeline.finish_job_run(7, run("run-broken", "GCP (GKE, Cloud Bui ld)", "broken"), target, 82.0, packet)
    assert not pipeline.finish_job_run(7, run("run-same", "GCP (GKE, Cloud Bui ld)", "worse"), target, 70.0, packet)
    assert pipeline.finish_job_run(7, run("run-clean", "GCP (GKE, Cloud Build)", "clean"), target, 80.0, packet)
    assert target.read_text() == "clean" and pipeline.job_best(7) == ("run-clean", 80.0)


# -- "I've used this": the line written for the person ---------------------------------------

POSTING = SimpleNamespace(id=7, title="Senior SRE", description=(
    "About the team\nYou will run our event platform.\n"
    "- Operate Kafka clusters that carry every order event between services.\n"
    "- Experience with Rust.\n"))


class Model:
    """A stand-in writer: the replies it is given, in order, and what it was asked."""

    def __init__(self, *replies):
        self.replies, self.asked = list(replies), []

    def __call__(self, system, user, cfg=None, **kw):
        self.asked.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(text=json.dumps(reply))


@pytest.fixture
def used(resume, monkeypatch):
    monkeypatch.setattr("ml.resume.pipeline.load_base_resume", lambda cfg: resume)
    monkeypatch.setattr(suggest, "_written", {})
    return [role for role, _ in suggest.role_blocks(resume)]


def test_the_line_is_written_from_that_jobs_own_lines_and_the_postings_words(used, monkeypatch):
    model = Model({"lines": [
        {"job": 0, "line": "Ran Kafka clusters on EKS that carried release events between services."},
        {"job": 1, "line": "Built Jenkins pipelines that published build events to Kafka topics."}]})
    monkeypatch.setattr("ml.resume.llm.complete", model)
    # A browser collapses the job line's spacing; it is matched on its words and sent back as sent.
    sent = [used[0], " ".join(used[1].split())]
    lines = suggest.draft_used(None, POSTING, "Kafka", sent)
    assert [x["role"] for x in lines] == sent and all(x["drafted"] for x in lines)
    assert lines[1]["line"].startswith("Built Jenkins pipelines")
    asked = model.asked[0]
    assert "Ran Kubernetes clusters on EKS for 40 services." in asked                # that job's own lines
    assert "Operate Kafka clusters that carry every order event between services." in asked  # what is asked
    assert "Rust" not in asked                                                        # not other skills


def test_a_line_is_written_once_unless_asked_again(used, monkeypatch):
    model = Model({"lines": [{"job": 0, "line": "Ran Kafka clusters on EKS that carried release events."}]},
                  {"lines": [{"job": 0, "line": "Operated Kafka on EKS for event traffic between services."}]})
    monkeypatch.setattr("ml.resume.llm.complete", model)
    first = suggest.draft_used(None, POSTING, "Kafka", used[:1])
    assert suggest.draft_used(None, POSTING, "kafka", used[:1]) == first and len(model.asked) == 1
    again = suggest.draft_used(None, POSTING, "Kafka", used[:1], again=True)
    assert again[0]["line"].startswith("Operated Kafka") and len(model.asked) == 2


def test_a_made_up_number_or_a_line_without_the_skill_is_not_offered(used, monkeypatch):
    monkeypatch.setattr("ml.resume.llm.complete", Model({"lines": [
        {"job": 0, "line": "Cut Kafka consumer lag by 40% across 12 clusters on EKS."},
        {"job": 1, "line": "Built Jenkins pipelines that published build events for every release."}]}))
    lines = suggest.draft_used(None, POSTING, "Kafka", used)
    assert [(x["line"], x["drafted"]) for x in lines] == [("", False), ("", False)]
    # Not a resume line: a person's "I" or "we".
    monkeypatch.setattr("ml.resume.llm.complete", Model({"lines": [
        {"job": 0, "line": "Ran Kafka on EKS for the release events of the services I managed."},
        {"job": 1, "line": "Built Jenkins pipelines that published our build events to Kafka topics."}]}))
    assert not any(x["drafted"] for x in suggest.draft_used(None, POSTING, "Kafka", used, again=True))
    # A number the job's own lines carry is the person's: "40 services".
    monkeypatch.setattr("ml.resume.llm.complete", Model({"lines": [
        {"job": 0, "line": "Ran Kafka on EKS for the 40 services that shared release events."}]}))
    assert suggest.draft_used(None, POSTING, "Kafka", used[:1], again=True)[0]["drafted"]


def test_without_a_model_the_person_writes_the_line(used, monkeypatch):
    monkeypatch.setattr("ml.resume.llm.complete", Model(RuntimeError("no model")))
    assert suggest.draft_used(None, POSTING, "Kafka", used[:1]) == [{"role": used[0], "line": "", "drafted": False}]
    assert suggest.draft_used(None, POSTING, "Kafka", ["A JOB THAT IS NOT ON THE RESUME  2020"]) == []


def test_the_job_page_asks_for_the_lines(used, monkeypatch):
    from backend.api import app as webapp
    from data_engineering.db import session as session_mod
    from data_engineering.db.models import Job
    from data_engineering.db.session import get_session, init_engine
    init_engine("sqlite:///:memory:")
    try:
        with get_session() as s:
            s.add(Job(id=7, source="greenhouse", company="Acme", company_slug="acme", external_id="x",
                      content_hash="h", title=POSTING.title, description=POSTING.description, is_open=True,
                      application_url="https://a"))
        monkeypatch.setattr(webapp, "cfg", lambda: None)
        model = Model({"lines": [{"job": 0, "line": "Ran Kafka clusters on EKS that carried release events."}]})
        monkeypatch.setattr("ml.resume.llm.complete", model)
        client = webapp.app.test_client()
        reply = client.post("/job/7/used/draft", json={"tool": "Kafka", "roles": used[:1]}).get_json()
        assert reply["ok"] and reply["lines"][0]["drafted"]
        assert client.post("/job/7/used/draft", json={"tool": "Kafka", "roles": []}).status_code == 400
        assert client.post("/job/99/used/draft", json={"tool": "Kafka", "roles": used[:1]}).status_code == 404
    finally:
        session_mod._engine = None
        session_mod._SessionFactory = None
