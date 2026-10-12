"""The same resume as a PDF, drawn rather than converted.

Employers ask for PDF at least as often as .docx, and the alternative to this
is opening Word by hand for every application -- which is the manual step the
rest of the tool exists to remove.

**Drawn from the shared plan, not converted from the .docx.** Conversion needs
LibreOffice or Word installed, produces a different result on every machine,
and silently re-flows the layout that `ml/resume/writer.py` was careful about.
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
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, HRFlowable, KeepTogether,
                                PageTemplate, Paragraph, Spacer)

from ml.resume.parser import Resume
from ml.resume.tailor import TailorResult
from ml.resume.writer import _tidy_contact, contact_link, plan_document

log = logging.getLogger(__name__)

# A real TrueType font is embedded rather than a PDF base-14 font, and the
# reason is extraction, not looks. With base-14 Helvetica a bullet comes back
# out of the file as \x7f -- a control character at the head of every bullet,
# which is what an ATS would read. Embedded, it extracts as a bullet. Checked
# both ways with pypdf before choosing.
#
# reportlab's `bulletText=` argument has the same fault whatever the font, so
# the bullet goes in the text flow instead, exactly as the .docx does it.
_WORD_FONTS = Path("/Applications/Microsoft Word.app/Contents/Resources/DFonts")
_CANDIDATES = [
    # The person's format: Cambria. Installed with Microsoft Office.
    ("Cambria", str(_WORD_FONTS / "Cambria.ttc"), str(_WORD_FONTS / "Cambriab.ttf")),
    ("Cambria", "/Library/Fonts/Cambria.ttc", "/Library/Fonts/Cambria Bold.ttf"),
    ("Calibri", str(_WORD_FONTS / "Calibri.ttf"), str(_WORD_FONTS / "Calibrib.ttf")),
    ("Calibri", "/Library/Fonts/Calibri.ttf", "/Library/Fonts/Calibri Bold.ttf"),
    ("Arial", "/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]

BODY = 11
LEADING = 1.17          # single line spacing, as Word sets Cambria
INK = Color(0, 0, 0)
MUTED = INK
RULE = Color(0.75, 0.75, 0.75)
HEADING_INK = INK
NAME_SIZE, HEAD_SIZE = 18, 12
GAP = 3
HEAD_BEFORE, HEAD_AFTER = 8, 4
ROLE_BEFORE, TITLE_AFTER = 6, 6

SIDE_MARGIN = 0.65 * inch
TOP_MARGIN = 0.65 * inch
BULLET_AT = 0.15 * inch          # the "•", from the left text margin
BULLET_TEXT_AT = 0.30 * inch     # its text, and every wrapped line


_REGISTERED: dict[str, tuple[str, str]] = {}


def _register_font(family: str | None = None) -> tuple[str, str]:
    """(regular, bold) font names, embedding a TrueType face where one exists.

    Falls back to reportlab's bundled Vera, then to base-14 Helvetica. The
    fallback still renders a correct resume -- only the bullet glyph degrades,
    and BULLET degrades with it rather than putting a control character into
    the text layer.
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if family in _REGISTERED:
        return _REGISTERED[family]
    # The template's family first, then any that is installed.
    for name, regular, bold in sorted(_CANDIDATES, key=lambda c: c[0] != family):
        if not Path(regular).exists():
            continue
        if name in _REGISTERED:
            return _REGISTERED[name]
        try:
            # A .ttc holds several faces; Cambria's first is the text face.
            extra = {"subfontIndex": 0} if regular.endswith(".ttc") else {}
            pdfmetrics.registerFont(TTFont(name, regular, **extra))
            bold_name = name
            if Path(bold).exists():
                bold_name = f"{name}-Bold"
                pdfmetrics.registerFont(TTFont(bold_name, bold))
            # A family, so <b> in a paragraph means this bold face. Without
            # it, <b> silently printed the regular weight: job titles came
            # out plain.
            from reportlab.lib.fonts import addMapping
            addMapping(name, 0, 0, name)
            addMapping(name, 1, 0, bold_name)
            addMapping(name, 0, 1, name)
            addMapping(name, 1, 1, bold_name)
            _REGISTERED[name] = (name, bold_name)
            if family:
                _REGISTERED[family] = (name, bold_name)
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


FONT, FONT_BOLD = _register_font("Cambria")

# Only a font that can carry it gets the real bullet.
BULLET = "-" if FONT == "Helvetica" else "\u2022"

import threading
_STYLE_LOCK = threading.RLock()


def _hex(value: str) -> Color:
    v = value.lstrip("#")
    return Color(int(v[0:2], 16) / 255, int(v[2:4], 16) / 255, int(v[4:6], 16) / 255)


def _apply_style(st) -> None:
    """Set this module's type and spacing from a template (ml/resume/style.py)."""
    global FONT, FONT_BOLD, BULLET, BODY, LEADING, INK, MUTED, RULE, HEADING_INK, NAME_SIZE, HEAD_SIZE
    global GAP, HEAD_BEFORE, HEAD_AFTER, ROLE_BEFORE, TITLE_AFTER, SIDE_MARGIN, TOP_MARGIN
    global BULLET_AT, BULLET_TEXT_AT
    FONT, FONT_BOLD = _register_font(st.font)
    BULLET = "-" if FONT == "Helvetica" else "\u2022"
    BODY, LEADING, NAME_SIZE, HEAD_SIZE = st.body, st.leading, st.name, st.heading
    INK = MUTED = _hex(st.ink)
    HEADING_INK, RULE = _hex(st.heading_color), _hex(st.rule_color)
    GAP, HEAD_BEFORE, HEAD_AFTER = st.gap, st.heading_before, st.heading_after
    ROLE_BEFORE, TITLE_AFTER = st.role_before, st.title_after
    SIDE_MARGIN = TOP_MARGIN = st.margin_in * inch
    BULLET_AT, BULLET_TEXT_AT = st.bullet_at_in * inch, st.bullet_text_in * inch


def _bullet_width() -> float:
    return BULLET_TEXT_AT


def _pack(items: list[str], width: float, size: float) -> list[str]:
    """Whole contact items per line, as many as fit.

    Left to wrap on its own, the line broke inside an item and left a "·"
    hanging at a line's end, the LinkedIn URL alone below it.
    """
    lines: list[list[str]] = [[]]
    for item in items:
        trial = " \u00b7 ".join(lines[-1] + [item])
        if lines[-1] and stringWidth(trial, FONT, size) > width:
            lines.append([item])
        else:
            lines[-1].append(item)
    return [" \u00b7 ".join(line) for line in lines if line]


def _styles() -> dict[str, ParagraphStyle]:
    """The template's type: one face, single spaced, GAP points after body lines."""
    base = ParagraphStyle(
        "body", fontName=FONT, fontSize=BODY, leading=BODY * LEADING,
        textColor=INK, spaceBefore=0, spaceAfter=GAP,
    )
    return {
        "name": ParagraphStyle("name", parent=base, fontName=FONT_BOLD,
                               fontSize=NAME_SIZE, leading=NAME_SIZE * LEADING, alignment=TA_CENTER,
                               spaceAfter=3),
        "contact": ParagraphStyle("contact", parent=base, alignment=TA_CENTER, spaceAfter=3),
        "title": ParagraphStyle("title", parent=base, fontName=FONT_BOLD,
                                alignment=TA_CENTER, spaceAfter=TITLE_AFTER),
        # keepWithNext: a heading, a group title or a role line is never
        # left alone at the foot of a page with its lines on the next.
        "heading": ParagraphStyle("heading", parent=base, fontName=FONT_BOLD, textColor=HEADING_INK,
                                  fontSize=HEAD_SIZE, leading=HEAD_SIZE * LEADING, spaceBefore=HEAD_BEFORE,
                                  spaceAfter=1, keepWithNext=1),
        "role": ParagraphStyle("role", parent=base, fontName=FONT_BOLD, spaceBefore=ROLE_BEFORE,
                               spaceAfter=GAP, keepWithNext=1),
        "degree": ParagraphStyle("degree", parent=base, spaceBefore=max(2, ROLE_BEFORE - 2), keepWithNext=1),
        "subheading": ParagraphStyle("subheading", parent=base,
                                     fontName=FONT_BOLD, spaceBefore=2,
                                     spaceAfter=GAP, keepWithNext=1),
        # The bullet at its position, its text and every wrapped line at the text position.
        "bullet": ParagraphStyle("bullet", parent=base, alignment=TA_JUSTIFY,
                                 leftIndent=BULLET_TEXT_AT, bulletIndent=BULLET_AT,
                                 bulletFontName=FONT, bulletFontSize=BODY),
        # Body text justified: straight right edges, the last line of each
        # paragraph left as it falls.
        "text": ParagraphStyle("text", parent=base, alignment=TA_JUSTIFY),
        "skill": ParagraphStyle("skill", parent=base),          # left aligned
    }


def _escape(text: str) -> str:
    """Paragraph text is mini-HTML to reportlab, so & < > must be escaped."""
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _role_markup(text: str, width: float, bold: bool = True) -> str:
    """A job title with its dates against the right margin.

    A right-aligned tab, the same device the .docx uses. Spaces would move
    with the font and a table is the thing parsers choke on.
    """
    from ml.resume.writer import _split_dates

    title, dates = _split_dates(text)
    head = f"<b>{_escape(title)}</b>" if bold else _escape(title)
    if not dates:
        return head
    # The tab positions the dates on the page but leaves NOTHING in the text
    # layer, so "Master of Science" and "Jan 2023" extract glued together and
    # a parser reads one nonsense token. Two spaces before it cost nothing
    # visually -- the tab still pushes the dates to the margin -- and keep the
    # two apart for anything reading the text rather than the page.
    tail = f"<b>{_escape(dates)}</b>" if bold else _escape(dates)
    return f'{head}&nbsp;&nbsp;<tab/>{tail}'



class _DatedLine(Flowable):
    """A title on the left and its dates against the right margin, on one line.

    reportlab's paragraphs ignore tab stops, so the dates sat straight after
    the title. Drawn as two strings instead: the text layer still reads title
    then dates, in that order, on one line.
    """

    def __init__(self, title: str, dates: str, font: str, size: float,
                 space_before: float, space_after: float):
        super().__init__()
        self.title, self.dates, self.font, self.size = title, dates, font, size
        self.spaceBefore, self.spaceAfter = space_before, space_after
        self.keepWithNext = 1           # a job is never split from its first line

    def fits(self, width: float) -> bool:
        gap = stringWidth("    ", self.font, self.size)
        return (stringWidth(self.title, self.font, self.size) + gap
                + stringWidth(self.dates, self.font, self.size)) <= width

    def wrap(self, avail_w, avail_h):
        self.width = avail_w
        return avail_w, self.size * LEADING

    def draw(self):
        c = self.canv
        c.setFillColor(INK)
        c.setFont(self.font, self.size)
        baseline = self.size * (LEADING - 1) / 2 + self.size * 0.22
        c.drawString(0, baseline, self.title + "  ")
        c.drawRightString(self.width, baseline, self.dates)


def _dated(text: str, width: float, font: str, style: ParagraphStyle):
    """The dated line, or a paragraph when title and dates will not share a line."""
    from ml.resume.writer import _split_dates
    title, dates = _split_dates(text)
    if dates:
        line = _DatedLine(title, dates, font, style.fontSize, style.spaceBefore, style.spaceAfter)
        if line.fits(width):
            return line
    bold = font == FONT_BOLD
    return Paragraph(_role_markup(text, width, bold=bold), style)


def _kept_with_next(story: list) -> list:
    """Each heading, job line or group title bound to the line after it.

    keepWithNext alone did not hold when the next line was itself kept whole
    (every bullet is): "Leadership, Collaboration & Agile Enablement" was left
    at the foot of page 1 with its bullet on page 2. Grouped explicitly, the
    title moves with its first line.
    """
    out, pending = [], []
    for f in story:
        # A paragraph says it through its style (getKeepWithNext); a drawn line by attribute.
        keep = f.getKeepWithNext() if hasattr(f, "getKeepWithNext") else getattr(f, "keepWithNext", 0)
        if keep:
            pending.append(f)
            continue
        if pending:
            inner = list(f._content) if isinstance(f, KeepTogether) else [f]
            out.append(KeepTogether(pending + inner))
            pending = []
        else:
            out.append(f)
    return out + pending


class _Rule(HRFlowable):
    """The hairline under a section heading."""

    def __init__(self, width):
        super().__init__(width="100%", thickness=0.4, color=RULE,
                         spaceBefore=1, spaceAfter=max(1, HEAD_AFTER - 1), lineCap="butt")
        self.keepWithNext = 1           # the rule goes with its section's first line


def write_pdf(base: Resume, result: TailorResult, output_path: Path | str,
              header_lines: list[str] | None = None) -> Path:
    """Render the tailored resume to PDF. Same content as the .docx, in the same template."""
    from ml.resume.style import style_for
    with _STYLE_LOCK:
        _apply_style(style_for(result))
        return _write_pdf(base, result, output_path, header_lines)


def _write_pdf(base, result, output_path, header_lines):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    page_w, page_h = LETTER
    text_w = page_w - 2 * SIDE_MARGIN
    styles = _styles()

    # A right tab at the margin, so role dates land there rather than
    # wherever the source happened to space them.
    for name in ("role", "degree"):
        styles[name].tabs = [(text_w, "right")]

    keywords = list(getattr(result, "emphasis", None) or [])

    def emphasised(text: str) -> str:
        from ml.resume.emphasis import spans
        return "".join(f"<b>{_escape(p)}</b>" if bold else _escape(p) for p, bold in spans(text, keywords))

    story: list[Flowable] = []
    for kind, text in plan_document(base, result, header_lines):
        if kind == "name":
            story.append(Paragraph(_escape(text), styles["name"]))
        elif kind == "contact":
            items = [contact_link(i) for i in _tidy_contact(text).split(" \u00b7 ") if i]
            shown = {s: u for s, u in items}
            packed = _pack([s for s, _ in items], text_w, styles["contact"].fontSize)

            def markup(line: str) -> str:
                parts = []
                for piece in line.split(" \u00b7 "):
                    url = shown.get(piece)
                    parts.append(f'<a href="{_escape(url)}" color="#000000"><u>{_escape(piece)}</u></a>'
                                 if url else _escape(piece))
                return " \u00b7 ".join(parts)

            story.append(Paragraph("<br/>".join(markup(line) for line in packed),
                                   styles["contact"]))
        elif kind == "heading":
            story.append(Paragraph(_escape(text.upper()), styles["heading"]))
            story.append(_Rule(text_w))
        elif kind == "title":
            from ml.resume.writer import _tidy_contact as tidy
            story.append(Paragraph(_escape(tidy(text)), styles["title"]))
        elif kind == "role":
            story.append(_dated(text, text_w, FONT_BOLD, styles["role"]))
        elif kind == "degree":
            story.append(_dated(text, text_w, FONT, styles["degree"]))
        elif kind == "school":
            story.append(Paragraph(f"<b>{_escape(text)}</b>", styles["text"]))
        elif kind == "subheading":
            story.append(Paragraph(f"<b>{_escape(text)}</b>", styles["subheading"]))
        elif kind == "bullet":
            # The bullet as the paragraph's own mark, not text: justified,
            # the spaces after a text bullet were stretched with the words.
            # Kept whole: a bullet is not split across a page break.
            story.append(KeepTogether([Paragraph(emphasised(text), styles["bullet"], bulletText=BULLET)]))
        elif kind == "skill":
            label, _, items = text.partition(": ")
            # The bold face by name: <b> needs a registered font family, and
            # without one the label silently came out in the regular weight.
            story.append(Paragraph(f'<font name="{FONT_BOLD}">{_escape(label)}:</font> {emphasised(items)}',
                                   styles["skill"]))
        else:
            story.append(Paragraph(emphasised(text), styles["text"]))

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
    doc.build(_kept_with_next(story))

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
