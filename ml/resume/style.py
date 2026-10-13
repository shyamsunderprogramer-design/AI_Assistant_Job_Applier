"""Resume templates: one set of type and spacing, read by both the .docx and the PDF writer.

Every template is ATS-safe by construction -- the writers never draw tables,
columns, text boxes or images whatever the template says -- so a template
changes only how the page looks: the font, the sizes, the spacing, the
colour of the headings, and where the section names sit.

  classic  the person's own format (Oct 2026): Cambria, black, 0.65" margins.
  modern   Calibri, dark blue headings: a cleaner sans-serif page.
  compact  Cambria a size smaller, tighter spacing: built for one or two pages.
  minimal  whitespace and no lines: the name and the work, nothing else.
  sidebar  the section names in a left rail and the content beside them. It
           looks like two columns but is one: every line is an ordinary
           paragraph, the content indented, so an ATS reads it top to bottom.
           (A real two-column page is read across both columns at once.)

On top of any template, a look: another font (one the PDF can embed and Word
has) and an accent colour for the headings and rules.

A tailoring chooses its template on the tailoring page; Settings holds the
default (resume.template). The choice travels with the draft
(TailorResult.template and .look), so every later render -- a fit
measurement, an edit, a line put back -- draws the same template.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


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
    rule: bool = True          # a hairline under each section heading
    indent_in: float = 0.0     # everything but the header and the headings, moved right (the sidebar)
    header_align: str = "center"   # the name, contact and title lines: center or left


TEMPLATES: dict[str, Style] = {
    "classic": Style("classic", "Classic", "Cambria, black, 0.65\" margins: your format.", "Cambria"),
    "modern": Style("modern", "Modern", "Calibri with dark blue headings: a cleaner sans-serif page.",
                    "Calibri", body=11, heading=12.5, name=20, contact=10.5,
                    heading_color="1F3864", rule_color="1F3864", heading_before=10, leading=1.2),
    "compact": Style("compact", "Compact", "A size smaller with tighter spacing: for one or two pages.",
                     "Cambria", body=10, heading=11, name=16, contact=10, margin_in=0.5, gap=2,
                     heading_before=6, heading_after=2, role_before=4, title_after=4,
                     bullet_at_in=0.12, bullet_text_in=0.25, leading=1.15),
    "minimal": Style("minimal", "Minimal", "Wide margins, no lines, the name on the left: the work does the talking.",
                     "Calibri", body=10.5, heading=11, name=22, contact=10, heading_color="595959",
                     rule=False, margin_in=0.8, gap=4, heading_before=14, heading_after=4, role_before=8,
                     title_after=10, header_align="left", leading=1.2),
    "sidebar": Style("sidebar", "Sidebar", "Section names in a left rail, the work beside them: a two-column look "
                                           "that is one column underneath, so every ATS reads it in order.",
                     "Calibri", body=10.5, heading=11, name=22, contact=10, heading_color="1F3864",
                     rule_color="1F3864", rule=False, indent_in=1.3, margin_in=0.6, heading_before=12,
                     heading_after=2, role_before=4, title_after=10, header_align="left", leading=1.18),
}
DEFAULT = "classic"

# Fonts the PDF can embed on this Mac (ml/resume/pdf.py) and Word sets the .docx in.
FONTS = ("Cambria", "Calibri", "Aptos", "Georgia", "Arial", "Book Antiqua", "Times New Roman")
ACCENTS = {"000000": "Black", "1F3864": "Navy", "0B5563": "Teal", "2D5A27": "Forest",
           "7A1F2B": "Burgundy", "4A4A4A": "Graphite"}


def clean_look(look) -> dict:
    """{font, accent}: only a font and an accent this module knows; anything else dropped."""
    look = look if isinstance(look, dict) else {}
    out = {}
    if look.get("font") in FONTS:
        out["font"] = look["font"]
    accent = str(look.get("accent") or "").lstrip("#").upper()
    if accent in ACCENTS:
        out["accent"] = accent
    return out


def style_for(result=None, name: str | None = None, look: dict | None = None) -> Style:
    """The template a draft asked for, else the default, in the draft's font and accent."""
    key = name or (getattr(result, "template", "") if result is not None else "") or DEFAULT
    st = TEMPLATES.get(key, TEMPLATES[DEFAULT])
    look = clean_look(look if look is not None else getattr(result, "look", None))
    if look.get("font"):
        st = replace(st, font=look["font"])
    if look.get("accent"):
        # Headings and their rules take the accent; a grey hairline stays a hairline.
        st = replace(st, heading_color=look["accent"],
                     rule_color=look["accent"] if st.rule_color != "BFBFBF" else st.rule_color)
    return st


def choices() -> list[dict]:
    return [{"key": s.key, "label": s.label, "description": s.description} for s in TEMPLATES.values()]


def look_choices() -> dict:
    return {"fonts": list(FONTS), "accents": [{"key": k, "label": v} for k, v in ACCENTS.items()]}
