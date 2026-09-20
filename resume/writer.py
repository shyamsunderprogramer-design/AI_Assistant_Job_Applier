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

from resume.parser import Resume, strip_bullet
from resume.tailor import TailorResult

log = logging.getLogger(__name__)

FONT = "Calibri"          # widely parsed; no exotic glyphs
BODY_SIZE = Pt(10.5)
HEADING_SIZE = Pt(10.5)
NAME_SIZE = Pt(19)
CONTACT_SIZE = Pt(9.5)

INK = RGBColor(0x1A, 0x1A, 0x1A)      # near-black reads softer than pure black
MUTED = RGBColor(0x55, 0x55, 0x55)    # contact line, dates

# Content width, so the right-aligned date tab lands on the margin.
PAGE_WIDTH_IN = 8.5
SIDE_MARGIN_IN = 0.65
TEXT_WIDTH_IN = PAGE_WIDTH_IN - 2 * SIDE_MARGIN_IN

BULLET_INDENT = Pt(11)    # how far the "•" sits in
BULLET_HANG = Pt(11)      # and how far the text hangs past it

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
    fmt.space_after = Pt(2)
    fmt.space_before = Pt(0)
    fmt.line_spacing = 1.06          # room to breathe without costing a page
    fmt.widow_control = True         # never strand one line of a bullet

    for section in document.sections:
        section.top_margin = Inches(0.5)
        section.bottom_margin = Inches(0.5)
        section.left_margin = Inches(SIDE_MARGIN_IN)
        section.right_margin = Inches(SIDE_MARGIN_IN)

    # Heading 1 is restyled rather than avoided: parsers that look for section
    # structure read the style, and a resume with no headings makes them guess.
    heading = document.styles["Heading 1"]
    heading.font.name = FONT
    heading.font.size = HEADING_SIZE
    heading.font.bold = True
    heading.font.color.rgb = INK
    heading.font.all_caps = True
    hfmt = heading.paragraph_format
    hfmt.space_before = Pt(11)
    hfmt.space_after = Pt(3)
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
    bottom.set(qn("w:color"), "BFBFBF")
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
        run.font.color.rgb = INK
    _rule_under(para)


def _add_role(document: Document, text: str) -> None:
    """A job title line, with its dates pushed to the right margin.

    Resumes put dates on the right. Doing it with spaces breaks the moment the
    font changes and doing it with a table breaks the parser, so it is a right
    tab stop -- which is what the tab character is for.
    """
    title, dates = _split_dates(text)
    para = document.add_paragraph()
    fmt = para.paragraph_format
    fmt.space_before = Pt(7)
    fmt.space_after = Pt(1)
    fmt.keep_with_next = True        # never split a job from its first bullet
    fmt.tab_stops.add_tab_stop(Inches(TEXT_WIDTH_IN), WD_TAB_ALIGNMENT.RIGHT)

    run = para.add_run(title)
    run.bold = True
    run.font.name = FONT
    run.font.size = BODY_SIZE
    if dates:
        tail = para.add_run("\t" + dates)
        tail.font.name = FONT
        tail.font.size = BODY_SIZE
        tail.font.color.rgb = MUTED
    return para


def _add_subheading(document: Document, text: str) -> None:
    """A grouping inside one job ("Cloud Infrastructure & Scalability")."""
    para = _add(document, text, bold=True, space_before=5)
    para.paragraph_format.space_after = Pt(1)
    para.paragraph_format.keep_with_next = True
    return para


def _add_bullet(document: Document, text: str) -> None:
    """A bullet with a hanging indent, so wrapped text aligns under text.

    A literal "• " rather than a Word list style: list numbering lives in
    separate numbering XML, which is one of the things weaker parsers drop --
    and a dropped bullet turns a list into a wall of prose.
    """
    para = document.add_paragraph()
    fmt = para.paragraph_format
    fmt.left_indent = BULLET_INDENT + BULLET_HANG
    fmt.first_line_indent = -BULLET_HANG     # the hanging part
    fmt.space_after = Pt(2.5)
    run = para.add_run(f"\u2022\t{strip_bullet(text)}")
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
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    document = Document()
    _style_document(document)

    rewrites = {
        (b.get("original") or "").strip(): (b.get("tailored") or "").strip()
        for b in result.bullets
        if b.get("original") and b.get("tailored")
    }

    # Header: contact block, verbatim from the base resume. Only the type
    # changes -- the name leads, the rest is quieter and smaller, and the
    # separators are normalised so one line does not use "|" and the next " - ".
    header = header_lines if header_lines is not None else _base_header(base)
    for i, line in enumerate(header):
        if not (line or "").strip():
            continue
        if i == 0:
            name = _add(document, line.strip(), bold=True, size=NAME_SIZE,
                        align=WD_ALIGN_PARAGRAPH.CENTER)
            name.paragraph_format.space_after = Pt(1)
        else:
            contact = _add(document, _tidy_contact(line), size=CONTACT_SIZE,
                           align=WD_ALIGN_PARAGRAPH.CENTER, color=MUTED)
            contact.paragraph_format.space_after = Pt(1)

    if result.summary:
        _add_heading(document, "Summary")
        _add(document, result.summary)

    for section in base.sections:
        if section.heading == "HEADER":
            continue
        if result.summary and section.heading.lower() in ("summary", "objective", "profile"):
            continue  # already emitted above, tailored

        _add_heading(document, section.heading)
        as_list = _section_is_a_list(section.lines, rewrites)
        for line in section.lines:
            stripped = line.strip()
            if not stripped:
                continue
            body = strip_bullet(stripped)
            replacement = rewrites.get(stripped) or rewrites.get(body)

            if _is_role_line(stripped):
                _add_role(document, stripped)
            elif _is_subheading(stripped) and not replacement:
                _add_subheading(document, stripped)
            elif as_list or replacement:
                _add_bullet(document, replacement or body)
            else:
                _add(document, body)

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
    return section.lines[:5] if section else []


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
    lines += ["", "REWRITTEN BULLETS:"]
    for bullet in result.bullets:
        lines += [
            f"  FROM: {bullet.get('original', '')}",
            f"  TO:   {bullet.get('tailored', '')}",
            f"  WHY:  {bullet.get('reason', '')}",
            "",
        ]
    if result.omitted:
        lines += ["OMITTED:"] + [f"  - {o}" for o in result.omitted] + [""]
    if result.guard and not result.guard.ok:
        lines += ["FABRICATION GUARD — REJECTED:", result.guard.report(), ""]

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
