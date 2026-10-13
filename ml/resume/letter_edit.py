"""Edit a job's cover letter in the browser: checked as you type, every version kept.

The letter is written once by the model (ml/resume/letter.py) under the
fabrication guard. Until now changing a word meant opening the file. This
reads it into parts (greeting, paragraphs, closing), checks an edit the way
the written letter was checked, and saves it with the version before it kept.

The checks, each naming the words it is about:

  claims      the same guard as the written letter: a skill, number or name
              the resume does not show is flagged. The person may keep it --
              it is their letter -- but never without seeing it.
  length      over 320 words, or under 120; more than five paragraphs
  stock       "I am writing to express", "passionate", "leverage", long dashes:
              the phrases that mark a letter as written by nobody
  job back    "The role requires..." -- describing the job to the people who wrote it
  close       "I look forward to hearing from you" -- the close every applicant sends
  details     no greeting, no name under the closing, a placeholder left in
              ("[Company]"), the company never named, a sentence over 40 words

Saving writes cover-letter.md (what the application attaches and the job page
links), a .docx and a .pdf with the person's name and contact line at the
top, and keeps the version it replaced in letter-versions/.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from ml.resume.letter import MAX_WORDS, check_letter

VERSIONS = "letter-versions"
KEEP_VERSIONS = 30
DOCX_FILE = "cover-letter.docx"
PDF_FILE = "cover-letter.pdf"

GREETING = re.compile(r"^(dear|hello|hi|to whom|greetings)\b", re.I)
CLOSING = re.compile(r"^(sincerely|best|regards|kind regards|warm regards|best regards|thank you|thanks|"
                     r"respectfully|yours|cheers|with thanks|many thanks)\b", re.I)
STOCK = ("i am writing to express", "i am writing to apply", "passionate", "leverage", "utilize", "spearhead",
         "seamless", "robust", "dynamic", "cutting-edge", "cutting edge", "delve", "pivotal", "meticulous",
         "transformative", "elevate", "empower", "proven track record", "fast-paced", "synergy", "thrilled",
         "furthermore", "moreover", "additionally", "team player", "results-driven", "go-getter",
         "i believe i would be a great fit", "perfect fit", "dream job")
JOB_BACK = re.compile(r"(^|[.!?]\s+)(the|this)\s+(role|position|job|opening)\s+(requires|involves|is responsible|calls for|"
                      r"needs|demands)|(^|[.!?]\s+)your (company|team|organi[sz]ation) is (looking|seeking|searching)", re.I)
GENERIC_CLOSE = ("look forward to hearing from you", "thank you for your time and consideration",
                 "i am ready to discuss how my experience", "please do not hesitate to contact",
                 "would welcome the opportunity to discuss")
PLACEHOLDER = re.compile(r"\[[^\]]{2,40}\]|\{[^}]{2,40}\}|<[^>]{2,40}>|\bXXX+\b|\bTBD\b|lorem ipsum", re.I)


@dataclass
class Finding:
    severity: str            # problem | warning | tip
    title: str
    detail: str = ""
    quotes: list[str] = field(default_factory=list)


def parse(text: str) -> dict:
    """{greeting, paragraphs[], closing}: the letter as the editor shows it."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", (text or "").strip()) if b.strip()]
    greeting = blocks.pop(0) if blocks and (GREETING.match(blocks[0]) or blocks[0].rstrip().endswith(",")
                                            and len(blocks[0]) < 80) else ""
    closing = blocks.pop() if blocks and CLOSING.match(blocks[-1]) else ""
    return {"greeting": greeting, "paragraphs": blocks, "closing": closing}


def join(parts: dict) -> str:
    """The letter as it is saved: one blank line between its parts."""
    pieces = [str(parts.get("greeting") or "").strip(),
              *[str(p).strip() for p in parts.get("paragraphs") or []],
              str(parts.get("closing") or "").strip()]
    return "\n\n".join(p for p in pieces if p) + "\n"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def check(text: str, resume_text: str, jd_text: str = "", company: str = "") -> dict:
    """Every check above, on the whole letter: {words, paragraphs, findings[], claims[]}."""
    parts = parse(text)
    body = "\n\n".join(parts["paragraphs"])
    low = text.lower()
    words = len(body.split())
    out: list[Finding] = []

    verdict = check_letter(resume_text or "", jd_text or "", body)
    claims = [{"kind": v.kind, "value": v.value, "context": v.context} for v in verdict.violations]
    if claims:
        names = list(dict.fromkeys(c["value"] for c in claims))
        out.append(Finding("problem", "Not on your resume: " + ", ".join(names[:6]),
                           "The letter claims something your resume does not show. Keep it only if it is true, "
                           "and add it to your resume too, so the two agree.",
                           [c["context"] for c in claims[:4]]))
    holes = PLACEHOLDER.findall(text)
    if holes:
        out.append(Finding("problem", "A placeholder is left in", "Replace it before sending.", holes[:4]))
    if not parts["greeting"]:
        out.append(Finding("warning", "No greeting", "Start with “Dear Hiring Manager,” or the person's name."))
    closing = parts["closing"]
    if not closing:
        out.append(Finding("warning", "No closing", "End with “Sincerely,” and your name on the next line."))
    elif len([l for l in closing.splitlines() if l.strip()]) < 2:
        out.append(Finding("warning", "No name under the closing", "Put your full name on the line after it."))
    if words > MAX_WORDS:
        out.append(Finding("warning", f"{words} words", f"Keep it under {MAX_WORDS}: it is read at the end of a long "
                                                        "day. Cut the paragraph that repeats the resume."))
    elif words < 120 and parts["paragraphs"]:
        out.append(Finding("tip", f"Only {words} words", "Room for one concrete story from your resume."))
    if len(parts["paragraphs"]) > 5:
        out.append(Finding("tip", f"{len(parts['paragraphs'])} paragraphs", "Three is usual: why this job, "
                                                                           "the evidence, the close."))
    if company:
        # Named if any distinctive word of the company's name is in the letter: "2K's" names "2k games".
        generic = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "group", "the", "holdings", "and",
                   "of", "plc", "lp", "llp", "gmbh", "sa", "ag", "usa", "us", "international"}
        words = [w for w in re.findall(r"[a-z0-9][a-z0-9&'-]*", company.lower()) if w not in generic and len(w) >= 2]
        letter_words = set(re.findall(r"[a-z0-9][a-z0-9&-]*", low))
        if words and not any(w in letter_words for w in words):
            out.append(Finding("tip", f"{company} is never named", "Say what about this employer made you write."))
    stock = sorted({s for s in STOCK if re.search(rf"(?<![a-z]){re.escape(s)}(?![a-z])", low)})
    if stock:
        out.append(Finding("warning", "Stock phrases: " + ", ".join(stock[:6]),
                           "Every letter says them, so the reader stops reading. Say the thing itself instead."))
    if "—" in text:
        out.append(Finding("tip", "Long dashes", "Use a comma or a full stop: long dashes read as machine-written."))
    back = [s for s in _sentences(body) if JOB_BACK.search(s)]
    if back:
        out.append(Finding("warning", "Describes the job back to them",
                           "They wrote the posting. Say what you do and what you did instead.", back[:2]))
    generic = [g for g in GENERIC_CLOSE if g in low]
    if generic:
        out.append(Finding("tip", "A close every applicant sends",
                           "Name something at this employer you would want to start on.", generic[:2]))
    long = [s for s in _sentences(body) if len(s.split()) > 40]
    if long:
        out.append(Finding("tip", f"{len(long)} sentence(s) over 40 words", "Split them.", [s[:160] for s in long[:2]]))
    order = {"problem": 0, "warning": 1, "tip": 2}
    out.sort(key=lambda f: order[f.severity])
    return {"words": words, "paragraphs": len(parts["paragraphs"]), "findings": [asdict(f) for f in out],
            "claims": claims, "ok": not any(f.severity == "problem" for f in out)}


# -- saving, with every earlier version kept ----------------------------------------------

def versions(folder: Path) -> list[dict]:
    root = folder / VERSIONS
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.glob("*.md"), reverse=True):
        text = path.read_text(encoding="utf-8", errors="replace")
        body = " ".join(parse(text)["paragraphs"])
        stamp = path.stem
        try:
            when = datetime.strptime(stamp[:15], "%Y%m%d-%H%M%S").strftime("%b %-d, %H:%M")
        except ValueError:
            when = stamp
        out.append({"id": stamp, "when": when, "words": len(body.split()), "start": body[:90]})
    return out


def save(folder: Path, text: str, header: list[str] | None = None, template: str | None = None,
         look: dict | None = None) -> dict:
    """Write the letter and its documents; the version it replaces goes into letter-versions/."""
    from ml.resume.packet import LETTER_FILE
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / LETTER_FILE
    text = (text or "").strip() + "\n"
    if target.exists():
        old = target.read_text(encoding="utf-8", errors="replace")
        if old.strip() == text.strip():
            return {"saved": False, "files": _documents(folder, text, header, template, look)}
        keep = folder / VERSIONS
        keep.mkdir(exist_ok=True)
        (keep / f"{datetime.now():%Y%m%d-%H%M%S}.md").write_text(old, encoding="utf-8")
        for stale in sorted(keep.glob("*.md"))[:-KEEP_VERSIONS]:
            stale.unlink()
    target.write_text(text, encoding="utf-8")
    return {"saved": True, "files": _documents(folder, text, header, template, look)}


def restore(folder: Path, version_id: str, header=None, template=None, look=None) -> str:
    if not re.fullmatch(r"[\w-]{6,40}", version_id or ""):
        raise ValueError("No such version.")
    path = folder / VERSIONS / f"{version_id}.md"
    if not path.exists():
        raise ValueError("That version is not there any more.")
    text = path.read_text(encoding="utf-8", errors="replace")
    save(folder, text, header, template, look)          # the current one is kept in turn
    return text


# -- the letter as a document ------------------------------------------------------------

def _documents(folder: Path, text: str, header: list[str] | None, template: str | None,
               look: dict | None) -> dict:
    """cover-letter.docx and .pdf: the name and contact line on top, the date, the letter."""
    try:
        write_docx(folder / DOCX_FILE, text, header or [], template, look)
        write_pdf(folder / PDF_FILE, text, header or [], template, look)
        return {"docx": DOCX_FILE, "pdf": PDF_FILE}
    except Exception:                       # the .md is the letter; documents are a convenience
        return {}


def _contact_lines(header: list[str]) -> tuple[str, str]:
    from ml.resume.writer import _is_contact_line
    name = header[0] if header else ""
    contact = next((h for h in header[1:] if _is_contact_line(h) or "linkedin" in h.lower()), "")
    return name, contact


def write_docx(path: Path, text: str, header: list[str], template: str | None = None, look: dict | None = None):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor
    from ml.resume.style import style_for
    st = style_for(name=template, look=look)
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = st.font, Pt(st.body)
    normal.paragraph_format.space_after = Pt(8)
    for section in doc.sections:
        section.left_margin = section.right_margin = Inches(1)
        section.top_margin = section.bottom_margin = Inches(0.9)
    name, contact = _contact_lines(header)
    align = WD_ALIGN_PARAGRAPH.LEFT if st.header_align == "left" else WD_ALIGN_PARAGRAPH.CENTER
    if name:
        p = doc.add_paragraph()
        p.alignment = align
        run = p.add_run(name)
        run.bold, run.font.size = True, Pt(st.name - 2)
        run.font.color.rgb = RGBColor.from_string(st.heading_color)
        p.paragraph_format.space_after = Pt(2)
    if contact:
        p = doc.add_paragraph(contact.replace(" | ", " · "))
        p.alignment = align
        p.paragraph_format.space_after = Pt(18)
    doc.add_paragraph(datetime.now().strftime("%B %-d, %Y")).paragraph_format.space_after = Pt(12)
    parts = parse(text)
    for block in [parts["greeting"], *parts["paragraphs"]]:
        if block:
            p = doc.add_paragraph(block)
            p.paragraph_format.space_after = Pt(10)
    if parts["closing"]:
        lines = [l for l in parts["closing"].splitlines() if l.strip()]
        p = doc.add_paragraph(lines[0])
        p.paragraph_format.space_after = Pt(2 if len(lines) > 1 else 10)
        for line in lines[1:]:
            doc.add_paragraph(line).paragraph_format.space_after = Pt(0)
    doc.save(path)


def write_pdf(path: Path, text: str, header: list[str], template: str | None = None, look: dict | None = None):
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from ml.resume import pdf as resume_pdf
    from ml.resume.style import style_for
    st = style_for(name=template, look=look)
    with resume_pdf._STYLE_LOCK:
        font, bold = resume_pdf._register_font(st.font)
    esc = resume_pdf._escape
    align = TA_LEFT if st.header_align == "left" else TA_CENTER
    body = ParagraphStyle("b", fontName=font, fontSize=st.body, leading=st.body * 1.3, spaceAfter=10)
    story = []
    name, contact = _contact_lines(header)
    if name:
        story.append(Paragraph(esc(name), ParagraphStyle("n", parent=body, fontName=bold, fontSize=st.name - 2,
                                                          leading=(st.name - 2) * 1.2, alignment=align,
                                                          textColor=resume_pdf._hex(st.heading_color),
                                                          spaceAfter=2)))
    if contact:
        story.append(Paragraph(esc(contact.replace(" | ", " · ")),
                               ParagraphStyle("c", parent=body, alignment=align, spaceAfter=18)))
    story.append(Paragraph(datetime.now().strftime("%B %-d, %Y"), body))
    parts = parse(text)
    for block in [parts["greeting"], *parts["paragraphs"]]:
        if block:
            story.append(Paragraph(esc(block), body))
    if parts["closing"]:
        story.append(Spacer(1, 2))
        story.append(Paragraph("<br/>".join(esc(l) for l in parts["closing"].splitlines() if l.strip()), body))
    SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=inch, rightMargin=inch, topMargin=0.9 * inch,
                      bottomMargin=0.9 * inch, title=f"{name} — Cover letter" if name else "Cover letter",
                      author=name).build(story)


# -- one paragraph rewritten, on request -------------------------------------------------

HOW = {
    "shorter": "Make it about a third shorter. Keep the facts that matter most.",
    "plainer": "Make it plainer: shorter sentences, everyday words, no stock phrases.",
    "specific": "Tie it more closely to this employer: one concrete detail from the posting, said once.",
    "story": "Tell it as one story: the situation, what the candidate did, and what changed because of it.",
}

REWRITE_SYSTEM = """You rewrite ONE paragraph of a cover letter. Reply with JSON only:
{"paragraph": "the rewritten paragraph"}
Rules:
- The resume is the ONLY source of truth about the candidate. Never add a skill, number, date, employer or
  result that is not in it. You may name the employer and quote the posting.
- No "I am writing to express", "passionate", "leverage", "utilize", "spearhead", "seamless", "robust",
  "dynamic", "cutting-edge", "Furthermore", "Moreover", "Additionally". No long dashes.
- Never describe the job back to the people who wrote it.
- First person, plain, the way the candidate talks. Return only the paragraph, no greeting or closing."""


def rewrite_paragraph(paragraph: str, how: str, resume_text: str, jd_text: str, company: str, title: str,
                      cfg, ask=None, whole: str = "") -> dict:
    if not paragraph.strip():
        raise ValueError("That paragraph is empty.")
    if how not in HOW:
        raise ValueError("Choose how to rewrite it.")
    if ask is None:
        from ml.tailoring.review import _ask as ask
    body = ask(REWRITE_SYSTEM, f"HOW: {HOW[how]}\n\nJOB: {title} at {company}\n\nPOSTING:\n{jd_text[:7000]}\n\n"
                               f"RESUME:\n{resume_text[:14000]}\n\nTHE WHOLE LETTER (for context):\n{whole[:4000]}\n\n"
                               f"THE PARAGRAPH TO REWRITE:\n{paragraph}", cfg)
    text = " ".join(str(body.get("paragraph") or "").split()).replace(" — ", ", ").replace("—", ", ")
    if not text:
        raise RuntimeError("The model wrote nothing.")
    verdict = check_letter(resume_text, jd_text, text)
    return {"paragraph": text, "claims": [{"kind": v.kind, "value": v.value} for v in verdict.violations]}
