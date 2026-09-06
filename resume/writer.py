"""ATS-safe .docx output.

What breaks ATS parsers, and is therefore absent here by construction: tables,
multiple columns, text boxes, headers/footers, images, and unusual fonts. This
writer emits nothing but ordinary paragraphs in a standard font — visually
plain on purpose.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from resume.parser import Resume, strip_bullet
from resume.tailor import TailorResult

log = logging.getLogger(__name__)

FONT = "Calibri"          # widely parsed; no exotic glyphs
BODY_SIZE = Pt(10.5)
HEADING_SIZE = Pt(12)
NAME_SIZE = Pt(16)


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
    normal.paragraph_format.space_after = Pt(2)
    normal.paragraph_format.space_before = Pt(0)


def _add(document: Document, text: str, *, bold=False, size=None, align=None, space_before=0):
    para = document.add_paragraph()
    para.paragraph_format.space_before = Pt(space_before)
    if align is not None:
        para.alignment = align
    run = para.add_run(text)
    run.bold = bold
    run.font.name = FONT
    run.font.size = size or BODY_SIZE
    return para


def _add_heading(document: Document, text: str) -> None:
    _add(document, text.upper(), bold=True, size=HEADING_SIZE, space_before=10)


def _add_bullet(document: Document, text: str) -> None:
    # A literal "• " rather than a Word list style: list numbering XML is one
    # of the things weaker ATS parsers mangle.
    para = document.add_paragraph()
    para.paragraph_format.left_indent = Pt(12)
    para.paragraph_format.space_after = Pt(2)
    run = para.add_run(f"• {strip_bullet(text)}")
    run.font.name = FONT
    run.font.size = BODY_SIZE


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

    # Header: contact block, verbatim from the base resume.
    header = header_lines if header_lines is not None else _base_header(base)
    for i, line in enumerate(header):
        _add(document, line, bold=(i == 0), size=NAME_SIZE if i == 0 else None,
             align=WD_ALIGN_PARAGRAPH.CENTER)

    if result.summary:
        _add_heading(document, "Summary")
        _add(document, result.summary)

    for section in base.sections:
        if section.heading == "HEADER":
            continue
        if result.summary and section.heading.lower() in ("summary", "objective", "profile"):
            continue  # already emitted above, tailored

        _add_heading(document, section.heading)
        for line in section.lines:
            stripped = line.strip()
            if not stripped:
                continue
            body = strip_bullet(stripped)
            replacement = rewrites.get(stripped) or rewrites.get(body)
            if line.strip().startswith(("-", "•", "*", "▪")) or replacement:
                _add_bullet(document, replacement or body)
            else:
                _add(document, stripped)

    document.save(output_path)
    log.info("Wrote ATS-safe resume: %s", output_path)
    return output_path


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
