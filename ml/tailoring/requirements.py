"""Turn a job description into an itemised list of requirements.

Each requirement keeps the employer's ORIGINAL wording (shown in the report),
a NORMALISED term for matching, a CATEGORY, a PRIORITY, and the full sentence
it came from as CONTEXT. A sentence naming several things ("Experience with
Terraform, Ansible and Kubernetes") becomes one requirement per thing, each
pointing back at that sentence.

A model reads the posting; then everything it returns is checked here:

  * the original wording must actually appear in the posting -- an item the
    model wrote itself is dropped, not reported;
  * the priority is decided from the posting's own words, transparently: an
    item under a "Preferred"/"Nice to have" heading, or in a sentence saying
    preferred, plus, bonus, ideally or desired, is preferred; everything else
    is required;
  * items that name the same thing (K8s and Kubernetes; a requirement the
    posting repeats) are merged into one, so evidence is never counted twice.

Without a model the same structure is built by rules, less finely.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from ml.tailoring.synonyms import canonical

CATEGORIES = ("skill", "tool", "responsibility", "experience", "education", "certification", "version")
PREFERRED = re.compile(r"\b(preferred|nice[- ]to[- ]have|bonus|a plus|is a plus|plus\b|ideally|desired|"
                       r"desirable|optional|good to have|advantageous)", re.I)
PREFERRED_HEADING = re.compile(r"(preferred|nice[- ]to[- ]have|bonus|desired|plus|additional)", re.I)
HEADING = re.compile(r"^\s*([A-Z][A-Za-z /&'-]{2,60}):?\s*$")

SYSTEM = """You read a job posting and list what it asks of a candidate.

Return ONLY JSON: {"requirements": [ ... ]}, each item:
  {"original":   "<the employer's exact words for this one item, copied from the posting>",
   "normalized": "<a short plain name for it, e.g. 'kubernetes', '5+ years devops', 'bachelor's degree in computer science'>",
   "category":   "skill" | "tool" | "responsibility" | "experience" | "education" | "certification" | "version",
   "context":    "<the full sentence or bullet it came from, copied exactly>"}

Rules:
- One item per separate requirement. Split a sentence that lists several tools or skills into one item each, all with the same context.
- "original" must be copied exactly from the posting -- never paraphrased.
- Include duties ("responsibility"), years ("experience"), degrees ("education"), certifications, and named versions ("version", e.g. "Java 17").
- Leave out pay, benefits, company description, legal and equal-opportunity text.
- Do not decide what is required or preferred; that is worked out from the posting."""


@dataclass
class Requirement:
    id: str
    original: str
    normalized: str
    category: str
    priority: str                     # required | preferred
    context: str
    also: list[str] = field(default_factory=list)   # the same thing worded elsewhere in the posting

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _locate(jd: str, phrase: str) -> int:
    """Where the phrase is in the posting (whitespace and case forgiving), or -1."""
    flat = _clean(jd).lower()
    return flat.find(_clean(phrase).lower())


def priority_of(jd: str, phrase: str, context: str) -> str:
    """Required or preferred, from the posting's own words.

    Preferred when the item's own sentence says so (preferred, a plus,
    bonus, ideally, desired, nice to have) or when it sits under a heading
    that does ("Preferred Qualifications", "Nice to have"). Required
    otherwise -- the posting did not say it was optional.
    """
    if PREFERRED.search(context or "") or PREFERRED.search(phrase or ""):
        return "preferred"
    needle = _clean(context or phrase).lower()[:80]
    heading = ""
    for line in _clean_lines(jd):
        if (HEADING.match(line) or (len(line.split()) <= 6 and line.endswith(":"))) and not line.startswith(("-", "•", "*")):
            heading = line
            continue
        if needle and needle in _clean(line).lower():
            return "preferred" if PREFERRED_HEADING.search(heading) else "required"
    return "required"


def _clean_lines(jd: str) -> list[str]:
    return [l.strip() for l in (jd or "").splitlines() if l.strip()]


def extract(jd: str, cfg=None, use_model: bool = True) -> tuple[list[Requirement], list[str]]:
    """(requirements, notes). Notes say what was dropped and why."""
    from ml.resume.ats import requirements_only
    text = requirements_only(jd or "")
    notes: list[str] = []
    items: list[dict] = []
    if use_model:
        try:
            from ml.resume.llm import complete, extract_json
            reply = complete(SYSTEM, "JOB POSTING:\n\n" + text, cfg, max_tokens=6000)
            items = extract_json(reply.text).get("requirements") or []
        except Exception as exc:
            notes.append(f"The model could not read the posting ({type(exc).__name__}); "
                         f"requirements were found by rules instead.")
            items = []
    if not items:
        items = _by_rules(text)

    reqs: list[Requirement] = []
    by_term: dict[str, Requirement] = {}
    for item in items:
        original = _clean(str(item.get("original") or ""))
        context = _clean(str(item.get("context") or original))
        if not original or _locate(text, original) < 0:
            if original:
                notes.append(f"Left out “{original[:60]}”: those words are not in the posting.")
            continue
        if _locate(text, context) < 0:
            context = original
        category = str(item.get("category") or "skill").lower()
        category = category if category in CATEGORIES else "skill"
        term = canonical(str(item.get("normalized") or original))
        priority = priority_of(text, original, context)
        if term in by_term:                         # the posting asks for it again
            kept = by_term[term]
            if original.lower() != kept.original.lower() and original not in kept.also:
                kept.also.append(original)
            if priority == "required":
                kept.priority = "required"          # asked for as required anywhere: required
            continue
        req = Requirement(f"r{len(reqs) + 1}", original, term, category, priority, context)
        reqs.append(req)
        by_term[term] = req
    return reqs, notes


def _by_rules(text: str) -> list[dict]:
    """Each requirement line of the posting: its years, its degree, and each tool it names.

    Longest names first, and a stretch of text is used once: "CI/CD" is one
    requirement, not "CI" and "CD".
    """
    from ml.resume.scorer import SKILL_TERMS
    from ml.tailoring.synonyms import SAME
    names = set(SKILL_TERMS) | set(SAME) | {n for ns in SAME.values() for n in ns}
    names = sorted((n for n in names if len(n) > 1), key=len, reverse=True)
    items = []
    for line in _clean_lines(text):
        if HEADING.match(line) or len(line.split()) < 3:
            continue
        body = line.lstrip("-•* ")
        years = re.search(r"\d+\+?\s*(?:years|yrs)[^.;,]*", body, re.I)
        if years:
            items.append({"original": years.group(0).strip(), "normalized": years.group(0).strip().lower(),
                          "category": "experience", "context": line})
        # A degree the posting asks for, not one in a founder's biography.
        if (re.search(r"\b(degree|bachelor|master|b\.s\.|m\.s\.|phd)\b", body, re.I)
                and not re.search(r"\b(was|were|did|previously|founded|holds)\b", body, re.I)):
            items.append({"original": body, "normalized": body.lower()[:60], "category": "education",
                          "context": line})
        taken = [False] * len(line)
        for name in names:
            for m in re.finditer(r"(?<![\w/])" + re.escape(name) + r"(?![\w/])", line, re.I):
                if any(taken[m.start():m.end()]):
                    continue
                taken[m.start():m.end()] = [True] * (m.end() - m.start())
                items.append({"original": m.group(0), "normalized": name, "category": "tool",
                              "context": line})
    return items


def as_json(reqs: list[Requirement]) -> str:
    return json.dumps([r.to_dict() for r in reqs], indent=1)
