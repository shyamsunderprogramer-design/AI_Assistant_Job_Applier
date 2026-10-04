"""Organise a resume's blocks into sections, jobs and facts, each traceable to its source.

    contact · summary · skills · experience · projects · education · certifications · other

A job (an "entry") keeps its title, employer, dates and its own bullets
together: a bullet belongs to the entry above it until the next entry or
section begins, so it can never drift to another employer. Every line becomes
a Fact with an id ("f12") and the source it came from, which is what a match
or a generated sentence cites.

What could make the output wrong is recorded as a warning rather than
guessed past: no section headings, a job with no dates, dates that cannot be
read or run backwards, a section with nothing in it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date

from ml.tailoring.reader import Block, Extracted

# Checked in this order: the more specific kinds first, so "Core Competencies
# / Technical Skills Summary" is skills, not a summary.
SECTION_WORDS = {
    "skills": ("skill", "competenc", "technolog", "tools", "expertise", "proficienc"),
    "experience": ("experience", "employment", "work history", "career history", "professional background"),
    "projects": ("project",),
    "education": ("education", "academic", "qualification"),
    "certifications": ("certif", "license", "accreditation", "training", "courses"),
    "summary": ("summary", "objective", "profile", "about me"),
}
OTHER_HEADINGS = ("awards", "achievements", "highlights", "publications", "volunteer", "languages",
                  "interests", "references", "activities", "impact")

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_POINT = rf"(?:{_MONTH}\s+\d{{4}}|\d{{1,2}}/\d{{4}}|\d{{4}})"
_NOW = r"(?:present|current|now|today|ongoing)"
DATE_RANGE = re.compile(rf"({_POINT})\s*(?:–|—|-|to|until)\s*({_POINT}|{_NOW})", re.I)
SINGLE_DATE = re.compile(rf"\b({_POINT})\b", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
URL = re.compile(r"(?:https?://)?(?:www\.)?(?:linkedin\.com|github\.com|gitlab\.com|[\w-]+\.(?:dev|io|me|com))/[\w/.-]*", re.I)


@dataclass
class Fact:
    id: str
    text: str
    section: str
    kind: str                      # bullet | text | entry | group | contact
    source: str
    entry: str | None = None       # the entry id this fact belongs to
    confidence: str = "high"


@dataclass
class Entry:
    id: str
    section: str
    header: str
    title: str
    employer: str
    dates: str
    start: str | None              # "YYYY-MM"
    end: str | None                # "YYYY-MM", or None with present=True
    present: bool
    source: str
    facts: list[str] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)


@dataclass
class ResumeModel:
    name: str
    contact: dict
    facts: list[Fact]
    entries: list[Entry]
    sections: dict[str, list[str]]           # section -> fact ids, in order
    warnings: list[str]
    ocr: bool = False

    def fact(self, fid: str) -> Fact | None:
        return next((f for f in self.facts if f.id == fid), None)

    def entry(self, eid: str) -> Entry | None:
        return next((e for e in self.entries if e.id == eid), None)

    def to_dict(self) -> dict:
        return asdict(self)


def section_kind(text: str) -> str | None:
    """Which section a heading line names, or None if it is not a heading."""
    low = text.strip().rstrip(":").lower()
    if not low or len(low) > 60:
        return None
    for kind, words in SECTION_WORDS.items():
        if any(w in low for w in words):
            return kind
    if any(w in low for w in OTHER_HEADINGS):
        return "other"
    return None


def _is_heading(block: Block) -> str | None:
    """The section a block opens, or None.

    It must name a section AND read as a heading -- a heading style, or the
    shape the base parser recognises (all capitals, or a line made of section
    words). "Highlights of Impact" in mixed case inside a job is a group in
    that job, not a new section; cutting the job there lost three jobs.
    """
    from ml.resume.parser import looks_like_heading
    text = block.text.strip()
    if block.list_item or len(text) > 60 or DATE_RANGE.search(text) or text.endswith("."):
        return None
    kind = section_kind(text)
    if kind and (block.heading or looks_like_heading(text)):
        return kind
    return None


def parse_point(text: str) -> str | None:
    t = text.strip().lower().rstrip(".")
    m = re.match(r"([a-z]+)\.?\s+(\d{4})$", t)
    if m and m.group(1)[:3] in MONTHS:
        return f"{m.group(2)}-{MONTHS[m.group(1)[:3]]:02d}"
    m = re.match(r"(\d{1,2})/(\d{4})$", t)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{4})$", t)
    if m:
        return f"{m.group(1)}-01"
    return None


def split_header(header: str) -> tuple[str, str]:
    """(title, employer) from a job line with its dates removed. Best effort."""
    head = DATE_RANGE.sub("", header).strip(" ,|–—-\t")
    parts = [p.strip() for p in re.split(r"\s*(?:,|\||–|—|\s-\s|\bat\b|@)\s*", head) if p.strip()]
    if len(parts) >= 2:
        # Everything after the title is the employer as written -- "PR DEPT.,
        # GO., AP, IND" is one employer, not "PR DEPT." and a location.
        return parts[0], ", ".join(parts[1:])
    return head, ""


def build(extracted: Extracted) -> ResumeModel:
    blocks = extracted.blocks
    facts: list[Fact] = []
    entries: list[Entry] = []
    sections: dict[str, list[str]] = {}
    warnings = list(extracted.warnings)
    contact: dict = {"emails": [], "phones": [], "links": [], "location": ""}

    def add_fact(text, section, kind, block, entry=None) -> Fact:
        f = Fact(f"f{len(facts) + 1}", text, section, kind, block.source, entry, block.confidence)
        facts.append(f)
        sections.setdefault(section, []).append(f.id)
        if entry:
            next(e for e in entries if e.id == entry).facts.append(f.id)
        return f

    section = "contact"
    current: Entry | None = None
    pending_title: Block | None = None
    headings_seen = 0
    name = ""

    for i, block in enumerate(blocks):
        text = block.text.strip()
        kind = _is_heading(block)
        if kind:
            headings_seen += 1
            section, current, pending_title = kind, None, None
            continue

        if section == "contact":
            if not name and not EMAIL.search(text) and not PHONE.search(text) and len(text.split()) <= 5:
                name = text
            contact["emails"] += EMAIL.findall(text)
            contact["phones"] += [p.strip() for p in PHONE.findall(text) if sum(c.isdigit() for c in p) >= 10]
            contact["links"] += URL.findall(text) + block.links
            add_fact(text, "contact", "contact", block)
            continue

        dated = DATE_RANGE.search(text)
        if section in ("experience", "projects", "education") and dated and not block.list_item:
            # A new job: the line with the dates, plus the line above it when
            # that line was a title with no dates of its own.
            header = text
            if pending_title is not None:
                header = f"{pending_title.text} {text}"
            title, employer = split_header(header)
            start, end = parse_point(dated.group(1)), parse_point(dated.group(2))
            present = bool(re.match(_NOW, dated.group(2), re.I))
            entry = Entry(f"e{len(entries) + 1}", section, header, title, employer, dated.group(0),
                          start, end, present, block.source)
            if not start or (not end and not present):
                entry.uncertain.append(f"The dates “{dated.group(0)}” could not be fully read.")
            elif end and start > end:
                entry.uncertain.append(f"The dates “{dated.group(0)}” run backwards.")
            if not employer and section == "experience":
                entry.uncertain.append("The employer could not be told apart from the title.")
            entries.append(entry)
            current, pending_title = entry, None
            add_fact(header, section, "entry", block, entry.id)
            continue

        if section in ("experience", "projects") and current is None and not block.list_item:
            # Possibly a title line whose dates are on the next line.
            nxt = blocks[i + 1].text if i + 1 < len(blocks) else ""
            if DATE_RANGE.search(nxt) and not DATE_RANGE.search(text):
                pending_title = block
                continue

        is_group = (not block.list_item and current is not None and len(text.split()) <= 8
                    and not text.endswith(".") and not DATE_RANGE.search(text))
        fkind = "bullet" if block.list_item else ("group" if is_group else "text")
        add_fact(text, section, fkind, block, current.id if current else None)

    # -- uncertainty ------------------------------------------------------------
    if headings_seen == 0:
        warnings.append("No section headings were found, so sections could not be told apart. "
                        "Check the extracted text below.")
    if "experience" not in sections:
        warnings.append("No work experience section was found.")
    for e in entries:
        if e.section == "experience" and len(e.facts) <= 1:      # only its own header line
            e.uncertain.append("No description was found under this job.")
        warnings += [f"{e.header[:60]}: {u}" for u in e.uncertain]
    if any(f.confidence == "low" for f in facts):
        warnings.append("Some text was recognised from a scanned page and may contain errors.")

    contact = {k: (list(dict.fromkeys(v)) if isinstance(v, list) else v) for k, v in contact.items()}
    return ResumeModel(name, contact, facts, entries, sections, warnings, extracted.ocr)


def today() -> str:
    return date.today().strftime("%Y-%m")
