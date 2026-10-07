"""Interview prep: links come from the app, the PDF is an index, only for applied jobs."""

import json

import pytest

from ml.interview import prep


def test_known_tools_link_to_their_official_docs():
    links = prep.links_for("Kubernetes")
    assert links[0] == {"label": "Official docs", "url": "https://kubernetes.io/docs/home/"}
    assert any("youtube.com/results" in l["url"] for l in links)


def test_unknown_topics_get_searches_not_guessed_urls():
    links = prep.links_for("Blameless postmortems")
    assert all(l["url"].startswith(("https://www.youtube.com/results", "https://www.google.com/search")) for l in links)


def test_the_models_own_links_are_never_used(monkeypatch, tmp_path):
    reply = {"likely_questions": [{"q": "Tell me about Kubernetes upgrades", "why": "posting"}],
             "topics": [{"topic": "Terraform", "why": "IaC", "links": [{"label": "x", "url": "https://made-up.example"}]}],
             "chance": {"rating": "Good", "why": "covers most"}}

    class Reply:
        text = json.dumps(reply)

    class Job:
        id, company, title, description = 9, "Acme", "SRE", "Kubernetes and Terraform."

    monkeypatch.setattr("ml.resume.llm.complete", lambda *a, **k: Reply())
    monkeypatch.setattr("ml.resume.pipeline.load_base_resume", lambda cfg: type("R", (), {"text": lambda s: "resume"})())
    monkeypatch.setattr(prep, "FOLDER", tmp_path)
    made = prep.prepare(None, Job())
    urls = [l["url"] for t in made["topics"] for l in t["links"]]
    assert "https://made-up.example" not in urls and urls[0].startswith("https://developer.hashicorp.com")
    assert prep.load(9)["chance"]["rating"] == "Good"


def test_the_pdf_is_an_index_of_links(tmp_path):
    pytest.importorskip("pypdf")
    from pypdf import PdfReader
    p = {"company": "Acme", "title": "SRE", "company_links": prep.company_links("Acme", "SRE"),
         "topics": [{"topic": "Kubernetes", "links": prep.links_for("Kubernetes")}],
         "likely_questions": [{"q": "Why SRE?"}], "scenarios": [{"q": "An outage you led"}]}
    pdf = prep.write_pdf(p, tmp_path / "prep.pdf")
    reader = PdfReader(str(pdf))
    uris = [a.get_object()["/A"]["/URI"] for page in reader.pages for a in (page.get("/Annots") or [])]
    assert "https://kubernetes.io/docs/home/" in uris and len(reader.pages) <= 2


def test_prep_is_only_for_applied_jobs(monkeypatch):
    from backend.api import app as webapp

    class Job:
        status = "Not Applied"
    monkeypatch.setattr(webapp, "cfg", lambda: None)

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, model, job_id):
            return Job()
    monkeypatch.setattr(webapp, "get_session", lambda: Session())
    reply = webapp.app.test_client().post("/job/5/prep")
    assert reply.status_code == 400 and "applied" in reply.get_json()["error"]


def test_a_number_not_on_the_resume_is_flagged(monkeypatch, tmp_path):
    reply = {"scenarios": [{"q": "Q", "star": {"situation": "s", "task": "t", "action": "a", "result": "cut costs by 30%"}},
                           {"q": "Q2", "star": {"situation": "s", "task": "t", "action": "a", "result": "cut costs by 99%"}}]}

    class Reply:
        text = json.dumps(reply)

    class Job:
        id, company, title, description = 3, "Acme", "SRE", "x"
    monkeypatch.setattr("ml.resume.llm.complete", lambda *a, **k: Reply())
    monkeypatch.setattr("ml.resume.pipeline.load_base_resume",
                        lambda cfg: type("R", (), {"text": lambda s: "Lowered cloud spend 30% with FinOps."})())
    monkeypatch.setattr(prep, "FOLDER", tmp_path)
    made = prep.prepare(None, Job())
    assert "check" not in made["scenarios"][0] and "99" in made["scenarios"][1]["check"]
