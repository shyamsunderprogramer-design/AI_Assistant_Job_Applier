"""Building a resume from scratch: the form, the documents, and the model kept to what the person wrote."""

import pytest

from ml.resume import builder


@pytest.fixture(autouse=True)
def folder(monkeypatch, tmp_path):
    monkeypatch.setattr(builder, "FOLDER", tmp_path)
    monkeypatch.setattr(builder, "DATA", tmp_path / "resume.json")
    monkeypatch.setattr(builder, "OUT", tmp_path / "out")
    monkeypatch.setattr(builder, "DOCX", tmp_path / "out" / "resume.docx")
    monkeypatch.setattr(builder, "PDF", tmp_path / "out" / "resume.pdf")
    monkeypatch.setattr(builder, "BACKUPS", tmp_path / "replaced")
    return tmp_path


def form():
    d = builder.empty()
    d["contact"].update(name="Alex Rivera", email="alex@example.com", phone="555-010-2030", city="Austin",
                        region="TX", linkedin="https://www.linkedin.com/in/alex-demo/")
    d["summary"] = "Data analyst with 4 years in retail analytics."
    d["experience"] = [
        {"id": "j1", "title": "Data Analyst", "company": "Bluebonnet", "location": "Austin, TX", "start": "2022-06",
         "end": "", "current": True, "notes": "tableau dashboards for 12 managers",
         "bullets": ["Built Tableau dashboards for 12 store managers.", "Cut a weekly report from 6 hours to 1."]},
        {"id": "j2", "title": "Intern", "company": "Lone Star", "location": "", "start": "2021-05", "end": "2021-08",
         "current": False, "notes": "", "bullets": ["Tracked shipments in Excel."]}]
    d["skills"] = [{"id": "s1", "label": "Analytics", "items": "SQL, Excel"}]
    d["education"] = [{"id": "e1", "degree": "Bachelor of Science", "field": "Statistics", "school": "UT Austin",
                       "location": "Austin, TX", "start": "2018-08", "end": "2022-05", "details": []}]
    return d


def test_clean_keeps_only_the_form_shapes():
    d = builder.clean({"contact": {"name": "A", "evil": "x"}, "experience": [{"title": "T", "start": "June",
                                                                               "bullets": "• one\n\n- two"}],
                       "template": "nope"})
    assert "evil" not in d["contact"] and d["experience"][0]["start"] == ""
    assert d["experience"][0]["bullets"] == ["one", "two"] and d["template"] == "classic"


def test_the_form_becomes_the_lines_the_writers_draw():
    base, header = builder.to_resume(form())
    assert header == ["Alex Rivera", "Austin, TX · 555-010-2030 · alex@example.com · linkedin.com/in/alex-demo"]
    exp = next(s for s in base.sections if s.heading == "Experience")
    assert exp.lines[0] == "Data Analyst, Bluebonnet, Austin, TX\tJun 2022 – Present"
    assert exp.lines[1] == "• Built Tableau dashboards for 12 store managers."
    edu = next(s for s in base.sections if s.heading == "Education").lines
    assert edu == ["Bachelor of Science in Statistics\tAug 2018 – May 2022", "UT Austin, Austin, TX"]


def test_render_makes_both_documents_with_the_lines_in_them():
    out = builder.render(form())
    assert builder.DOCX.exists() and builder.PDF.exists() and out["count"] == 1
    from ml.resume.ats import pdf_text
    text = pdf_text(builder.PDF)
    assert "Built Tableau dashboards for 12 store managers." in text and "Bluebonnet" in text
    assert not [c for c in out["checks"] if c["severity"] == "problem"]


def test_gaps_name_what_is_missing():
    d = form()
    d["contact"]["email"] = ""
    d["experience"][1]["end"] = ""
    msgs = [g["message"] for g in builder.gaps(d)]
    assert "An email address." in msgs and any("when it ended" in m for m in msgs)


def test_years_add_up_the_jobs_not_the_gap_after_an_internship():
    d = form()
    d["experience"][0]["current"] = False
    d["experience"][0]["end"] = "2023-05"          # Jun 2022 – May 2023: 12 months; the intern summer: 4
    assert builder.years_of(d) == round(16 / 12, 1)


def test_skills_named_in_the_jobs_but_not_listed_are_suggested():
    assert builder.suggest_skills(form()) == ["Tableau"]


def test_bullets_from_notes_flag_what_the_notes_never_said():
    reply = {"bullets": ["Built tableau dashboards for 12 managers.", "Cut costs 30% with Snowflake."],
             "ask": ["How many dashboards?"]}
    out = builder.write_bullets(form(), "experience", "j1", None, ask=lambda s, u, c: reply)
    first, second = out["bullets"]
    assert first["text"] == "Built Tableau dashboards for 12 managers." and not first["unsupported_numbers"]
    assert second["unsupported_numbers"] == ["30"] and second["unsupported_tools"] == ["snowflake"]
    assert out["ask"] == ["How many dashboards?"]


def test_bullets_need_notes_first():
    d = form()
    d["experience"][1]["bullets"] = []
    with pytest.raises(ValueError):
        builder.write_bullets(d, "experience", "j2", None, ask=lambda *a: {})


def test_a_summary_with_a_number_not_on_the_form_is_flagged():
    out = builder.write_summary(form(), None, ask=lambda s, u, c: {"summary": "Analyst with 9 years of SQL."})
    assert out["unsupported_numbers"] == ["9"]


def test_save_and_load_round_trip():
    saved = builder.save(form())
    assert saved["updated"] and builder.load()["experience"][0]["bullets"][0].startswith("Built Tableau")


def test_making_it_the_master_keeps_the_old_one(tmp_path):
    builder.render(form())
    resumes = tmp_path / "resumes"
    resumes.mkdir()
    (resumes / "base_resume.pdf").write_bytes(b"old")
    target, kept = builder.make_master(resumes)
    assert target.name == "base_resume.docx" and target.exists()
    assert kept.read_bytes() == b"old" and not (resumes / "base_resume.pdf").exists()


def test_the_builder_pages_and_routes(monkeypatch):
    from backend.api import app as webapp
    monkeypatch.setattr(webapp, "cfg", lambda: None)
    c = webapp.app.test_client()
    assert b"Resume Builder" in c.get("/resume/build").data
    saved = c.post("/resume/build/save", json={"data": form()}).get_json()
    assert saved["ok"] and saved["suggest"] == ["Tableau"] and "j2" in saved["job_checks"]
    shown = c.post("/resume/build/preview", json={}).get_json()
    assert shown["ok"] and shown["count"] == 1 and 0 < shown["health"]["score"] <= 100
    assert c.get("/resume/build/download/pdf").status_code == 200
    assert c.get("/resume/build/page/" + shown["pages"][0]).status_code == 200
    assert c.get("/resume/build/page/..%2Fresume.json").status_code == 404
