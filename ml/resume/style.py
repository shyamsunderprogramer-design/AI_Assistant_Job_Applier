"""Resume templates: one set of type and spacing, read by both the .docx and the PDF writer.

The writers never draw tables, columns, text boxes or images whatever the
template says, so a template changes only how the page looks: the font, the
sizes, the spacing, the colour of the headings, where the section names sit,
and how dates are written.

  classic  the person's own format (Oct 2026): Cambria, black, 0.65" margins.
  modern   Calibri, dark blue headings: a cleaner sans-serif page.
  compact  Cambria a size smaller, tighter spacing: built for one or two pages.
  minimal  whitespace and no lines: the name and the work, nothing else.
  federal  Times New Roman, black, every date as MM/YYYY: the plain page federal
           hiring (USAJOBS) and government contractors read months and years from.
  sidebar  the section names in a left rail and the content beside them. Every
           line is an ordinary paragraph, but a reader that groups text by
           position (pdfminer, under many resume parsers) takes the rail for a
           second column and reads the headings after their sections. Not
           offered for applying (ats_safe=False); fine to print or email.

Each template is proven, not assumed: ml/resume/ats_readback.verify_template
draws a resume in it and reads both files back with three PDF readers and two
.docx readers, and every tailored resume is read back the same way.

On top of any template, a look: another font (one the PDF can embed and Word
has) and an accent colour for the headings and rules.

A tailoring chooses its template on the tailoring page; Settings holds the
default (resume.template). The choice travels with the draft
(TailorResult.template and .look), so every later render -- a fit
measurement, an edit, a line put back -- draws the same template.
"""

from __future__ import annotations

import re
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
    dates: str = "words"       # "words": Feb 2025 – Present; "numeric": 02/2025 – Present
    ats_safe: bool = True      # every reader reads it in order (ml/resume/ats_readback.py)


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
    "federal": Style("federal", "Federal", "Times New Roman, black, dates as MM/YYYY: the plain page federal "
                                           "jobs (USAJOBS) and government contractors expect.",
                     "Times New Roman", body=11, heading=12, name=17, contact=11, rule_color="000000",
                     margin_in=0.75, heading_before=10, heading_after=3, role_before=7, dates="numeric"),
    "sidebar": Style("sidebar", "Sidebar", "Section names in a left rail, the work beside them. Some ATS readers "
                                           "take the rail for a second column: for printing, not for applying.",
                     "Calibri", body=10.5, heading=11, name=22, contact=10, heading_color="1F3864",
                     rule_color="1F3864", rule=False, indent_in=1.3, margin_in=0.6, heading_before=12,
                     heading_after=2, role_before=4, title_after=10, header_align="left", leading=1.18,
                     ats_safe=False),
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
    return [{"key": s.key, "label": s.label, "description": s.description, "ats_safe": s.ats_safe}
            for s in TEMPLATES.values()]


# -- the template for a job ---------------------------------------------------------------------------

# An employer's name that says government; "U.S. Bank" and a marketing agency do not.
_GOVERNMENT = re.compile(
    r"\b(department of|dept\.? of|county of|city of|state of|commonwealth of|town of|veterans affairs|"
    r"national guard|social security administration|government|"
    r"federal (?:bureau|reserve|aviation|emergency|trade|communications|highway|transit|deposit)|"
    r"(?:u\.s\.|united states) (?:department|dept|army|navy|air force|marine corps|coast guard|space force|"
    r"agency|bureau|office|postal|patent|census|courts?|secret service|mint|geological|forest|senate|house))\b",
    re.I)
# Work for the government itself: a clearance, public trust, a GS grade, a government contract. A
# commercial company that only sells to "federal customers" or holds FedRAMP is not this.
_CLEARANCE = re.compile(
    r"\b((?:active |current )?(?:security|secret|top secret|ts/sci) clearance|public trust|ts/sci|top secret|"
    r"clearance (?:is )?required|usajobs|gs-\d{1,2}\b|department of defense|dod\b|"
    r"(?:federal|government) contract(?:or|ing)?s?|government contracts?)", re.I)
# A company that says it is a startup -- not one that only says it moves like one.
_STARTUP = re.compile(
    r"\b(series [a-e]\b(?= (?:funding|round|company|startup|stage|backed|-))|seed[- ](?:stage|funded|round)|"
    r"venture[- ]backed|early[- ]stage (?:startup|company)|founding (?:engineer|team member)|"
    r"(?:we(?:'re| are)|is) an? (?:[\w-]+ ){0,3}start-?up\b)", re.I)


def for_job(source: str = "", company: str = "", title: str = "", description: str = "",
            default: str | None = None, clearance: str = "") -> tuple[str, str]:
    """(template, why) for a posting: the template a tailoring for it starts in.

      federal  a federal posting (USAJOBS), a government employer, or work that needs a clearance
               or public trust: federal HR and contractors' recruiters read months and years from
               every job, so the dates are MM/YYYY;
      modern   a startup: a cleaner sans-serif page;
      else     the person's default (Settings), for everyone else.
    Only ATS-safe templates are chosen. The person can change it on the job page.
    """
    default = default if default in TEMPLATES and TEMPLATES[default].ats_safe else DEFAULT
    text = f"{title}\n{description or ''}"
    if (source or "").lower() == "usajobs":
        return "federal", "a federal posting (USAJOBS)"
    if _GOVERNMENT.search(company or ""):
        return "federal", "a government employer"
    if clearance:
        named = clearance if "clearance" not in clearance.lower() else ""
        return "federal", f"the posting needs a {named + ' ' if named else 'security '}clearance".replace("  ", " ")
    m = _CLEARANCE.search(text)
    if m:
        return "federal", f"the posting asks for “{m.group(0)}”"
    m = _STARTUP.search(text)
    if m and default == DEFAULT:
        return "modern", f"a startup (“{m.group(0)}”)"
    return default, "your default template (Settings)"


def look_choices() -> dict:
    return {"fonts": list(FONTS), "accents": [{"key": k, "label": v} for k, v in ACCENTS.items()]}
