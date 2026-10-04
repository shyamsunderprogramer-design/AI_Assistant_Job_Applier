"""The page checks catch real layout problems, and pass a clean document."""

from pathlib import Path

import pytest
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

from ml.tailoring.documents import content_checks, visual_checks, word_to_pdf

pytest.importorskip("pypdfium2")


def pdf(path, pages):
    """pages: list of [(x, y, text)] per page."""
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont("Helvetica", 11)
    for items in pages:
        for x, y, text in items:
            c.drawString(x, y, text)
        c.showPage()
    c.save()
    return path


def body(n, top=720):
    return [(72, top - i * 16, f"Line {i} of ordinary resume text about work done.") for i in range(n)]


def kinds(checks):
    return {c.kind for c in checks}


def test_a_clean_document_passes(tmp_path):
    p = pdf(tmp_path / "ok.pdf", [[(72, 740, "EXPERIENCE")] + body(30), body(20)])
    assert visual_checks(p, "pdf", ["EXPERIENCE"]) == []


def test_text_past_the_margin_is_clipped(tmp_path):
    p = pdf(tmp_path / "c.pdf", [body(10) + [(560, 400, "This line runs off the right edge of the page")]])
    assert "clipped text" in kinds(visual_checks(p, "pdf", []))


def test_overlapping_lines_are_caught(tmp_path):
    p = pdf(tmp_path / "o.pdf", [body(10) + [(72, 704, "Overlapping words printed over a line")]])
    assert "overlapping text" in kinds(visual_checks(p, "pdf", []))


def test_a_heading_alone_at_the_foot_of_a_page(tmp_path):
    p = pdf(tmp_path / "h.pdf", [body(30) + [(72, 60, "EDUCATION")], body(10)])
    assert any(c.kind == "page break" and "EDUCATION" in c.detail for c in visual_checks(p, "pdf", ["EDUCATION"]))


def test_a_nearly_empty_last_page(tmp_path):
    p = pdf(tmp_path / "e.pdf", [body(40), body(2)])
    assert any("last page" in c.detail for c in visual_checks(p, "pdf", []))


def test_a_missing_section_and_a_missing_line(tmp_path):
    p = pdf(tmp_path / "m.pdf", [[(72, 740, "EXPERIENCE")] + body(10)])
    assert "missing section" in kinds(visual_checks(p, "pdf", ["EXPERIENCE", "EDUCATION"]))
    blocks = [("bullet", "Line 3 of ordinary resume text about work done."),
              ("bullet", "Migrated forty services to containers without downtime.")]
    found = content_checks(p, "pdf", blocks)
    assert len(found) == 1 and "1 line(s)" in found[0].detail


@pytest.mark.skipif(not Path("/Applications/Microsoft Word.app").exists(), reason="needs Microsoft Word")
def test_word_lays_the_docx_out_as_pages(tmp_path):
    from docx import Document
    d = Document()
    d.add_paragraph("A short document for Word to lay out.")
    d.save(tmp_path / "w.docx")
    assert word_to_pdf(tmp_path / "w.docx", tmp_path / "w.pdf") and (tmp_path / "w.pdf").stat().st_size > 0
