"""Match each requirement to evidence in the resume, and score the match.

Statuses, as the report shows them:

  direct     the resume clearly demonstrates the requirement itself
  related    it shows similar or transferable experience, not the exact thing
  unclear    relevant experience may be there, but the resume does not say enough
             (a tool named only in a skills list, with no work using it, is here)
  not_found  nothing in the resume supports it

Rules decide what they can, transparently: a job bullet naming the tool is
direct; something that counts as it (EKS for Kubernetes) is direct with the
reason; a comparable technology (GitLab CI for Jenkins) is related with the
reason. A model then judges duties, years, degrees and whatever rules left
open, comparing what the person did rather than their job titles -- and
everything it returns is checked:

  * every piece of evidence must be a real line of the resume (by its id);
  * a tool, skill or version can only be direct if a cited line names it, or
    something that counts as it -- otherwise it is lowered, never raised;
  * nothing it says is ever added to the resume; this is a report.

Score (the builder's estimate, not an employer's ATS):
    sum(weight × value) ÷ sum(weight) × 100
    weight: required 3, preferred 1      value: direct 1.0, related 0.5, else 0
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from ml.tailoring.requirements import Requirement
from ml.tailoring.structure import ResumeModel
from ml.tailoring.synonyms import implied_by, mentions, related_reason

WEIGHT = {"required": 3, "preferred": 1}
VALUE = {"direct": 1.0, "related": 0.5, "unclear": 0.0, "not_found": 0.0}
STATUSES = tuple(VALUE)
RULE_CATEGORIES = ("tool", "skill", "version", "certification")
# Where evidence of doing something is strongest.
SECTION_RANK = {"experience": 0, "projects": 1, "summary": 2, "certifications": 3,
                "education": 3, "skills": 4, "other": 5, "contact": 9}

JUDGE = """You compare a candidate's resume with a job's requirements and report the evidence.

For each requirement decide a status:
  "direct"     the resume clearly shows the requirement itself
  "related"    it shows similar or transferable experience, but not the exact requirement
  "unclear"    relevant experience may be present but the resume does not explain it enough
  "not_found"  nothing in the resume supports it

Rules:
- Cite evidence ONLY by the fact ids given (e.g. "f12"). Never quote or invent text.
- Judge by what the person DID (duties, tools, results), not by job titles. A different
  title with the same duties still matches; a title alone is not evidence.
- A similar technology is "related", never "direct" (e.g. GitLab CI for a Jenkins requirement).
- Years of experience: use the job dates given. Do not round up.
- Do not assume anything the resume does not say. When in doubt, "unclear".

Return ONLY JSON:
{"matches": [{"id": "<requirement id>", "status": "...", "evidence": ["f12", ...],
              "explanation": "<one or two sentences: why this evidence does or does not meet it>"}],
 "alignment": "<3-5 sentences: how the person's demonstrated responsibilities and skills relate to
               this position, citing only what the evidence shows; do not discuss title differences
               as a gap>"}"""


@dataclass
class Match:
    requirement: dict
    status: str
    evidence: list[dict] = field(default_factory=list)   # {id, text, source, where}
    explanation: str = ""
    method: str = "rules"                                 # rules | model | rules+model
    weight: int = 0
    value: float = 0.0
    contribution: float = 0.0


@dataclass
class Report:
    matches: list[Match]
    score: float
    total_weight: int
    alignment: str
    notes: list[str]

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def gaps(self) -> list[Match]:
        return [m for m in self.matches if m.status in ("unclear", "not_found")]


def _where(model: ResumeModel, fact) -> str:
    if fact.entry:
        e = model.entry(fact.entry)
        if e:
            return f"{e.title}{' at ' + e.employer if e.employer else ''} ({e.dates})"
    return fact.section.capitalize()


def _evidence(model: ResumeModel, ids) -> list[dict]:
    out = []
    for fid in ids:
        f = model.fact(fid)
        if f is not None and f.kind != "contact":
            out.append({"id": f.id, "text": f.text, "source": f.source, "where": _where(model, f),
                        "confidence": f.confidence})
    return out


def _by_strength(model: ResumeModel, facts) -> list:
    return sorted(facts, key=lambda f: (SECTION_RANK.get(f.section, 5), f.kind != "bullet"))


DEGREE_LEVEL = [(r"\b(ph\.?d|doctor)", 4), (r"\b(master|m\.s\.|m\.sc|mba|m\.tech|m\.e\.)", 3),
                (r"\b(bachelor|b\.s\.|b\.sc|b\.a\.|b\.tech|b\.e\.|undergraduate)", 2),
                (r"\b(associate)", 1)]


def _degree(text: str) -> int:
    import re
    low = (text or "").lower()
    return max((level for pattern, level in DEGREE_LEVEL if re.search(pattern, low)), default=0)


def _months(entry) -> int:
    from ml.tailoring.structure import today
    if not entry.start:
        return 0
    end = today() if entry.present else entry.end
    if not end or end < entry.start:
        return 0
    (y1, m1), (y2, m2) = map(int, entry.start.split("-")), map(int, end.split("-"))
    return (y2 - y1) * 12 + (m2 - m1) + 1


def years_match(req: Requirement, model: ResumeModel) -> tuple[str, list[str], str] | None:
    """"5+ years with Kubernetes": the dates of the jobs that show it, added up. Overlaps count once."""
    import re
    m = re.search(r"(\d+)\+?\s*(?:years|yrs)", req.original, re.I)
    if not m:
        return None
    need = int(m.group(1))
    from ml.resume.scorer import SKILL_TERMS
    from ml.tailoring.synonyms import SAME
    known = set(SKILL_TERMS) | set(SAME) | {n for ns in SAME.values() for n in ns}
    from ml.tailoring.synonyms import canonical
    tools = sorted({canonical(t) for t in known if len(t) > 2 and mentions(req.original, t)})
    jobs = [e for e in model.entries if e.section == "experience"]
    if tools:
        jobs = [e for e in jobs if any(mentions(model.fact(f).text, t) or implied_by(model.fact(f).text, t)
                                       for f in e.facts for t in tools)]
    months = set()
    for e in jobs:
        if e.start:
            y, mo = map(int, e.start.split("-"))
            for k in range(_months(e)):
                months.add((y + (mo - 1 + k) // 12, (mo - 1 + k) % 12))
    have = len(months) / 12
    what = f" with {', '.join(sorted(tools))}" if tools else ""
    ids = [e.facts[0] for e in jobs[:3]]
    if have >= need:
        return ("direct", ids, f"Your jobs{what} add up to about {have:.1f} years ({need}+ asked).")
    if have > 0:
        return ("related", ids, f"Your jobs{what} add up to about {have:.1f} years; {need}+ were asked.")
    return None


def degree_match(req: Requirement, model: ResumeModel) -> tuple[str, list[str], str] | None:
    want = _degree(req.original)
    if not want:
        return None
    degrees = [f for f in model.facts if f.section == "education" and _degree(f.text)]
    if not degrees:
        return None
    from ml.tailoring.structure import DATE_RANGE
    best = max(degrees, key=lambda f: _degree(f.text))
    name = DATE_RANGE.sub("", best.text).strip(" ,|–-\t")
    if _degree(best.text) >= want:
        return ("direct", [best.id], f"Your {name} meets the degree level asked for. Whether the field "
                                     f"matches is for the reader to judge.")
    return ("related", [best.id], f"Your {name} is below the degree level asked for.")


def _snippet(text: str, n: int = 90) -> str:
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


RANK = {"direct": 3, "related": 2, "unclear": 1}


def rule_match(req: Requirement, model: ResumeModel) -> tuple[str, list[str], str] | None:
    """(status, fact ids, explanation) from the rules, or None when rules cannot tell."""
    if req.any_of:
        # "Go, TypeScript, Python, or similar": any one of them meets it.
        from dataclasses import replace
        best, which = None, ""
        for choice in req.any_of:
            got = rule_match(replace(req, normalized=choice, original=choice, any_of=[]), model)
            if got and (best is None or RANK.get(got[0], 0) > RANK.get(best[0], 0)):
                best, which = got, choice
            if best and best[0] == "direct":
                break
        if best is None:
            return None
        return best[0], best[1], f"Any one of “{req.original}” is asked for; {which}: {best[2]}"
    if req.category == "experience":
        return years_match(req, model)
    if req.category == "education":
        return degree_match(req, model)
    if req.category not in RULE_CATEGORIES:
        return None
    term = req.normalized
    # A job's title and employer line is not evidence of a tool: "GO." in an
    # employer's name is not the Go language.
    facts = [f for f in model.facts if f.kind not in ("contact", "entry")]
    named = _by_strength(model, [f for f in facts if mentions(f.text, term)])
    worked = [f for f in named if f.section in ("experience", "projects", "summary")]
    if worked:
        where = _where(model, worked[0])
        return ("direct", [f.id for f in worked[:3]],
                f"Shown in your work — {where}: “{_snippet(worked[0].text)}”")
    implied = [(f, implied_by(f.text, term)) for f in _by_strength(model, facts)]
    implied = [(f, why) for f, why in implied if why and f.section in ("experience", "projects", "summary")]
    if implied:
        return ("direct", [f.id for f, _ in implied[:3]], implied[0][1])
    if named:
        if req.category == "certification":
            return ("direct", [f.id for f in named[:2]], f"{req.original} is listed on your resume.")
        return ("unclear", [f.id for f in named[:2]],
                f"{req.original} is listed in your {named[0].section}, but no job or project on the "
                f"resume shows you using it.")
    related = [(f, related_reason(f.text, term)) for f in _by_strength(model, facts)]
    related = [(f, r) for f, r in related if r]
    if related:
        f, (other, why) = related[0]
        return ("related", [f.id for f, _ in related[:2]],
                f"Your resume shows {other}, not {req.original}: {why}. Similar experience, "
                f"not hands-on {req.original}.")
    return None


def _verify(req: Requirement, model: ResumeModel, status: str, ids: list[str]) -> tuple[str, list[str], str]:
    """Hold a model's answer to the evidence it cites. Lowers, never raises."""
    ids = [i for i in ids if model.fact(i) is not None and model.fact(i).kind != "contact"]
    note = ""
    if status not in STATUSES:
        status = "unclear"
    if status in ("direct", "related") and not ids:
        return "not_found", [], " (No resume line was cited, so it is reported as not found.)"
    if status == "direct" and req.category in RULE_CATEGORIES:
        texts = [model.fact(i).text for i in ids]
        names = req.any_of or [req.normalized]          # any one of the alternatives will do
        if not any(mentions(t, n) or implied_by(t, n) for t in texts for n in names):
            if any(related_reason(t, n) for t in texts for n in names):
                status, note = "related", " (Lowered to related: the cited lines name a similar tool, not this one.)"
            else:
                status, note = "unclear", " (Lowered to unclear: the cited lines do not name it.)"
    return status, ids, note


def career_lines(model: ResumeModel) -> str:
    rows = []
    for e in model.entries:
        if e.section in ("experience", "projects"):
            rows.append(f"- {e.id}: {e.header} | from {e.start or '?'} to "
                        f"{'present' if e.present else (e.end or '?')}")
    return "\n".join(rows)


def match(reqs: list[Requirement], model: ResumeModel, cfg=None, use_model: bool = True) -> Report:
    notes: list[str] = []
    results: dict[str, Match] = {}
    open_reqs: list[Requirement] = []
    for req in reqs:
        ruled = rule_match(req, model)
        if ruled and ruled[0] == "direct":
            status, ids, why = ruled
            results[req.id] = Match(req.to_dict(), status, _evidence(model, ids), why, "rules")
        else:
            open_reqs.append(req)
            if ruled:
                status, ids, why = ruled
                results[req.id] = Match(req.to_dict(), status, _evidence(model, ids), why, "rules")

    alignment = ""
    if use_model and open_reqs:
        facts = [f for f in model.facts if f.kind != "contact"]
        prompt = ("JOB REQUIREMENTS:\n" + "\n".join(
                      f"- {r.id} [{r.priority}, {r.category}]: {r.original}  (from: {r.context[:160]})"
                      for r in open_reqs)
                  + "\n\nCANDIDATE JOBS AND DATES:\n" + career_lines(model)
                  + "\n\nRESUME FACTS:\n" + "\n".join(
                      f"{f.id} [{f.section}{' / ' + f.entry if f.entry else ''}]: {f.text}" for f in facts)
                  + "\n\nALREADY SHOWN BY THE RESUME (for the alignment summary):\n" + "\n".join(
                      f"- {m.requirement['original']}" for m in results.values() if m.status == "direct"))
        try:
            from ml.resume.llm import complete, extract_json
            reply = extract_json(complete(JUDGE, prompt, cfg, max_tokens=8000).text)
            # The model cites fact ids ("(f113, f153)"); the report shows the
            # evidence itself, so the ids are taken out of the prose.
            alignment = re.sub(r"\s*\((?:f\d+(?:,\s*)?)+\)", "",
                               str(reply.get("alignment") or "")).strip()
            judged = {str(j.get("id")): j for j in reply.get("matches") or [] if isinstance(j, dict)}
        except Exception as exc:
            notes.append(f"The model could not judge the remaining requirements "
                         f"({type(exc).__name__}); they were matched by rules only.")
            judged = {}
        for req in open_reqs:
            j = judged.get(req.id)
            if not j:
                continue
            status, ids, note = _verify(req, model, str(j.get("status") or "").lower(),
                                        [str(i) for i in j.get("evidence") or []])
            ruled = results.get(req.id)
            # Rules found something the model did not improve on: keep the rule's answer.
            if ruled and VALUE[ruled.status] >= VALUE[status]:
                continue
            results[req.id] = Match(req.to_dict(), status, _evidence(model, ids),
                                    str(j.get("explanation") or "").strip() + note,
                                    "rules+model" if ruled else "model")

    if use_model:
        _recheck_contradictions(reqs, results, model, cfg, notes)

    matches = []
    for req in reqs:
        m = results.get(req.id) or Match(req.to_dict(), "not_found", [],
                                         f"Nothing in your resume shows {req.original}.", "rules")
        m.weight = WEIGHT.get(req.priority, 3)
        m.value = VALUE[m.status]
        matches.append(m)
    total = sum(m.weight for m in matches)
    for m in matches:
        m.contribution = round(m.weight * m.value / total * 100, 1) if total else 0.0
    score = round(sum(m.weight * m.value for m in matches) / total * 100, 1) if total else 0.0
    return Report(matches, score, total, alignment, notes)


# An explanation that says the resume shows it, on a status that says it does not.
CONTRADICTS = re.compile(r"\b(explicitly|clearly|directly)\b.{0,40}\b(shows?|states?|establish\w*|names?|"
                         r"describes?|demonstrates?)\b|\bresume (shows|states|names)\b", re.I)


def _recheck_contradictions(reqs, results, model, cfg, notes) -> None:
    """Ask once more about items whose explanation contradicts their status.

    The model sometimes wrote "the resume explicitly establishes SLIs" under
    "unclear". Those items -- and only those -- go back with the
    contradiction pointed out; the answer is verified like the first, so it
    can raise a status only on evidence that names the requirement.
    """
    odd = [r for r in reqs if (m := results.get(r.id)) and m.method != "rules"
           and m.status in ("unclear", "related") and CONTRADICTS.search(m.explanation or "")]
    if not odd:
        return
    facts = [f for f in model.facts if f.kind != "contact"]
    prompt = ("These requirements were judged with an explanation that contradicts the status. "
              "Decide each again so the status matches the evidence.\n\n"
              + "\n".join(f"- {r.id}: {r.original} -- was \"{results[r.id].status}\" with: "
                          f"{results[r.id].explanation}" for r in odd)
              + "\n\nRESUME FACTS:\n" + "\n".join(f"{f.id} [{f.section}]: {f.text}" for f in facts))
    try:
        from ml.resume.llm import complete, extract_json
        reply = extract_json(complete(JUDGE, prompt, cfg, max_tokens=3000).text)
    except Exception:
        return
    for j in reply.get("matches") or []:
        req = next((r for r in odd if r.id == str(j.get("id"))), None)
        if req is None:
            continue
        status, ids, note = _verify(req, model, str(j.get("status") or "").lower(),
                                    [str(i) for i in j.get("evidence") or []])
        if status != results[req.id].status:
            results[req.id] = Match(req.to_dict(), status, _evidence(model, ids),
                                    str(j.get("explanation") or "").strip() + note
                                    + " (Re-checked: the first judgement contradicted its own explanation.)",
                                    "model")


def as_json(report: Report) -> str:
    return json.dumps(report.to_dict(), indent=1)
