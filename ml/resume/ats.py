"""How well a tailored resume matches its posting, the way an ATS reads it.

An applicant tracking system ranks a resume by how much of the posting's
vocabulary it finds in the file. So this scores the file that would be sent
-- the tailored PDF's own text, not the model's answer -- against the posting,
and the person's full resume beside it for comparison.

What is still missing splits in two, and the split is the useful part:

  fixable  the posting asks for it and the person's resume has it, but the
           tailored one lost it (cut to fit two pages, or reworded away).
           "Align closer" brings these back.
  gaps     the person's resume does not have it at all. Nothing honest adds
           these; they are listed so the person knows, not so they are filled.

Pure keyword coverage -- no role-fit weighting -- because that is what an ATS
measures. The "Fit" on the job page also asks whether the role is the
person's field; this asks only whether the words are there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ml.resume.scorer import score_resume


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    text = " ".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    # One line of text: a wrapped "distributed / systems" is still the phrase
    # to anything reading the file, an ATS included.
    return re.sub(r"\s+", " ", text)


# Paragraphs about pay, benefits, the law and the company's culture page. They
# are in every posting and say nothing about the work, so "paid", "off" and
# "market" (from "paid time off", "market range") were being reported as
# skills the resume lacks.
BOILERPLATE = re.compile(
    r"compensation|salary|pay range|benefits|401\(k\)|paid (time|leave)|"
    r"equal[- ]opportunity|discriminat|accommodation|background check|"
    r"e-verify|drug[- ]free|we are an? .{0,30}employer|privacy (notice|policy)|"
    r"learn more here|originally posted", re.I)


def requirements_only(jd_text: str) -> str:
    """The posting without its pay, benefits and legal paragraphs."""
    paragraphs = re.split(r"\n\s*\n|\n(?=\s)", jd_text or "")
    kept = [p for p in paragraphs if not BOILERPLATE.search(p)]
    return "\n\n".join(kept) if kept else jd_text


# Plain words the posting's prose uses, not things a resume could show.
FILLER = {"next", "part", "bring", "come", "challenges", "large", "additional", "similar",
          "value", "join", "looking", "including", "within", "across", "ability", "strong",
          "work", "working", "team", "teams", "role", "opportunity", "help", "new",
          "contributing", "contribute", "passionate", "enjoy", "dream", "building",
          "available", "highly", "using", "make", "making", "ensure", "deliver",
          "delivering", "provide", "providing", "drive", "driving",
          # words a company uses about itself, not kinds of work
          "brand", "brands", "solutions", "associate", "associates", "customer",
          "customers", "client", "clients", "company", "organization", "mission",
          "culture", "people", "members", "partners", "world", "experience",
          "cutting-edge", "founder", "founders", "entrepreneurs", "autonomy", "upside",
          "equity", "ownership", "mindset", "exceptional", "elite", "world-class",
          "complete", "maximum", "highlights", "launching", "initiatives"}


def as_phrases(terms: list[str], jd_text: str) -> list[str]:
    """The posting's own name for each missing term.

    The scorer counts words, so "Online Data Stores" came back as "online"
    and "stores" -- fragments nobody could act on. Where the posting writes
    the term inside a capitalised name, show that name, once.
    """
    shown: list[str] = []
    for term in terms:
        # Capitalised words around the term (case-sensitive), the term itself
        # in any case; a name the posting uses at least twice.
        found = re.findall(r"(?:[A-Z][\w/-]*[ \t]+){0,3}(?i:" + re.escape(term) + r")(?:[ \t]+[A-Z][\w/-]*){0,3}",
                           jd_text or "")
        names = [f.strip() for f in found if len(f.split()) > 1 and f[:1].isupper()]
        repeated = [n for n in set(names) if names.count(n) >= 2]
        best = max(repeated, key=lambda n: (names.count(n), -len(n))).lower() if repeated else term
        if not any(best == s or best in s or s in best for s in shown):
            shown = [s for s in shown if s not in best] + [best]
    return shown


def _shown(terms: list[str]) -> list[str]:
    """Drop filler, including a phrase that starts with a filler word."""
    return [t for t in terms if t not in FILLER and t.split()[0] not in FILLER]


def _sentences(jd_text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?:;])\s+|\n+|\s+[-\u2022]\s+", jd_text or "") if s.strip()]


def _mid_sentence_capital(term: str, sentence: str) -> bool:
    """Written capitalised where an ordinary word would not be: a name."""
    for m in re.finditer(r"\b" + re.escape(term) + r"\b", sentence, re.I):
        before = sentence[:m.start()].rstrip()
        if m.group(0)[:1].isupper() and before and before[-1:].isalnum():
            return True
    return False


def is_tool(term: str, jd_text: str = "") -> bool:
    """A named technology -- never added -- rather than a kind of work.

    A known skill, or a name the posting writes beside one ("Protobuf and
    Avro" in a sentence about schemas and Kafka). A capitalised word with no
    skill near it is a company or a place ("founder of Adjust", "the
    Americas"), and one only capitalised at a sentence's start is an
    ordinary word ("Complete autonomy") -- neither is a tool.
    """
    from ml.resume.scorer import SKILL_TERMS
    if term in SKILL_TERMS:
        return True
    if " " in term:
        return False
    for sentence in _sentences(jd_text):
        if _mid_sentence_capital(term, sentence):
            words = set(re.findall(r"[a-z0-9+#.-]+", sentence.lower()))
            if words & (SKILL_TERMS - {term}):
                return True
    return False


def _is_name(term: str, jd_text: str) -> bool:
    """Capitalised mid-sentence somewhere: a company, place or product."""
    return " " not in term and any(_mid_sentence_capital(term, s) for s in _sentences(jd_text))


def split_gaps(gaps: list[str], jd_text: str) -> tuple[list[str], list[str]]:
    """(concepts a resume line may be framed with, tools it may never claim).

    Anything that is neither is dropped from both: a company name or a
    sentence-start word is not something a resume is missing.
    """
    tools = [g for g in gaps if is_tool(g, jd_text)]
    # Only a phrase names a kind of work: "online data stores", "on-premise".
    # A lone word is a fragment ("hands" of hands-on, "based" of cloud-based),
    # company talk ("brands", "support") or a claim about the person's sector
    # ("federal") -- framing a resume line with one of those pads it at best
    # and misleads at worst, so it is never offered.
    def is_concept(g: str) -> bool:
        if g in tools or _is_name(g, jd_text) or g in FILLER:
            return False
        return " " in g or "-" in g
    return [g for g in gaps if is_concept(g)], tools


def compare(base_text: str, tailored_text: str | None, jd_text: str,
            requirements: str | None = None, company: str | None = None) -> dict:
    jd_text = requirements_only(jd_text)
    requirements = requirements_only(requirements) if requirements else requirements
    before = score_resume(base_text, jd_text, requirements=requirements, company=company)
    out = {"before": before.percent, "terms": before.jd_terms, "after": None,
           "matched": before.matched, "fixable": [],
           "gaps": _shown(as_phrases(_shown(before.missing), jd_text))}
    out["concepts"], out["tools"] = split_gaps(out["gaps"], jd_text)
    if tailored_text is None:
        return out
    after = score_resume(tailored_text, jd_text, requirements=requirements, company=company)
    have = set(before.matched)
    out.update({
        "after": after.percent,
        "matched": after.matched,
        "fixable": _shown([t for t in after.missing if t in have]),
        "gaps": _shown(as_phrases(_shown([t for t in after.missing if t not in have]), jd_text)),
    })
    out["concepts"], out["tools"] = split_gaps(out["gaps"], jd_text)
    return out


def tailored_pdf(cfg, job) -> Path:
    from ml.resume.packet import packet_root
    from ml.resume.writer import output_filename
    return (packet_root(cfg) / output_filename(job.company, job.title, job.external_id or "")).with_suffix(".pdf")


def check(cfg, job, base_text: str | None = None) -> dict:
    """The comparison for one job, saved beside its tailored resume."""
    if base_text is None:
        from ml.resume.pipeline import load_base_resume
        base_text = load_base_resume(cfg).text()
    pdf = tailored_pdf(cfg, job)
    result = compare(base_text, pdf_text(pdf) if pdf.exists() else None,
                     job.description or job.title, job.requirements, job.company)
    if pdf.exists():
        pdf.with_name(pdf.stem + "_ats.json").write_text(json.dumps(result, indent=1))
    return result


def summary(result: dict) -> str:
    if result.get("after") is None:
        return f"ATS match: your resume {result['before']}% — no tailored resume yet."
    line = f"ATS match with the posting: your resume {result['before']}% → tailored {result['after']}%"
    if result["fixable"]:
        line += f"; {len(result['fixable'])} term(s) your resume has could come back"
    return line
