"""ATS-safe .docx output that still reads like a designed document.

What breaks ATS parsers, and is therefore absent by construction: tables,
multiple columns, text boxes, headers/footers, images, and unusual fonts.
Everything here is ordinary paragraphs in a standard font.

Plain is not the same as careless, though, and the first version confused the
two. Measured on a real output: 145 paragraphs, every one of them styled
`Normal`, 43 lines carrying a "•" and 64 long body lines carrying nothing --
four bullets under one heading with a marker on exactly one of them. Bullets
were decided by whether the SOURCE line still had its marker, and PDF text
extraction drops that marker from some lines and not others. The indent had
no hanging component either, so every wrapped bullet line sat under the dot
instead of under the text.

So the layout rules here are deliberate:

  * bullets are decided per SECTION, not per line -- if a section bullets
    anything, every body line in it is a bullet, which is what a human means
    by a list;
  * a hanging indent, so wrapped text aligns under text;
  * real Heading styles, because a good number of parsers locate EXPERIENCE
    and EDUCATION by style rather than by reading the words;
  * a hairline rule under each section heading and a right-aligned tab stop
    for dates -- both are plain paragraph properties, invisible to a parser
    and the difference between "typed" and "typeset" to a reader.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Inches, Pt, RGBColor

from ml.resume.parser import Resume, strip_bullet
from ml.resume.tailor import TailorResult

log = logging.getLogger(__name__)

# The person's format (Oct 2026): Cambria throughout, black, single spaced.
# The PDF embeds the same face, so a reader without Office sees it too.
FONT = "Cambria"
BODY_SIZE = Pt(11)
HEADING_SIZE = Pt(12)
NAME_SIZE = Pt(18)
CONTACT_SIZE = Pt(11)

INK = RGBColor(0x00, 0x00, 0x00)
MUTED = INK                           # contact line and dates are black too
HEADING_COLOR = INK
RULE_COLOR = "BFBFBF"
GAP = 3                               # points after a body paragraph or bullet
HEAD_BEFORE, HEAD_AFTER = 8, 4
ROLE_BEFORE = 6
TITLE_AFTER = 6
LINE_SPACING = 1.0

# Content width, so the right-aligned date tab lands on the margin.
PAGE_WIDTH_IN = 8.5
SIDE_MARGIN_IN = 0.65
TEXT_WIDTH_IN = PAGE_WIDTH_IN - 2 * SIDE_MARGIN_IN

BULLET_AT = Inches(0.15)       # the "•", from the left text margin
BULLET_TEXT_AT = Inches(0.30)  # its text, and every wrapped line under it
RULE_ON = True                 # a hairline under each section heading
INDENT = Inches(0)             # the content moved right of a rail of headings (the sidebar)
HEADER_ALIGN = WD_ALIGN_PARAGRAPH.CENTER

# The values above are the classic template; a render sets them from the
# draft's template (ml/resume/style.py) under this lock, so two renders at
# once cannot mix one template's sizes with another's.
import threading
_STYLE_LOCK = threading.RLock()


def _apply_style(st) -> None:
    global FONT, BODY_SIZE, HEADING_SIZE, NAME_SIZE, CONTACT_SIZE, INK, MUTED, HEADING_COLOR
    global RULE_COLOR, GAP, HEAD_BEFORE, HEAD_AFTER, ROLE_BEFORE, TITLE_AFTER, SIDE_MARGIN_IN
    global TEXT_WIDTH_IN, BULLET_AT, BULLET_TEXT_AT, RULE_ON, INDENT, HEADER_ALIGN
    FONT = st.font
    RULE_ON, INDENT = st.rule, Inches(st.indent_in)
    HEADER_ALIGN = WD_ALIGN_PARAGRAPH.LEFT if st.header_align == "left" else WD_ALIGN_PARAGRAPH.CENTER
    BODY_SIZE, HEADING_SIZE, NAME_SIZE, CONTACT_SIZE = Pt(st.body), Pt(st.heading), Pt(st.name), Pt(st.contact)
    INK = MUTED = RGBColor.from_string(st.ink)
    HEADING_COLOR = RGBColor.from_string(st.heading_color)
    RULE_COLOR = st.rule_color
    GAP, HEAD_BEFORE, HEAD_AFTER = st.gap, st.heading_before, st.heading_after
    ROLE_BEFORE, TITLE_AFTER = st.role_before, st.title_after
    SIDE_MARGIN_IN = st.margin_in
    TEXT_WIDTH_IN = PAGE_WIDTH_IN - 2 * SIDE_MARGIN_IN
    BULLET_AT, BULLET_TEXT_AT = Inches(st.bullet_at_in), Inches(st.bullet_text_in)

# A line naming a job and its dates, rather than describing the work.
DATE_RE = re.compile(
    r"\b(19|20)\d{2}\b|\bpresent\b|\bcurrent\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(19|20)\d{2}",
    re.I)


def _slug(value: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value or "").strip("-").lower()
    return cleaned[:limit] or "role"


def output_filename(company: str, job_title: str, external_id: str = "") -> str:
    """Deterministic name so a resume file traces back to one posting."""
    stem = f"{_slug(company, 24)}_{_slug(job_title)}"
    if external_id:
        stem += f"_{_slug(str(external_id), 16)}"
    return f"{stem}.docx"


def _style_document(document: Document) -> None:
    normal = document.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = BODY_SIZE
    normal.font.color.rgb = INK
    fmt = normal.paragraph_format
    fmt.space_after = Pt(GAP)
    fmt.space_before = Pt(0)
    fmt.line_spacing = LINE_SPACING
    fmt.widow_control = True         # never strand one line of a bullet

    for section in document.sections:
        section.page_width = Inches(8.5)
        section.page_height = Inches(11)
        section.top_margin = Inches(SIDE_MARGIN_IN)
        section.bottom_margin = Inches(SIDE_MARGIN_IN)
        section.left_margin = Inches(SIDE_MARGIN_IN)
        section.right_margin = Inches(SIDE_MARGIN_IN)

    # Heading 1 is restyled rather than avoided: parsers that look for section
    # structure read the style, and a resume with no headings makes them guess.
    heading = document.styles["Heading 1"]
    heading.font.name = FONT
    heading.font.size = HEADING_SIZE
    heading.font.bold = True
    heading.font.color.rgb = HEADING_COLOR
    heading.font.all_caps = True
    hfmt = heading.paragraph_format
    hfmt.space_before = Pt(HEAD_BEFORE)
    hfmt.space_after = Pt(HEAD_AFTER)
    hfmt.keep_with_next = True       # a heading alone at a page foot is a bug


def _rule_under(para) -> None:
    """A hairline under a section heading.

    A paragraph border, not a drawn line or a table: it carries no content, so
    a parser never sees it, and it is what separates a typeset page from a
    typed one.
    """
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "4")          # quarter-point hairline
    bottom.set(qn("w:space"), "2")
    bottom.set(qn("w:color"), RULE_COLOR)
    borders.append(bottom)
    para._p.get_or_add_pPr().append(borders)


def _add(document: Document, text: str, *, bold=False, size=None, align=None,
         space_before=0, color=None, italic=False):
    para = document.add_paragraph()
    para.paragraph_format.space_before = Pt(space_before)
    if align is not None:
        para.alignment = align
    run = para.add_run(text)
    run.bold = bold
    run.italic = italic
    run.font.name = FONT
    run.font.size = size or BODY_SIZE
    if color is not None:
        run.font.color.rgb = color
    return para


def _add_heading(document: Document, text: str) -> None:
    para = document.add_heading(text.upper(), level=1)
    # Also set on the paragraph, not only on the Heading 1 style: a style is
    # editable in Word, and a heading stranded at the foot of a page is the
    # one layout fault a reader always notices.
    para.paragraph_format.keep_with_next = True
    for run in para.runs:
        run.font.name = FONT
        run.font.size = HEADING_SIZE
        run.font.color.rgb = HEADING_COLOR
    if RULE_ON:
        _rule_under(para)


def _add_role(document: Document, text: str, *, bold_title: bool = True) -> None:
    """A job title line in bold, its dates on a right tab stop at the margin.

    The person's format: title and company left, dates right, all bold. A
    table would break parsers and spaces move with the font, so a tab -- and
    two spaces before it, so text extraction keeps the two apart.
    """
    title, dates = _split_dates(text)
    para = document.add_paragraph()
    fmt = para.paragraph_format
    fmt.space_before = Pt(ROLE_BEFORE)
    fmt.space_after = Pt(GAP)
    fmt.keep_with_next = True        # never split a job from its first bullet
    fmt.left_indent = INDENT
    # Tab stops are measured from the page margin, not the indent: the dates still reach the margin.
    fmt.tab_stops.add_tab_stop(Inches(TEXT_WIDTH_IN), WD_TAB_ALIGNMENT.RIGHT)

    run = para.add_run(title + ("  \t" if dates else ""))
    run.bold = bold_title
    run.font.name = FONT
    run.font.size = BODY_SIZE
    if dates:
        tail = para.add_run(dates)
        tail.bold = bold_title
        tail.font.name = FONT
        tail.font.size = BODY_SIZE
    return para


def _add_school(document: Document, text: str) -> None:
    """The university line of an education entry, in bold."""
    para = _add(document, text, bold=True)
    para.paragraph_format.space_after = Pt(GAP)
    para.paragraph_format.left_indent = INDENT


def _add_title(document: Document, text: str) -> None:
    """The professional title and primary technologies under the contact line."""
    para = _add(document, _tidy_contact(text), bold=True, size=BODY_SIZE,
                align=HEADER_ALIGN)
    para.paragraph_format.space_before = Pt(0)
    para.paragraph_format.space_after = Pt(TITLE_AFTER)


def _add_contact(document: Document, text: str) -> None:
    """The contact line, with the LinkedIn address as a linked word."""
    para = document.add_paragraph()
    para.alignment = HEADER_ALIGN
    para.paragraph_format.space_after = Pt(GAP)
    items = [i for i in _tidy_contact(text).split(" \u00b7 ") if i]
    for n, item in enumerate(items):
        if n:
            _contact_run(para, " \u00b7 ")
        shown, url = contact_link(item)
        if url:
            _hyperlink(para, shown, url)
        else:
            _contact_run(para, shown)


def _contact_run(para, value: str):
    run = para.add_run(value)
    run.font.name = FONT
    run.font.size = CONTACT_SIZE
    run.font.color.rgb = MUTED
    return run


def _hyperlink(para, shown: str, url: str) -> None:
    """A clickable run. python-docx has no API for it; this is the XML Word writes."""
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    rel = para.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    for tag, attr, value in (("w:rFonts", "w:ascii", FONT), ("w:color", "w:val", str(INK)),
                             ("w:u", "w:val", "single"), ("w:sz", "w:val", str(int(CONTACT_SIZE.pt * 2)))):
        el = OxmlElement(tag)
        el.set(qn(attr), value)
        if tag == "w:rFonts":
            el.set(qn("w:hAnsi"), value)
        props.append(el)
    run.append(props)
    t = OxmlElement("w:t")
    t.text = shown
    run.append(t)
    link.append(run)
    para._p.append(link)


def _add_skill(document: Document, text: str, keywords: list[str] | None = None) -> None:
    """"Label: items" with the label in bold and the items in the regular weight.

    The posting's tools set bold among the items made every line of the section a patchwork of
    half-bold names; the labels already lead the eye (Oct 2026). `keywords` is accepted and unused.
    """
    label, _, items = text.partition(": ")
    para = document.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.LEFT
    para.paragraph_format.space_after = Pt(GAP)
    para.paragraph_format.left_indent = INDENT
    run = para.add_run(label + ": ")
    run.bold = True
    run.font.name = FONT
    run.font.size = BODY_SIZE
    _emphasised(para, "", items, None)


def _add_subheading(document: Document, text: str) -> None:
    """A grouping inside one job ("Cloud Infrastructure & Scalability")."""
    para = _add(document, text, bold=True, space_before=2)
    para.paragraph_format.space_after = Pt(GAP)
    para.paragraph_format.keep_with_next = True
    para.paragraph_format.left_indent = INDENT
    return para


def _bullet_width_pt() -> float:
    """Width of "•" and an en space in the body font, in points."""
    try:
        from reportlab.pdfbase.pdfmetrics import stringWidth

        from ml.resume.pdf import FONT as PDF_FONT
        return stringWidth("\u2022", PDF_FONT, BODY_SIZE.pt) + 0.5 * BODY_SIZE.pt
    except Exception:
        return 0.85 * BODY_SIZE.pt           # Arial: bullet 0.35em + en space 0.5em


def _add_bullet(document: Document, text: str, keywords: list[str] | None = None,
                used: set[str] | None = None) -> None:
    """A bullet at 0.15", its text at 0.30", wrapped lines under the first word.

    A literal "•" and a tab rather than a Word list style: list numbering
    lives in separate numbering XML, which weaker parsers drop. The hanging
    indent puts every wrapped line at the tab, under the bullet's text.
    """
    para = document.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    fmt = para.paragraph_format
    fmt.left_indent = BULLET_TEXT_AT + INDENT
    fmt.first_line_indent = BULLET_AT - BULLET_TEXT_AT
    fmt.tab_stops.add_tab_stop(BULLET_TEXT_AT + INDENT)
    fmt.space_after = Pt(GAP)
    fmt.keep_together = True             # a bullet is not split across pages
    _emphasised(para, "\u2022\t", strip_bullet(text), keywords, used)


def _emphasised(para, lead: str, text: str, keywords: list[str] | None, used: set[str] | None = None) -> None:
    """The text as runs, the posting's keywords in bold (ml/resume/emphasis.py), each once per job."""
    from ml.resume.emphasis import spans
    for i, (piece, bold) in enumerate(spans(text, keywords or [], used=used)):
        run = para.add_run((lead if i == 0 else "") + piece)
        run.bold = bold
        run.font.name = FONT
        run.font.size = BODY_SIZE


def _split_dates(line: str) -> tuple[str, str]:
    """Separate "SENIOR ENGINEER, ACME" from "Feb 2025 - Present".

    The source keeps them on one line, joined by a tab or a run of spaces.
    Splitting lets the dates be tabbed to the right margin instead of sitting
    wherever the original spacing happened to put them.
    """
    parts = re.split(r"\t+|\s{3,}", line.strip())
    if len(parts) >= 2 and DATE_RE.search(parts[-1]):
        return " ".join(p.strip() for p in parts[:-1]), parts[-1].strip()
    # Or joined by a bar, which is the other common spelling.
    if "|" in line:
        head, _, tail = line.rpartition("|")
        if DATE_RE.search(tail) and head.strip():
            return head.strip(), tail.strip()
    return line.strip(), ""


def _is_role_line(line: str) -> bool:
    """Does this name a job, rather than describe one?

    A role line carries dates, or is short and shouty -- "SENIOR DEVOPS
    ENGINEER, DOUBLENE, USA". A bullet is a sentence about the work.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if _split_dates(stripped)[1]:
        return True
    letters = [c for c in stripped if c.isalpha()]
    mostly_caps = letters and sum(c.isupper() for c in letters) / len(letters) > 0.7
    return bool(mostly_caps and len(stripped) < 90)


def _is_subheading(line: str) -> bool:
    """A grouping label inside a job: short, titled, and not a sentence."""
    stripped = line.strip().rstrip(":")
    if not stripped or len(stripped) > 60:
        return False
    if stripped.endswith((".", ";", ",")):
        return False
    words = stripped.split()
    if len(words) > 7:
        return False
    # "Cloud Infrastructure & Scalability" -- most words capitalised.
    caps = sum(1 for w in words if w[:1].isupper())
    return len(words) >= 2 and caps >= max(2, len(words) - 1)


def _section_is_a_list(lines: list[str], rewrites: dict) -> bool:
    """Should this section's body lines be bullets?

    Decided once for the whole section, because deciding per line is what
    produced four bullets under one heading with a marker on exactly one of
    them. PDF extraction keeps the "•" on some lines and drops it from
    others; a section either is a list or it is not.
    """
    body = [l for l in lines
            if l.strip() and not _is_role_line(l) and not _is_subheading(l)]
    if not body:
        return False
    marked = sum(1 for l in body if l.strip().startswith(("-", "\u2022", "*", "\u25aa", "\u00b7")))
    rewritten = sum(1 for l in body
                    if l.strip() in rewrites or strip_bullet(l.strip()) in rewrites)
    # Any real evidence of a list, or simply several sentence-length lines,
    # which is what an experience section is even when the markers are gone.
    if marked or rewritten:
        return True
    return sum(1 for l in body if len(l.strip()) > 70) >= 2


# At most this share of one section's body lines may be dropped. A model asked
# to tailor will sometimes cut most of a job; a role stripped to one line reads
# as a gap in the career, which is worse than a slightly long resume.
MAX_DROPPED_SHARE = 0.5


def _dropped_lines(result: TailorResult) -> set[str]:
    """The base-resume lines the model asked to leave out, verbatim.

    Only entries carrying the original text can drop anything. A prose
    description of what was dropped -- which is what this field used to be --
    names no line and removes nothing.
    """
    lines = set()
    for entry in result.omitted or []:
        if isinstance(entry, dict):
            original = (entry.get("original") or "").strip()
            if original:
                lines.add(original)
                lines.add(strip_bullet(original))
    return lines


def _keepable(lines: list[str], dropped: set[str], fitted: bool = False) -> set[str]:
    """Which of this section's lines to actually remove.

    Returns the lines to SKIP, having refused the removals that would damage
    the record: a job title, a date line or a degree is the person's history,
    not padding, and no section gives up more than half its body.
    """
    if not dropped:
        return set()

    body = [l.strip() for l in lines
            if l.strip() and not _is_role_line(l) and not _is_subheading(l)]
    asked = [l for l in body
             if l in dropped or strip_bullet(l) in dropped]
    if not asked:
        return set()

    allowed = len(body) if fitted else int(len(body) * MAX_DROPPED_SHARE)
    if len(asked) > allowed:
        # Keep the ones it asked for first; the model puts its weakest
        # material at the front of the list.
        asked = asked[:allowed]
    return set(asked)


def plan_document(base: Resume, result: TailorResult,
                  header_lines: list[str] | None = None) -> list[tuple[str, str]]:
    """The resume as a list of (kind, text) blocks, before any format exists.

    Both renderers read this, so the .docx and the .pdf cannot disagree about
    what is a bullet, a job title or a section. Deciding twice is how two
    exports of "the same" resume end up different documents.

    kinds: name · contact · heading · role · subheading · bullet · text
    """
    rewrites = {
        (b.get("original") or "").strip(): (b.get("tailored") or "").strip()
        for b in result.bullets
        if b.get("original") and b.get("tailored")
    }

    dropped = _dropped_lines(result)

    blocks: list[tuple[str, str]] = []
    trimmed: set[int] = set()
    header = header_lines if header_lines is not None else _base_header(base)
    for i, line in enumerate(header):
        if (line or "").strip():
            # kinds added here: title -- the professional title line, bold.
            kind = "name" if i == 0 else "contact" if _is_contact_line(line) or "linkedin" in line.lower() else "title"
            blocks.append((kind, line.strip()))

    if result.summary:
        blocks.append(("heading", "Summary"))
        blocks.append(("text", result.summary))

    for section in base.sections:
        if section.heading == "HEADER":
            continue
        if result.summary and section.heading.lower() in ("summary", "objective", "profile"):
            continue
        blocks.append(("heading", section.heading))
        as_list = _section_is_a_list(section.lines, rewrites)
        keep = _keepable(section.lines, dropped, getattr(result, "fitted", False))
        present = [l.strip() for l in section.lines if l.strip()]
        for index, stripped in enumerate(present):
            body = strip_bullet(stripped)
            replacement = rewrites.get(stripped) or rewrites.get(body)
            following = present[index + 1] if index + 1 < len(present) else None
            if stripped in keep:
                # Remember which title just lost a line: only such a title may
                # later be removed for having nothing under it.
                last_title = max((i for i, b in enumerate(blocks)
                                  if b[0] in _OUTRANKS), default=None)
                if last_title is not None:
                    trimmed.add(last_title)
                continue
            if _is_role_line(stripped):
                blocks.append(("role", stripped))
            elif not replacement and (_is_subheading(stripped)
                                      or _is_group_title(stripped, following)):
                blocks.append(("subheading", stripped))
            elif as_list or replacement:
                blocks.append(("bullet", replacement or body))
            else:
                blocks.append(("text", body))
    from ml.resume.typeset import plain_words, state_codes, typeset
    # Not a role line: its title and dates are told apart by the run of
    # spaces between them, which the tidy would close up.
    tidy = [(kind, text if kind in ("name", "role", "contact")
             else plain_words(state_codes(typeset(text))))
            for kind, text in _without_empty_titles(blocks, trimmed)]
    arranged = _arrange(tidy)
    overrides = getattr(result, "overrides", None) or {}
    if overrides:
        # The person's own edits from the preview, applied last and as written.
        arranged = [(k, overrides.get(t, t)) for k, t in arranged]
        arranged = [(k, t) for k, t in arranged if (t or "").strip()]
    from ml.resume.style import style_for
    if style_for(result).dates == "numeric":
        arranged = [(k, numeric_dates(t) if k in ("role", "degree") else t) for k, t in arranged]
    return arranged


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"), 1)}
_MONTH_YEAR = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+((?:19|20)\d{2})\b", re.I)


def numeric_dates(line: str) -> str:
    """A role or degree line's dates as MM/YYYY ("Feb 2025 – Present" -> "02/2025 – Present"), the
    federal form: federal HR counts months of experience from them. The title is left as it is."""
    title, dates = _split_dates(line)
    if not dates:
        return line
    at = line.rfind(dates)
    return line[:at] + _MONTH_YEAR.sub(lambda m: f"{_MONTHS[m.group(1).lower()[:3]]:02d}/{m.group(2)}", dates)


# Section order for an experienced engineer: what they do, what they know, where
# they did it, and the degree last. A master resume kept in any order -- this
# one had Education second -- comes out in this one.
_ORDER = (("summary", "objective", "profile"), ("highlight", "impact", "achievement"),
          ("expertise", "practice"), ("skill", "competenc", "technolog"),
          ("experience", "employment", "work history"), ("project",),
          ("certif", "license"), ("education",))
_SKILLS = ("skill", "competenc", "technolog")


_SIDE = ("highlight", "impact", "achievement", "expertise", "practice", "project")


def _rank(heading: str, first: bool = False) -> int:
    low = heading.lower()
    # The more specific kinds first: "Core Competencies / Technical Skills
    # Summary" is a skills section that happens to end in "Summary".
    for i in sorted(range(len(_ORDER)), key=lambda i: i == 0):
        if any(w in low for w in _ORDER[i]):
            return i
    # An unknown first section is usually the name read as a heading ("JANE
    # DOE" over the contact line): it stays at the top. Any other unknown one
    # sits just above Education.
    return -1 if first else len(_ORDER) - 1


def _arrange(blocks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Order the sections, compact the skills, and drop a section left with one line.

    kinds added here: skill -- "Label: items", drawn with the label in bold.
    """
    head = [b for b in blocks if b[0] in ("name", "contact", "title")]
    sections: list[list[tuple[str, str]]] = []
    for kind, text in blocks:
        if kind in ("name", "contact", "title"):
            continue
        if kind == "heading" or not sections:
            sections.append([])
        sections[-1].append((kind, text))

    # A master with two Skills sections (an import once split one in two) prints one: the second's
    # lines join the first, and the section is cleaned as a whole.
    first_skills, kept = None, []
    for section in sections:
        heading = section[0][1] if section[0][0] == "heading" else ""
        if heading and any(w in heading.lower() for w in _SKILLS):
            if first_skills is not None:
                first_skills.extend(section[1:])
                continue
            first_skills = section
        kept.append(section)
    sections = kept

    out_sections = []
    for number, section in enumerate(sections):
        heading = section[0][1] if section[0][0] == "heading" else ""
        body = section[1:] if heading else section
        low = heading.lower()
        if any(w in low for w in _SKILLS):
            body = _skills_section(body)
        if any(w in low for w in ("education", "certif", "license", "project", "award")):
            # A school, certificate or project the parser took for a label
            # printed bold under one entry and plain under the next. Plain.
            body = [("text", t) if k == "subheading" else (k, t) for k, t in body]
        if "education" in low:
            # kinds added here: degree -- "Master of Science <tab> 2023 - 2024",
            # regular with the years right; school -- the university, bold.
            body = [("degree", t) if k == "role" else ("school", t) if k == "text" else (k, t)
                    for k, t in body]
        if any(w in low for w in ("expertise", "practice", "highlight")):
            # "Infrastructure as Code (IaC): Built ..." reads as its skills
            # neighbour does: the label in bold, the work after it.
            body = [("skill", t) if k in ("bullet", "text") and _labelled(t) else (k, t)
                    for k, t in body]
        if _rank(heading) == 0:
            # A summary is prose. Kept as three lines in the master, it came out
            # as three bullets; joined into one it was a wall. Paragraphs.
            body = [("text", t) if k == "bullet" else (k, t) for k, t in body]
        lines = [b for b in body if b[0] in ("bullet", "text", "skill")]
        if any(w in low for w in _SIDE) and len(lines) < 2:
            continue                # "Highlights of Impact" with one bullet reads as a slip
        out_sections.append((_rank(heading, first=number == 0),
                             [section[0]] + body if heading else body))
    out_sections.sort(key=lambda s: s[0])          # stable: master order within a rank
    out_sections = _merge_repeats(out_sections)
    return _roles_alike(head + [b for _, section in out_sections for b in section])


def _roles_alike(blocks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Every job's title line in the same case: in capitals when the titles are written mostly in them.

    "Programmer Analyst / SENIOR DEVOPS ENGINEER" and "CLOUD Architect/ ENGINEER" sat among titles in
    capitals and read as mistakes. Only the case changes, and the spacing around a title's slash --
    never the words, and never the dates.
    """
    roles = [(i, _split_dates(t)) for i, (k, t) in enumerate(blocks) if k == "role"]
    letters = [c for _, (left, _) in roles for c in left if c.isalpha()]
    if len(roles) < 2 or not letters or sum(c.isupper() for c in letters) < 0.6 * len(letters):
        return blocks
    out = list(blocks)
    for i, (left, _) in roles:
        even = re.sub(r"(\w)\s*/\s+(\w)|(\w)\s+/\s*(\w)", lambda m: f"{m.group(1) or m.group(3)} / "
                      f"{m.group(2) or m.group(4)}", left).upper()
        if even != left:
            out[i] = ("role", blocks[i][1].replace(left, even, 1))
    return out


_RANK_SUMMARY, _RANK_HIGHLIGHT, _RANK_EXPERTISE, _RANK_SKILLS = 0, 1, 2, 3
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _label_words(label: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", label.lower())) - {"and", "as", "of", "the"}


def _same_label(a: str, b: str) -> bool:
    """"Security & Compliance (DevSecOps)" is "Security / DevSecOps / Compliance"."""
    wa, wb = _label_words(a), _label_words(b)
    if not wa or not wb:
        return False
    return wa <= wb or wb <= wa or len(wa & wb) / len(wa | wb) >= 0.6


def _merge_repeats(sections: list[tuple[int, list]]) -> list[tuple[int, list]]:
    """One skills section and one summary on a tailored copy, not three that repeat.

    The master keeps Key Expertise, Highlights of Impact and Core Competencies
    apart, and a recruiter reading the tailored copy met "CI/CD & GitOps
    Automation" twice before the first job. Expertise rows join the skills
    section, a row whose label is already there joined to that row; Highlights
    lines whose every number the summary already gives are dropped as repeats
    and the rest close the summary. Nothing is reworded.
    """
    def find(rank):
        return next((i for i, (r, s) in enumerate(sections)
                     if r == rank and s and s[0][0] == "heading"), None)

    exp, skills = find(_RANK_EXPERTISE), find(_RANK_SKILLS)
    if exp is not None and skills is not None:
        rows = list(sections[skills][1][1:])
        merged = []
        for kind, text in sections[exp][1][1:]:
            label, sep, rest = text.partition(": ")
            twin = next((j for j, (k, t) in enumerate(rows) if sep and k == "skill"
                         and _same_label(label, t.partition(": ")[0])), None)
            if twin is not None:
                theirs = rows.pop(twin)[1].partition(": ")[2]
                text = f"{label}: {rest.rstrip('.')}. {theirs.rstrip('.')}."   # a sentence, then its tools
                kind = "skill"
            merged.append((kind, text))
        sections[skills] = (_RANK_EXPERTISE, [sections[skills][1][0]] + merged + rows)
        sections.pop(exp)

    summary, high = find(_RANK_SUMMARY), find(_RANK_HIGHLIGHT)
    if summary is not None and high is not None:
        said = " ".join(t for _, t in sections[summary][1][1:])
        shown = set(_NUMBER.findall(said))
        extra = [("text", t) for k, t in sections[high][1][1:]
                 if k in ("bullet", "text", "skill")
                 and not ((nums := _NUMBER.findall(t)) and set(nums) <= shown)
                 and t not in said]
        sections[summary] = (_RANK_SUMMARY, sections[summary][1] + extra)
        sections.pop(high)
    return sections


def _labelled(text: str) -> bool:
    label, sep, rest = text.partition(": ")
    return bool(sep) and 1 <= len(label.split()) <= 6 and len(rest.split()) >= 4


def _skills_section(body: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Each skills group as one line, "Label: tool, tool, tool" (ml/resume/skills.py).

    Trimmed to a posting, a group often keeps one line, and a bold label over a single bullet
    looks like something fell off the page. A group's label over its own bullets is one group;
    each tool is shown once, without the description written after it.
    """
    from ml.resume import skills
    lines, other, label = [], [], ""
    for kind, text in body:
        text = strip_bullet(text).strip()
        if kind == "subheading":
            label = text.rstrip(":")
            lines.append(label + ":")
        elif kind in ("bullet", "text", "skill"):
            # "Operating Systems: Windows, Linux" under "IDEs / Servers / Operating Systems" would
            # print the label twice; drop the item's own.
            own, sep, rest = text.partition(": ")
            if kind != "skill" and sep and label and own.lower() in label.lower() and len(own.split()) <= 5:
                text = rest
            lines.append(text)
        else:
            other.append((kind, text))
    out = [("skill", line) if ": " in line and len(line.partition(": ")[0].split()) <= 7 else ("text", line)
           for line in skills.compact(lines)]
    return out + other


_TITLE_METRIC = re.compile(r"\d+\s?%|\d+\+")


def _is_group_title(line: str, following: str | None) -> bool:
    """A short label over longer lines, which `_is_subheading` misses.

    "Infrastructure as Code (IaC) & Cloud Provisioning" and "Monitoring /
    Observability / Reliability Engineering" fail the capitalised-words test
    on their brackets and slashes, and printed as bullets with a sentence's
    worth of nothing in them. What gives them away is what follows: a line of
    real work, much longer than they are.
    """
    body = strip_bullet(line).rstrip(":")
    if (not following or _is_role_line(following) or line.endswith(".")
            or len(body.split()) > 8 or _TITLE_METRIC.search(body)):
        return False
    return len(strip_bullet(following).split()) >= len(body.split()) + 6


# What each kind of title needs beneath it before the next title of its rank.
_OUTRANKS = {"subheading": ("subheading", "role", "heading"), "heading": ("heading",)}


def _without_empty_titles(blocks: list[tuple[str, str]],
                          trimmed: set[int]) -> list[tuple[str, str]]:
    """Drop a heading or subheading that trimming left with nothing under it.

    Trimming a master resume to a posting can empty a whole group ("Storage &
    Data Protection") or section; its title alone would read as a mistake.
    Only titles that actually LOST lines are candidates: a one-line section
    whose content the parser reads as a subheading ("Python, Django, SQL")
    was never empty and must stay. Role lines are never dropped — a job stays
    on the record however little of it this posting needs.
    """
    kept = [(kind, text, i in trimmed) for i, (kind, text) in enumerate(blocks)]
    changed = True
    while changed:
        changed = False
        for i, (kind, _, lost) in enumerate(kept):
            if kind not in _OUTRANKS or not lost:
                continue
            nxt = kept[i + 1][0] if i + 1 < len(kept) else None
            if nxt is None or nxt in _OUTRANKS[kind]:
                # Its parent heading lost a child too.
                parent = max((j for j in range(i) if kept[j][0] == "heading"), default=None)
                del kept[i]
                if kind == "subheading" and parent is not None:
                    kept[parent] = (kept[parent][0], kept[parent][1], True)
                changed = True
                break
    return [(kind, text) for kind, text, _ in kept]


def write_tailored_resume(
    base: Resume,
    result: TailorResult,
    output_path: Path | str,
    header_lines: list[str] | None = None,
) -> Path:
    """Write the tailored resume, preserving the base document's structure.

    Bullets Claude rewrote are substituted in place; everything else — contact
    details, employers, dates, education — is copied through from the base
    resume untouched, because those are exactly the fields nothing should edit.
    """
    from ml.resume.style import style_for
    with _STYLE_LOCK:
        _apply_style(style_for(result))
        return _write_tailored(base, result, output_path, header_lines)


def _write_tailored(base, result, output_path, header_lines):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    document = Document()
    _style_document(document)
    keywords = list(getattr(result, "emphasis", None) or [])
    used: set[str] = set()               # bold once per job (and once in the summary), not every bullet

    for kind, text in plan_document(base, result, header_lines):
        if kind in ("heading", "role"):
            used = set()
        if kind == "name":
            name = _add(document, text, bold=True, size=NAME_SIZE,
                        align=HEADER_ALIGN)
            name.paragraph_format.space_after = Pt(GAP)
        elif kind == "contact":
            _add_contact(document, text)
        elif kind == "title":
            _add_title(document, text)
        elif kind == "degree":
            _add_role(document, text, bold_title=False)
        elif kind == "school":
            _add_school(document, text)
        elif kind == "heading":
            _add_heading(document, text)
        elif kind == "role":
            _add_role(document, text)
        elif kind == "subheading":
            _add_subheading(document, text)
        elif kind == "bullet":
            _add_bullet(document, text, keywords, used)
        elif kind == "skill":
            _add_skill(document, text, keywords)
        else:
            para = document.add_paragraph()
            para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            para.paragraph_format.space_after = Pt(GAP)
            para.paragraph_format.left_indent = INDENT
            _emphasised(para, "", text, keywords, used)

    document.save(output_path)
    log.info("Wrote ATS-safe resume: %s", output_path)
    return output_path


def _tidy_contact(line: str) -> str:
    """One separator for the contact line, whatever the source used.

    Resumes arrive with "|", " - " and " * " mixed on the same line because
    they were typed over years. A middot reads as deliberate and survives
    plain-text extraction, which a vertical bar in some fonts does not.
    """
    cleaned = re.sub(r"\s*[|\u2022]\s*", " \u00b7 ", (line or "").strip())
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" \u00b7")


def _base_header(base: Resume) -> list[str]:
    section = next((s for s in base.sections if s.heading == "HEADER"), None)
    lines = section.lines[:5] if section else []
    contact = applicant_contact_line()
    if not contact:
        return lines
    for i, line in enumerate(lines[1:], start=1):
        if _is_contact_line(line):
            relocate = "relocat" in line.lower()
            lines = list(lines)
            lines[i] = contact(relocate)
            break
    return lines


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


def _is_contact_line(line: str) -> bool:
    return bool(_EMAIL.search(line or "") or _PHONE.search(line or ""))


def contact_link(item: str) -> tuple[str, str | None]:
    """(what the contact line shows, where it links) for one item.

    A LinkedIn address shows as the word "LinkedIn", linked to the profile:
    a long URL in the middle of the contact line is the part that wrapped
    and looked untidy, and a recruiter clicks it rather than reads it.
    """
    text = (item or "").strip()
    if re.match(r"^(https?://)?(www\.)?linkedin\.com/", text, re.I):
        url = text if text.lower().startswith("http") else "https://" + text
        return "LinkedIn", url
    return text, None


def applicant_contact_line():
    """The contact line from the applicant profile, or None to keep the resume's.

    The form and the resume go to the same recruiter. When they give two
    different phone numbers, the one on the resume is the one that gets
    called — so the details the person keeps current on the Applicant page
    are the ones every tailored resume carries. Only contact details are
    taken from the profile; nothing about experience ever is.

    Returns a function of `open_to_relocate`, kept from the base resume's own
    line, since that is a statement about the person the profile may not make.
    """
    try:
        from backend.apply.profile import load
        from backend.config.loader import PROJECT_ROOT

        person = load(PROJECT_ROOT / "backend" / "config" / "applicant.yaml")
    except Exception:
        return None
    if not (person.email or person.phone):
        return None

    city = str(person.location.get("city") or "").strip()
    state = str(person.location.get("state") or "").strip()
    place = ", ".join(p for p in (city, state) if p)
    linkedin = str(person.links.get("linkedin") or "").strip()
    linkedin = re.sub(r"^https?://(www\.)?", "", linkedin).split("?")[0].rstrip("/")

    def line(open_to_relocate: bool) -> str:
        where = place + (" (Open to Relocate)" if place and open_to_relocate else "")
        return " · ".join(p for p in (where, person.phone, person.email, linkedin) if p)

    return line


def write_review_note(result: TailorResult, path: Path | str, job_desc: str = "") -> Path:
    """Plain-text companion explaining what changed and what's missing."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"Tailoring review — generated {datetime.now():%Y-%m-%d %H:%M}",
        "=" * 60,
        "",
        "GAPS (what this JD wants that your resume does not evidence):",
    ]
    lines.extend(f"  - {g}" for g in result.gaps or ["(none reported)"])
    from ml.resume.typeset import tells_in
    tells = sorted({t for b in result.bullets for t in tells_in(b.get("tailored") or "")}
                   | set(tells_in(result.summary or "")))
    if tells:
        lines += ["", "READS MACHINE-WRITTEN (swap for a plain word before sending):",
                  f"  {', '.join(tells)}"]
    lines += ["", "REWRITTEN BULLETS:"]
    for bullet in result.bullets:
        lines += [
            f"  FROM: {bullet.get('original', '')}",
            f"  TO:   {bullet.get('tailored', '')}",
            f"  WHY:  {bullet.get('reason', '')}",
            "",
        ]
    if result.omitted:
        lines += ["OMITTED (removed from the document):"]
        for entry in result.omitted:
            if isinstance(entry, dict):
                text = entry.get("original") or entry.get("reason") or ""
                why = entry.get("reason") if entry.get("original") else ""
                lines.append(f"  - {text}" + (f"\n      why: {why}" if why else ""))
            else:
                lines.append(f"  - {entry}")
        lines.append("")
    if getattr(result, "retried", False) and result.first_violations:
        # A near miss is worth seeing. The first answer claimed these, the
        # guard caught them, and the second answer was asked to drop them --
        # if any keep reappearing across postings, the prompt needs the fix,
        # not the retry.
        lines += [
            "RETRIED — the first answer claimed these and the resume does not:",
            *(f"  - {v}" for v in result.first_violations),
            "",
        ]
    if result.guard and not result.guard.ok:
        lines += ["FABRICATION GUARD — REJECTED:", result.guard.report(), ""]

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
