"""The page checks catch real layout problems, and pass a clean document."""

import os
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


def test_a_federal_date_is_the_same_date_and_a_lost_one_is_still_caught(tmp_path):
    # 21 Oct 2026: the Federal template writes "Feb 2025" as "02/2025", and every date of the
    # resume was reported missing. A date that really is gone still is.
    from types import SimpleNamespace
    p = pdf(tmp_path / "f.pdf", [[(72, 740, "SENIOR DEVOPS ENGINEER, ACME 02/2025 - Present")] + body(5)])
    model = SimpleNamespace(entries=[SimpleNamespace(employer="Acme", dates="Feb 2025 - Present"),
                                     SimpleNamespace(employer="Acme", dates="Apr 2018 - Dec 2022")])
    found = content_checks(p, "pdf", [], model=model)
    assert [c.detail for c in found] == ["“Apr 2018 - Dec 2022” from the original resume is missing."]


# Opens the real Microsoft Word, so only when asked for (conftest lets osascript through then).
@pytest.mark.skipif(not Path("/Applications/Microsoft Word.app").exists()
                    or os.environ.get("JOB_APPLIER_LIVE_WORD") != "1",
                    reason="opens Microsoft Word itself: run with JOB_APPLIER_LIVE_WORD=1")
def test_word_lays_the_docx_out_as_pages(tmp_path, monkeypatch):
    from docx import Document
    from ml.tailoring import documents
    monkeypatch.setattr(documents, "use_word", lambda: True)
    d = Document()
    d.add_paragraph("A short document for Word to lay out.")
    d.save(tmp_path / "w.docx")
    assert word_to_pdf(tmp_path / "w.docx", tmp_path / "w.pdf") and (tmp_path / "w.pdf").stat().st_size > 0


def test_word_is_left_alone_unless_switched_on(tmp_path, monkeypatch):
    from ml.tailoring import documents
    monkeypatch.setattr(documents, "use_word", lambda: False)
    monkeypatch.setattr(documents, "_word_save_pdf", lambda *a: pytest.fail("Word must not be opened"))
    assert word_to_pdf(tmp_path / "w.docx", tmp_path / "w.pdf") is False
