"""Resume templates: one set of type and spacing, read by both the .docx and the PDF writer.

Every template is ATS-safe by construction -- the writers never draw tables,
columns, text boxes or images whatever the template says -- so a template
changes only how the page looks: the font, the sizes, the spacing and the
colour of the headings.

  classic  the person's own format (Oct 2026): Cambria, black, 0.65" margins.
  modern   Calibri, dark blue headings: a cleaner sans-serif page.
  compact  Cambria a size smaller, tighter spacing: built for one or two pages.

A tailoring chooses its template on the tailoring page; Settings holds the
default (resume.template). The choice travels with the draft
(TailorResult.template), so every later render -- a fit measurement, an
edit, a line put back -- draws the same template.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Style:
    key: str
    label: str
    description: str
    font: str                  # the family named in the .docx, and embedded in the PDF
    body: float = 11           # points
    heading: float = 12
    name: float = 18
    contact: float = 11
    ink: str = "000000"        # hex, no "#"
    heading_color: str = "000000"
    rule_color: str = "BFBFBF"
    margin_in: float = 0.65
    gap: float = 3             # after a body paragraph or bullet
    heading_before: float = 8
    heading_after: float = 4
    role_before: float = 6
    title_after: float = 6
    bullet_at_in: float = 0.15
    bullet_text_in: float = 0.30
    leading: float = 1.17      # single spacing, as Word sets the face


TEMPLATES: dict[str, Style] = {
    "classic": Style("classic", "Classic", "Cambria, black, 0.65\" margins: your format.", "Cambria"),
    "modern": Style("modern", "Modern", "Calibri with dark blue headings: a cleaner sans-serif page.",
                    "Calibri", body=11, heading=12.5, name=20, contact=10.5,
                    heading_color="1F3864", rule_color="1F3864", heading_before=10, leading=1.2),
    "compact": Style("compact", "Compact", "A size smaller with tighter spacing: for one or two pages.",
                     "Cambria", body=10, heading=11, name=16, contact=10, margin_in=0.5, gap=2,
                     heading_before=6, heading_after=2, role_before=4, title_after=4,
                     bullet_at_in=0.12, bullet_text_in=0.25, leading=1.15),
}
DEFAULT = "classic"


def style_for(result=None, name: str | None = None) -> Style:
    """The template a draft asked for, else the default."""
    key = name or (getattr(result, "template", "") if result is not None else "") or DEFAULT
    return TEMPLATES.get(key, TEMPLATES[DEFAULT])


def choices() -> list[dict]:
    return [{"key": s.key, "label": s.label, "description": s.description} for s in TEMPLATES.values()]
