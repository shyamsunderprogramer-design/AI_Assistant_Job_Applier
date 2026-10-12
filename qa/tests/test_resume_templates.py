"""Templates: the same words, a different look; every one ATS-safe."""

from docx import Document

from ml.resume.parser import parse_resume
from ml.resume.style import TEMPLATES, style_for
from ml.resume.tailor import TailorResult
from ml.resume.writer import write_tailored_resume


def _resume(tmp_path):
    src = tmp_path / "in.docx"
    d = Document()
    for line in ("Jordan Example", "Austin, TX | jordan@example.com", "Experience",
                 "Engineer, Acme   2019 – 2023", "• Built deployment pipelines for forty services."):
        d.add_paragraph(line)
    d.save(src)
    return parse_resume(src)


def test_each_template_sets_its_own_font_and_heading_colour(tmp_path):
    base = _resume(tmp_path)
    for key, st in TEMPLATES.items():
        out = write_tailored_resume(base, TailorResult(summary=None, template=key), tmp_path / f"{key}.docx")
        doc = Document(str(out))
        assert doc.styles["Normal"].font.name == st.font
        assert str(doc.styles["Heading 1"].font.color.rgb) == st.heading_color
        assert not doc.tables                                   # ATS-safe whatever the look
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "Built deployment pipelines" in text


def test_a_render_leaves_the_next_one_untouched(tmp_path):
    base = _resume(tmp_path)
    write_tailored_resume(base, TailorResult(summary=None, template="compact"), tmp_path / "a.docx")
    doc = Document(str(write_tailored_resume(base, TailorResult(summary=None), tmp_path / "b.docx")))
    assert doc.styles["Normal"].font.size.pt == TEMPLATES["classic"].body


def test_an_unknown_template_falls_back_to_the_default():
    assert style_for(TailorResult(summary=None, template="neon")).key == "classic"
    assert style_for(None).key == "classic"


def test_the_pdf_draws_each_template(tmp_path):
    from pypdf import PdfReader
    from ml.resume.pdf import write_pdf
    base = _resume(tmp_path)
    for key in TEMPLATES:
        pdf = write_pdf(base, TailorResult(summary=None, template=key), tmp_path / f"{key}.pdf")
        assert "Built deployment pipelines" in PdfReader(str(pdf)).pages[0].extract_text()


def test_a_title_is_never_left_at_the_foot_of_a_page():
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import KeepTogether, Paragraph
    from ml.resume.pdf import _kept_with_next
    title = Paragraph("Leadership", ParagraphStyle("t", keepWithNext=1))
    bullet = KeepTogether([Paragraph("Led teams.", ParagraphStyle("b"))])
    out = _kept_with_next([title, bullet, Paragraph("After.", ParagraphStyle("b"))])
    assert len(out) == 2 and isinstance(out[0], KeepTogether)
    assert out[0]._content[0] is title                     # the title travels with its bullet
