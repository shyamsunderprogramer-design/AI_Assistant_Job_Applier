"""General questions: one answer from the person, used on every form that asks it.

Employers word the same question a dozen ways -- "Have you been employed by
Gemini in the past?", "Have you ever been employed by Sanmar or an
affiliate?", "SpaceX Employment History" -- and each comes with its own
choices ("No", "I have never worked for SpaceX..."). Here each wording is
recognised as one general question, the person answers it once, and the
answer is turned into whichever choice that form offers. When no choice
clearly says the same thing, nothing is chosen and the question is left
for the person.

One check is never skipped: "have you worked here before?" is answered "No"
only when the employer is not on the person's own resume. Someone who did
work there goes through the employer's rehire route, so the form is left
for them instead.

Answers live in backend/config/general_answers.yaml (gitignored).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

STORE = PROJECT_ROOT / "backend" / "config" / "general_answers.yaml"
YES_NO = ("Yes", "No")


@dataclass(frozen=True)
class Kind:
    id: str
    question: str                  # how the page asks it
    choices: tuple[str, ...]       # () = free text
    pattern: str                   # what a form's wording looks like
    exclude: str = ""              # wording that makes it a different question
    about_employer: bool = False   # "this company": checked against the resume


# Order matters: the first kind that matches wins.
KINDS = (
    Kind("referred", "Were you referred by a current employee?", YES_NO,
         r"\breferr(ed|al)\b", exclude=r"\bname\b|\bplease enter\b|\bwho\b"),
    Kind("family", "Do any family members or close relations work at the company you are applying to?", YES_NO,
         r"\bfamily\b|\brelatives?\b|close relationship", exclude=r"\bname\b|please list"),
    Kind("current_employee", "Are you a current employee of the company you are applying to?", YES_NO,
         r"\b(are you|you are) (a |currently |a current,? )*(current|currently)\b.*\b(employee|employed|intern)\b"
         r"|\bcurrently employed by\b", about_employer=True),
    Kind("worked_client", "Have you worked for one of the employer's clients, customers or partners?", YES_NO,
         r"\b(worked|work|employed)\b.*\b(client|customer|partner)s?\b"),
    Kind("worked_group", "Have you worked for a company in the employer's group (its subsidiaries or affiliates)?",
         YES_NO, r"\b(worked|employed|contracted)\b.*\b(subsidiar|affiliate|roper company)", about_employer=True),
    Kind("worked_here", "Have you ever worked for, contracted with or interviewed at the company you are applying to?",
         YES_NO,
         r"\b(have you|were you|did you)\b.*\b(employed|worked|contracted|interviewed)\b"
         r"|\bemployment history\b|\bformer\b.*\bemployee\b|\bpreviously (been )?employed\b",
         exclude=r"\bexplain\b|\bif yes\b|\bwhen\b.*\bcapacity\b", about_employer=True),
    Kind("age18", "Are you at least 18 years old?", YES_NO, r"\b18\b.*\b(age|old|older)\b|\bat least 18\b|\bover 18\b"),
    Kind("high_school", "Do you have a high school diploma or an equivalent such as the GED?", YES_NO,
         r"\bhigh school\b|\bged\b"),
    Kind("education", "What is the highest level of education you have completed?",
         ("High school", "Associate's degree", "Bachelor's degree", "Master's degree", "Doctorate"),
         r"\bhighest\b.*\b(education|degree)\b|\blevel of education\b"),
    Kind("employment_type", "What type of employment are you open to?",
         ("Full time (open to any type)", "Full time only", "Contract", "Part-time"),
         r"\btype of employment\b|\bemployment type\b|\btypes? of (work|position|role)s? (are you )?(open|interested)"),
    Kind("relocate", "Are you willing to relocate for a role?", YES_NO, r"\brelocat"),
    Kind("onsite", "Can you work on-site or hybrid at the employer's office when the role requires it?", YES_NO,
         r"\bon-?site\b|\bin[- ]office\b|\bhybrid\b|\bdays (a|per) week in\b"),
    Kind("start", "When can you start a new job?", (), r"\bstart date\b|\bavailable to start\b|\bearliest start\b|\bnotice period\b"),
    Kind("consent", "Do you accept employers' applicant privacy notices, data-processing consents and "
                    "background-check acknowledgements?",
         ("Yes, accept them", "No, ask me each time"),
         r"\bprivacy\b|\bdata protection\b|\bconsent\b|\backnowledg|\baffirmation\b|\bbackground check"),
)
BY_ID = {k.id: k for k in KINDS}


def kind_of(label: str) -> Kind | None:
    text = " ".join((label or "").lower().split())
    for k in KINDS:
        if re.search(k.pattern, text) and not (k.exclude and re.search(k.exclude, text)):
            return k
    return None


# -- the person's answers ----------------------------------------------------------

def load(path: Path | None = None) -> dict[str, str]:
    import yaml
    target = Path(path or STORE)
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}
    return {k: str(v) for k, v in data.items() if k in BY_ID and v not in (None, "")}


def save(kind_id: str, answer: str, path: Path | None = None) -> dict[str, str]:
    import yaml
    if kind_id not in BY_ID:
        raise ValueError(f"No such general question: {kind_id}")
    kind, answer = BY_ID[kind_id], (answer or "").strip()
    if kind.choices and answer and answer not in kind.choices:
        raise ValueError(f"Choose one of: {', '.join(kind.choices)}")
    data = load(path)
    if answer:
        data[kind_id] = answer
    else:
        data.pop(kind_id, None)
    target = Path(path or STORE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")
    return data


# -- the resume check --------------------------------------------------------------

_DROP = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "group", "the", "usa", "us", "ind",
         "india", "llp", "plc", "gmbh", "services", "technologies", "technology", "dept", "go", "ap"}


def _words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in _DROP and len(w) > 1]


@lru_cache(maxsize=1)
def past_employers() -> tuple[tuple[str, ...], ...]:
    """The employers on the person's own resume, as word lists."""
    try:
        from backend.config.loader import load_config
        from ml.resume.pipeline import base_resume_path
        from ml.tailoring.reader import read_document
        from ml.tailoring.structure import build
        model = build(read_document(base_resume_path(load_config())))
    except Exception:
        return ()
    return tuple(tuple(_words(e.employer)) for e in model.entries if e.section == "experience" and e.employer)


def worked_at(company: str, employers=None) -> bool:
    """Is this company one the person has worked for? First significant word must match."""
    words = _words(company)
    if not words:
        return False
    head = words[0]
    return any(head in e or (e and e[0] in words) for e in (past_employers() if employers is None else employers))


# -- turning a general answer into a form's answer --------------------------------------

def answer(label: str, company: str | None = None, saved: dict | None = None, employers=None) -> str | None:
    """The answer to this form question from the person's general answers, or None."""
    kind = kind_of(label)
    if kind is None:
        return None
    given = (load() if saved is None else saved).get(kind.id)
    if not given:
        return None
    if kind.about_employer and company and worked_at(company, employers):
        return None                          # they did work there: their rehire route, not this form
    if kind.id == "consent":
        return "Yes" if given.startswith("Yes") else None
    return given


_SAYS = {
    "Yes": (r"^yes\b", r"\bformer\b", r"\bi (have|am)\b(?!.*\bnever\b)"),
    "No": (r"^no\b", r"\bnever\b", r"\bnot\b"),
}


def pick_option(label: str, given: str, options: list[str]) -> int | None:
    """The one option that says what the general answer says, or None."""
    kind = kind_of(label)
    texts = [" ".join((o or "").lower().split()) for o in options]
    if kind is None or not texts:
        return None
    if kind.id == "consent":
        hits = [i for i, t in enumerate(texts) if re.search(r"\b(acknowledge|agree|accept|consent|yes|i have read)\b", t)
                and not re.search(r"\b(not|don't|do not|decline)\b", t)]
    elif given in _SAYS:
        hits = [i for i, t in enumerate(texts) if any(re.search(p, t) for p in _SAYS[given])]
        if given == "No":                    # "I have never worked at X" beats a later "not" elsewhere
            never = [i for i in hits if "never" in texts[i]]
            hits = never or hits
    elif kind.id == "employment_type" and given.startswith("Full time"):
        # Full time first; "open to any type" takes whatever the form offers when it has no full time.
        hits = [i for i, t in enumerate(texts) if re.search(r"\bfull[- ]?time\b|\bpermanent\b", t)][:1]
        if not hits and "any" in given:
            hits = [i for i, t in enumerate(texts) if t and not re.search(r"\bselect\b|\bchoose\b", t)][:1]
    else:
        want = given.lower().split("'")[0].split()[0]          # "master's degree" -> "master"
        hits = [i for i, t in enumerate(texts) if want in t]
    return hits[0] if len(hits) == 1 or (hits and given == "No" and "never" in texts[hits[0]]) else None
