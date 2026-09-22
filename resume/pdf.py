"""The same resume as a PDF, drawn rather than converted.

Employers ask for PDF at least as often as .docx, and the alternative to this
is opening Word by hand for every application -- which is the manual step the
rest of the tool exists to remove.

**Drawn from the shared plan, not converted from the .docx.** Conversion needs
LibreOffice or Word installed, produces a different result on every machine,
and silently re-flows the layout that `resume/writer.py` was careful about.
Both renderers read `plan_document()` instead, so the two files cannot
disagree about what is a bullet, a job title or a section -- deciding twice is
how two exports of "the same" resume become different documents.

The PDF stays ATS-safe on the same terms as the .docx: real selectable text in
one column, no tables, no images, no text boxes, and the section headings in
the reading order a parser walks. A PDF only breaks a parser when the text is
an image or the columns interleave, and neither happens here.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from reportlab.lib.colors import Color
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, HRFlowable,
                                PageTemplate, Paragraph, Spacer)

from resume.parser import Resume
from resume.tailor import TailorResult
from resume.writer import _tidy_contact, plan_document

log = logging.getLogger(__name__)

# A real TrueType font is embedded rather than a PDF base-14 font, and the
# reason is extraction, not looks. With base-14 Helvetica a bullet comes back
# out of the file as \x7f -- a control character at the head of every bullet,
# which is what an ATS would read. Embedded, it extracts as a bullet. Checked
# both ways with pypdf before choosing.
#
# reportlab's `bulletText=` argument has the same fault whatever the font, so
# the bullet goes in the text flow instead, exactly as the .docx does it.
_CANDIDATES = [
    ("Arial", "/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]

BODY = 10.5
INK = Color(0.10, 0.10, 0.10)
MUTED = Color(0.33, 0.33, 0.33)
RULE = Color(0.75, 0.75, 0.75)

SIDE_MARGIN = 0.65 * inch
TOP_MARGIN = 0.5 * inch
BULLET_INDENT = 11
BULLET_HANG = 11


def _register_font() -> tuple[str, str]:
    """(regular, bold) font names, embedding a TrueType face where one exists.

    Falls back to reportlab's bundled Vera, then to base-14 Helvetica. The
    fallback still renders a correct resume -- only the bullet glyph degrades,
    and BULLET degrades with it rather than putting a control character into
    the text layer.
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for name, regular, bold in _CANDIDATES:
        if not Path(regular).exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, regular))
            bold_name = name
            if Path(bold).exists():
                bold_name = f"{name}-Bold"
                pdfmetrics.registerFont(TTFont(bold_name, bold))
            return name, bold_name
        except Exception:                      # a broken font file is not fatal
            continue

    try:
        import reportlab
        vera = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
        if vera.exists():
            pdfmetrics.registerFont(TTFont("Vera", str(vera)))
            bold = vera.with_name("VeraBd.ttf")
            if bold.exists():
                pdfmetrics.registerFont(TTFont("Vera-Bold", str(bold)))
                return "Vera", "Vera-Bold"
            return "Vera", "Vera"
    except Exception:
        pass
    return "Helvetica", "Helvetica-Bold"


FONT, FONT_BOLD = _register_font()

# Only a font that can carry it gets the real bullet.
BULLET = "-" if FONT == "Helvetica" else "\u2022"


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle(
        "body", fontName=FONT, fontSize=BODY, leading=BODY * 1.30,
        textColor=INK, spaceAfter=2.5,
    )
    return {
        "name": ParagraphStyle("name", parent=base, fontName=FONT_BOLD,
                               fontSize=19, leading=22, alignment=TA_CENTER,
                               spaceAfter=1),
        "contact": ParagraphStyle("contact", parent=base, fontSize=9.5,
                                  leading=12, alignment=TA_CENTER,
                                  textColor=MUTED, spaceAfter=1),
        "heading": ParagraphStyle("heading", parent=base, fontName=FONT_BOLD,
                                  fontSize=10.5, leading=13, spaceBefore=11,
                                  spaceAfter=3),
        "role": ParagraphStyle("role", parent=base, spaceBefore=7, spaceAfter=1),
        "subheading": ParagraphStyle("subheading", parent=base,
                                     fontName=FONT_BOLD, spaceBefore=5,
                                     spaceAfter=1),
        # The hanging indent: the text block starts past the bullet, and the
        # first line steps back to where the bullet sits.
        "bullet": ParagraphStyle("bullet", parent=base,
                                 leftIndent=BULLET_INDENT + BULLET_HANG,
                                 # the bullet is part of the text now, so the
                                 # hang must clear it and its two spaces
                                 firstLineIndent=-(BULLET_INDENT + BULLET_HANG)),
        "text": base,
    }


def _escape(text: str) -> str:
    """Paragraph text is mini-HTML to reportlab, so & < > must be escaped."""
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _role_markup(text: str, width: float) -> str:
    """A job title with its dates against the right margin.

    A right-aligned tab, the same device the .docx uses. Spaces would move
    with the font and a table is the thing parsers choke on.
    """
    from resume.writer import _split_dates

    title, dates = _split_dates(text)
    head = f"<b>{_escape(title)}</b>"
    if not dates:
        return head
    # The tab positions the dates on the page but leaves NOTHING in the text
    # layer, so "Master of Science" and "Jan 2023" extract glued together and
    # a parser reads one nonsense token. Two spaces before it cost nothing
    # visually -- the tab still pushes the dates to the margin -- and keep the
    # two apart for anything reading the text rather than the page.
    return (f'{head}&nbsp;&nbsp;<tab/>'
            f'<font color="#555555">{_escape(dates)}</font>')



class _Rule(HRFlowable):
    """The hairline under a section heading."""

    def __init__(self, width):
        super().__init__(width="100%", thickness=0.4, color=RULE,
                         spaceBefore=1, spaceAfter=4, lineCap="butt")


def write_pdf(base: Resume, result: TailorResult, output_path: Path | str,
              header_lines: list[str] | None = None) -> Path:
    """Render the tailored resume to PDF. Same content as the .docx."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    page_w, page_h = LETTER
    text_w = page_w - 2 * SIDE_MARGIN
    styles = _styles()

    # A right tab at the margin, so role dates land there rather than
    # wherever the source happened to space them.
    for name in ("role",):
        styles[name].tabs = [(text_w, "right")]

    story: list[Flowable] = []
    for kind, text in plan_document(base, result, header_lines):
        if kind == "name":
            story.append(Paragraph(_escape(text), styles["name"]))
        elif kind == "contact":
            story.append(Paragraph(_escape(_tidy_contact(text)), styles["contact"]))
        elif kind == "heading":
            story.append(Paragraph(_escape(text.upper()), styles["heading"]))
            story.append(_Rule(text_w))
        elif kind == "role":
            story.append(Paragraph(_role_markup(text, text_w), styles["role"]))
        elif kind == "subheading":
            story.append(Paragraph(f"<b>{_escape(text)}</b>", styles["subheading"]))
        elif kind == "bullet":
            story.append(Paragraph(
                f"{BULLET}&nbsp;&nbsp;{_escape(text)}", styles["bullet"]))
        else:
            story.append(Paragraph(_escape(text), styles["text"]))

    doc = BaseDocTemplate(
        str(output_path), pagesize=LETTER,
        leftMargin=SIDE_MARGIN, rightMargin=SIDE_MARGIN,
        topMargin=TOP_MARGIN, bottomMargin=TOP_MARGIN,
        title=_document_title(base, result), author=_author(base),
    )
    frame = Frame(SIDE_MARGIN, TOP_MARGIN, text_w,
                  page_h - TOP_MARGIN * 2, id="body",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="resume", frames=[frame])])
    doc.build(story)

    log.info("Wrote ATS-safe PDF: %s", output_path)
    return output_path


def _author(base: Resume) -> str:
    """The PDF's author field: the person's own name, off the resume.

    Left blank it says "anonymous" in a reader's properties pane, which is a
    small thing to get wrong on a document about who you are.
    """
    for section in base.sections:
        if section.heading == "HEADER" and section.lines:
            return section.lines[0].strip()[:80]
    return ""


def _document_title(base: Resume, result: TailorResult) -> str:
    name = _author(base)
    return f"{name} — Resume" if name else "Resume"
