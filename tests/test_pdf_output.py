"""The PDF must parse the way an ATS reads it, not merely look right.

A PDF that renders beautifully and extracts to control characters is worse
than a plain one that extracts cleanly, and that was the first version: with
a base-14 font a bullet came back out of the file as \x7f, so every bullet
began with a control character in the text layer an ATS reads.
"""

import re

import pytest

from resume.parser import Resume, Section
from resume.tailor import TailorResult

pytest.importorskip("reportlab")
pytest.importorskip("pypdf")

BASE = Resume(
    raw_text="Jane Roe\nBoston, MA | +1 555 0100 | jane@example.com\n",
    sections=[
        Section(heading="HEADER", lines=[
            "Jane Roe", "Boston, MA | +1 555 0100 | jane@example.com"]),
        Section(heading="EXPERIENCE", lines=[
            "SENIOR PLATFORM ENGINEER, ACME CORP\tFeb 2025 - Present",
            # Long enough to read as a list: _section_is_a_list needs real
            # sentence-length lines, which is what a bullet actually is.
            "Ran the weekly release train for eleven services across three regions.",
            "Cut the median deploy from forty minutes to under six by reworking the pipeline.",
            "Held the on-call rota and wrote the runbooks that the rota itself depends on.",
        ]),
        Section(heading="EDUCATION", lines=["Master of Science\tJan 2023 - Dec 2024"]),
    ])


@pytest.fixture
def extracted(tmp_path):
    from pypdf import PdfReader
    from resume.pdf import write_pdf

    path = write_pdf(BASE, TailorResult(summary="Platform engineer.", bullets=[]),
                     tmp_path / "r.pdf")
    reader = PdfReader(path)
    return reader, "\n".join(p.extract_text() or "" for p in reader.pages)


def test_the_text_layer_has_no_control_characters(extracted):
    """The defect that made the first version unusable for an ATS."""
    _, text = extracted
    assert "\x7f" not in text
    assert "�" not in text


def test_bullets_survive_extraction(extracted):
    _, text = extracted
    assert text.count("•") >= 3, "a parser would see prose, not a list"


def test_a_parser_finds_what_it_looks_for(extracted):
    _, text = extracted
    assert re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "no email"
    assert re.search(r"\d[\d\s().-]{7,}\d", text), "no phone"
    assert "EXPERIENCE" in text.upper()
    assert "EDUCATION" in text.upper()
    assert re.search(r"(19|20)\d{2}", text), "no dates"


def test_the_content_is_selectable_text_not_an_image(extracted):
    _, text = extracted
    assert len(text) > 200


def test_the_document_says_whose_it_is(extracted):
    """Blank metadata shows as "anonymous" in a reader's properties pane."""
    reader, _ = extracted
    assert reader.metadata.author == "Jane Roe"
    assert "Jane Roe" in (reader.metadata.title or "")


def test_both_formats_render_the_same_plan(tmp_path):
    """The .docx and the PDF must not disagree about what is a bullet."""
    from docx import Document
    from pypdf import PdfReader

    from resume.pdf import write_pdf
    from resume.writer import write_tailored_resume

    result = TailorResult(summary="Platform engineer.", bullets=[])
    docx_path = write_tailored_resume(BASE, result, tmp_path / "r.docx")
    pdf_path = write_pdf(BASE, result, tmp_path / "r.pdf")

    docx_bullets = sum(1 for p in Document(docx_path).paragraphs
                       if p.text.strip().startswith("•"))
    pdf_text = "\n".join(p.extract_text() or "" for p in PdfReader(pdf_path).pages)
    assert docx_bullets == pdf_text.count("•"), "the two formats disagree"


def test_a_pdf_failure_never_costs_the_docx(tmp_path, monkeypatch):
    """reportlab is optional; the document that already succeeded must survive."""
    from resume import pipeline

    def boom(*args, **kwargs):
        raise RuntimeError("no font")

    monkeypatch.setattr("resume.pdf.write_pdf", boom)
    target = tmp_path / "r.docx"
    pipeline._write_both(BASE, TailorResult(summary=None, bullets=[]), target)
    assert target.exists(), "the .docx was lost to a PDF failure"


def test_a_job_title_does_not_glue_itself_to_its_dates(extracted):
    """The tab positions the dates but leaves nothing in the text layer, so
    "Master of Science" and "Jan 2023" extracted as one token."""
    _, text = extracted
    assert "ScienceJan" not in text
    assert "CORPFeb" not in text
