"""Editing a cover letter: parsed, checked like the written one, versions kept, documents written."""

import pytest

from ml.resume import letter_edit as le

RESUME = "Jane Doe\nDevOps engineer at Acme. Built Terraform modules and Jenkins pipelines for 40 services."
LETTER = """Dear Hiring Manager,

I build CI/CD platforms. At Acme I moved 40 services onto Jenkins pipelines.

Globex ships every week, and a slow build slows every team.

I would want to start with the flakiest pipeline.

Sincerely,
Jane Doe
"""


def test_parse_and_join_round_trip():
    parts = le.parse(LETTER)
    assert parts["greeting"] == "Dear Hiring Manager," and len(parts["paragraphs"]) == 3
    assert parts["closing"] == "Sincerely,\nJane Doe"
    assert le.join(parts).strip() == LETTER.strip()


def test_a_clean_letter_has_no_problems():
    out = le.check(LETTER, RESUME, "Globex wants a build engineer.", "Globex")
    assert out["ok"] and not [f for f in out["findings"] if f["severity"] != "tip"], out["findings"]


def test_claims_placeholders_stock_and_job_back_are_found():
    bad = LETTER.replace("I build CI/CD platforms.",
                         "I am writing to express my interest. The role requires Kubernetes. I ran Kubernetes for [Company].")
    titles = " | ".join(f["title"] for f in le.check(bad, RESUME, "Globex wants Kubernetes.", "Globex")["findings"])
    assert "Not on your resume" in titles and "kubernetes" in titles.lower()
    assert "placeholder" in titles and "Stock phrases" in titles and "job back" in titles


def test_save_keeps_the_version_it_replaces_and_writes_documents(tmp_path):
    first = le.save(tmp_path, LETTER, ["Jane Doe", "Austin, TX · jane@example.com"])
    assert first["saved"] and (tmp_path / "cover-letter.md").exists()
    assert (tmp_path / le.DOCX_FILE).exists() and (tmp_path / le.PDF_FILE).exists()
    assert not le.versions(tmp_path)
    le.save(tmp_path, LETTER.replace("flakiest", "slowest"), ["Jane Doe"])
    kept = le.versions(tmp_path)
    assert len(kept) == 1 and "flakiest" not in (tmp_path / "cover-letter.md").read_text()
    text = le.restore(tmp_path, kept[0]["id"])
    assert "flakiest" in text and "flakiest" in (tmp_path / "cover-letter.md").read_text()
    assert len(le.versions(tmp_path)) >= 1
    from pypdf import PdfReader
    pdf_text = PdfReader(str(tmp_path / le.PDF_FILE)).pages[0].extract_text()
    assert "Jane Doe" in pdf_text and "flakiest" in pdf_text


def test_saving_the_same_text_keeps_no_extra_version(tmp_path):
    le.save(tmp_path, LETTER, [])
    assert le.save(tmp_path, LETTER, [])["saved"] is False and not le.versions(tmp_path)


def test_a_rewrite_that_adds_a_claim_is_flagged():
    out = le.rewrite_paragraph("I build CI/CD platforms.", "plainer", RESUME, "", "Globex", "Build engineer", None,
                               ask=lambda s, u, c: {"paragraph": "I ran Kubernetes for 9 years — mostly at scale."})
    assert "—" not in out["paragraph"]
    assert {c["value"].lower() for c in out["claims"]} & {"kubernetes", "9"}


def test_a_rewrite_needs_a_way_and_a_paragraph():
    with pytest.raises(ValueError):
        le.rewrite_paragraph("Text.", "louder", RESUME, "", "", "", None, ask=lambda *a: {})
    with pytest.raises(ValueError):
        le.rewrite_paragraph("  ", "shorter", RESUME, "", "", "", None, ask=lambda *a: {})
