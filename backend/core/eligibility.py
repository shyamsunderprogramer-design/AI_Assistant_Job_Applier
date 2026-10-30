"""Who can take a job: security clearance, citizenship, visa sponsorship.

Read from the posting's own words, conservatively -- a stated requirement,
never a guess from the employer or the sector:

  clearance     Public Trust | Secret | Top Secret | TS/SCI, and whether it
                must already be held ("active") or can be obtained after hire
  polygraph     a polygraph is part of the clearance
  citizenship   "US citizen" | "US citizen or green card"
  sponsorship   "no" (will not sponsor) | "yes" (sponsorship available)

Where a posting says nothing about sponsorship, the company's own record in
the official registers (data/companies.db, H-1B for the US) shows whether it
has sponsored before. `eligible()` compares a job with the person's profile.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache

from backend.config.loader import PROJECT_ROOT

LEVELS = ("Public Trust", "Secret", "Top Secret", "TS/SCI")

# TS/SCI as postings write it: TS/SCI, TS-SCI, TS.SCI, TS SCI, TS//SCI.
_TS_SCI = re.compile(r"\bts\s*(?:/{1,2}|-|\.|\s)\s*sci\b|top\s*secret\s*/\s*sci|top\s+secret\s*\(\s*sci|"
                     r"top\s+secret\s+(?:with|w/|and|plus)\s+sci\b", re.I)
_TOP_SECRET = re.compile(r"\btop[\s-]+secret\b|\bTS\b(?=[^.\n]{0,25}\bclearance\b)|\bclearance\s+type\s*:\s*(?:ts|top)\b", re.I)
# "Secret" alone means Vault secrets as often as a clearance: only in clearance words.
_SECRET = re.compile(r"\bclearance\s+type\s*:\s*secret\b|\bsecret\s+(?:security\s+)?clearance|\bclearance\s*(?:level)?\s*(?::|of|at)?\s*secret\b"
                     r"|\b(?:active|current|interim)\s+secret\b|\bsecret\s+level\b", re.I)
_PUBLIC_TRUST = re.compile(r"\bpublic\s+trust\b", re.I)
_ANY_CLEARANCE = re.compile(r"\b(?:security\s+clearance|clearance\s+(?:is\s+)?required|"
                            r"(?:active|current)\s+(?:security\s+)?clearance|dod\s+clearance)\b", re.I)
_OBTAIN = re.compile(r"\b(?:ability|able|eligib\w*|willing\w*)\s+(?:and\s+eligibility\s+)?to\s+(?:obtain|be\s+granted|receive)"
                     r"(?![^.\n]{0,40}\bpoly)|\bobtain\s+and\s+maintain\b|\beligib\w+\s+(?:for|to\s+hold)\b[^.\n]{0,40}\bclearance|"
                     r"\bmust\s+be\s+(?:able\s+to\s+)?obtain\b|\bclearable\b", re.I)
# A field-style statement ("Clearance: TS/SCI", "CLEARANCE TS/SCI Full Poly") is a requirement.
_FIELD = re.compile(r"\b(?:clearance\s*(?:level)?|required)\s*:?\s*-?\s*(?:must\s+have\s+)?(?:an?\s+)?(?:active\s+)?"
                    r"(?:ts\s*(?:/{1,2}|-|\.|\s)\s*sci|top\s+secret|secret)\b", re.I)
_CLEARANCE_WORDS = re.compile(r"clearance|\bsecret\b|\bts\b|\bsci\b|public\s+trust", re.I)
_ACTIVE = re.compile(r"\b(?:active|current|existing|in[\s-]place)\b[^\n]{0,40}?\b(?:clearance|secret|ts|trust)\b|"
                     r"\b(?:must|required\s+to)\s+(?:possess|hold|have)\b[^.\n]{0,40}\bclearance\b|"
                     r"\bclearance\s+required\b|"
                     r"\b(?:ts\s*(?:/{1,2}|-|\.|\s)\s*sci|top\s+secret|secret\s+clearance|public\s+trust)\b[^.\n]{0,50}\brequired\b", re.I)
_POLY = re.compile(r"\b(?:full[\s-]scope|ci|counter[\s-]?intelligence|lifestyle)\s+poly(?:graph)?\b|\bpolygraph\b|"
                   r"\bfsp\b|\bw/?\s*poly\b|\bwith\s+(?:a\s+)?poly\b", re.I)

_CITIZEN = re.compile(
    r"\b(?:u\.?\s?s\.?|united\s+states)\s+citizen(?:ship)?\b[^.\n]{0,40}\b(?:required|only|is\s+a\s+must)\b|"
    r"\bmust\s+(?:be|hold)\s+(?:a\s+)?(?:u\.?\s?s\.?|united\s+states)\s+citizen|"
    r"\b(?:u\.?\s?s\.?|united\s+states)\s+citizens?\s+only\b|"
    r"\brequires?\s+(?:u\.?\s?s\.?|united\s+states)\s+citizenship\b|"
    r"\bcitizenship\s+(?:is\s+)?required\b|"
    # USAJOBS' own field: "Who may apply: United States Citizens" (federal civil service).
    r"\bwho\s+may\s+apply:\s*(?:u\.?\s?s\.?|united\s+states)\s+citizens\b", re.I)
_GREEN_CARD = re.compile(r"\bgreen\s+card\b|\bpermanent\s+resident", re.I)

_NO_SPONSOR = re.compile(
    r"\b(?:not|unable\s+to|cannot|can\s*not|can't|won't|will\s+not|does\s+not|do\s+not|is\s+not\s+able\s+to)\s+"
    r"(?:currently\s+)?(?:be\s+able\s+to\s+)?(?:provide\s+|offer\s+|support\s+)?(?:any\s+)?(?:employment\s+)?"
    r"(?:visa\s+|h-?1b\s+|work\s+(?:visa|authori[sz]ation)\s+)?sponsor|"
    r"\bsponsorship\s+(?:is\s+)?(?:not\s+(?:available|offered|provided|possible)|unavailable)\b|"
    r"\bno\s+(?:visa\s+|h-?1b\s+)?sponsorship\b|"
    r"\bwithout\s+(?:the\s+need\s+for\s+|requiring\s+|needing\s+)?(?:current\s+or\s+future\s+|now\s+or\s+in\s+the\s+future\s+)?"
    r"(?:visa\s+|employer\s+|employment\s+)?sponsorship\b", re.I)
_YES_SPONSOR = re.compile(
    r"\b(?:visa\s+|h-?1b\s+)?sponsorship\s+(?:is\s+)?(?:available|offered|provided|possible)\b|"
    r"\bwill\s+(?:consider\s+)?sponsor\b|\bwe\s+(?:can\s+|do\s+)?sponsor\b|"
    r"\bh-?1b\s+(?:transfer|sponsorship)\s+(?:is\s+)?(?:available|welcome|supported)\b|"
    r"\bopen\s+to\s+sponsor", re.I)


# A clearance one may obtain after hire, or that is only preferred, is not a bar.
_NOT_A_BAR = re.compile(r"\brequired\s+for\s+start\s*:\s*no\b|\b(?:nice\s+to\s+have|preferred|a\s+plus|bonus|"
                        r"desired|is\s+a\s+plus)\b[^.\n]{0,80}\bclearance\b|\bclearance\b(?:(?!poly)[^.\n]){0,60}\b(?:preferred|"
                        r"a\s+plus|nice\s+to\s+have|desired)\b|\bor\s+(?:the\s+)?(?:willingness|ability|eligibility)\b"
                        r"(?![^.\n]{0,40}\bpoly)", re.I)
_PREFERRED_SECTION = re.compile(r"(?:nice\s+to\s+have|preferred\s+(?:qualifications|skills)|bonus\s+points|"
                                r"would\s+be\s+nice)[^\n]*\n?", re.I)


def clearance(text: str) -> tuple[str | None, bool]:
    """(level, must already be held). (None, False) when no clearance is asked for.

    Lenient on purpose: a posting that lets the clearance be obtained, or only
    prefers it, is "obtainable" -- hiding a job wrongly costs more than showing one.
    """
    text = text or ""
    for level, pattern in (("TS/SCI", _TS_SCI), ("Top Secret", _TOP_SECRET), ("Secret", _SECRET),
                           ("Public Trust", _PUBLIC_TRUST)):
        if pattern.search(text):
            break
    else:
        if not _ANY_CLEARANCE.search(text):
            return None, False
        level = "Clearance"
    # Judged sentence by sentence: "ability to obtain" elsewhere in a posting
    # (about a polygraph, or another role) does not soften "must have an active TS/SCI".
    active = False
    for sentence in re.split(r"(?<=[.;!?])\s+|\n+|\s+[-•]\s+", text):
        if not _CLEARANCE_WORDS.search(sentence):
            continue
        if _OBTAIN.search(sentence) or _NOT_A_BAR.search(sentence):
            continue
        if _ACTIVE.search(sentence) or _FIELD.search(sentence):
            active = True
            break
    # A clearance listed only under "nice to have" is not a bar.
    if active and _PREFERRED_SECTION.search(text):
        section = text[_PREFERRED_SECTION.search(text).start():][:600]
        if _CLEARANCE_WORDS.search(section) and not any(
                (_ACTIVE.search(s) or _FIELD.search(s)) and not _OBTAIN.search(s)
                for s in re.split(r"(?<=[.;!?])\s+|\n+", text[:_PREFERRED_SECTION.search(text).start()])
                if _CLEARANCE_WORDS.search(s)):
            active = False
    return level, active


def citizenship(text: str) -> str | None:
    m = _CITIZEN.search(text or "")
    if not m:
        return None
    window = (text or "")[max(0, m.start() - 120): m.end() + 120]
    return "US citizen or green card" if _GREEN_CARD.search(window) else "US citizen"


_EXPORT = re.compile(r"\bsponsorship\s+for\s+an?\s+export\s+licen[cs]e\b", re.I)


def sponsorship(text: str) -> str | None:
    text = _EXPORT.sub(" ", text or "")          # export-control wording, not a visa
    if _NO_SPONSOR.search(text):
        return "no"
    if _YES_SPONSOR.search(text or ""):
        return "yes"
    return None


def parse(title: str | None, description: str | None, requirements: str | None) -> dict:
    """The job's columns: clearance, clearance_active, polygraph, citizenship, sponsorship."""
    text = "\n".join(p for p in (title, requirements, description) if p)
    level, active = clearance(text)
    return {"clearance": level, "clearance_active": active if level else None,
            "polygraph": bool(level and _POLY.search(text)) or None,
            "citizenship": citizenship(text), "sponsorship": sponsorship(text)}


# -- the company's own record ---------------------------------------------------------

@lru_cache(maxsize=4096)
def sponsor_history(company: str, country: str = "US") -> str | None:
    """'H-1B' when the official register shows this company has sponsored, else None."""
    if not company:
        return None
    try:
        from data_engineering.companies.db import normalize_name
        name = normalize_name(company)
        with sqlite3.connect(f"file:{PROJECT_ROOT / 'data' / 'companies.db'}?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT s.program FROM sponsorship s JOIN companies c ON c.id = s.company_id "
                "WHERE c.name_normalized = ? AND s.country = ? AND s.status = 'active' LIMIT 1",
                (name, country)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


# -- the person ----------------------------------------------------------------------

def held_level(stated: str | None) -> int:
    """How high a clearance the person holds: 0 none, 1 Public Trust ... 4 TS/SCI."""
    text = (stated or "").lower()
    if not text or text in ("none", "no", "n/a"):
        return 0
    if "sci" in text:
        return 4
    if "top" in text:
        return 3
    if "secret" in text:
        return 2
    if "trust" in text:
        return 1
    return 0


BLOCKS, UNKNOWN, WARNING = "blocks", "unknown", "warning"
_ANSWER_IT = "answer it in Settings → Your details"


def assess(job, authorisation: dict) -> list[tuple[str, str, str]]:
    """What the posting asks of the person, fact by fact, against what their profile states, as
    (fact, finding, words). It blocks only on a stated answer; a question the profile leaves
    unanswered is unknown -- never taken for a yes or a no; and what the person should know
    before applying is a warning. Reads the job's columns only: no text, no database."""
    out = []
    level = getattr(job, "clearance", None)
    named = "security" if level == "Clearance" else level
    held = authorisation.get("security_clearance")
    citizen = authorisation.get("us_citizen")
    if level and getattr(job, "clearance_active", None):
        # An unanswered question never hides a job: only a stated answer can rule one out...
        if held is not None:
            need = LEVELS.index(level) + 1 if level in LEVELS else 1
            if held_level(held) < need:
                out.append(("clearance", BLOCKS, f"needs an active {named} clearance"))
        # ...and "not a US citizen" is one: a US clearance is granted to US citizens only. Left
        # unanswered, it let a Secret-cleared role on Fort Meade through to an application.
        elif citizen is False:
            out.append(("clearance", BLOCKS, f"needs an active {named} clearance, which only US citizens hold"))
        else:
            out.append(("clearance", UNKNOWN, f"needs an active {named} clearance, and your profile doesn't "
                                              f"say whether you hold one: {_ANSWER_IT}"))
    elif level:
        out.append(("clearance", WARNING, f"mentions a {named} clearance, to be obtained after hire or only "
                                          "preferred" + (": US clearances go to US citizens" if citizen is False else "")))
    if getattr(job, "polygraph", None) and "poly" not in str(held or "").lower():
        out.append(("polygraph", WARNING, "a polygraph is part of its clearance"))
    need_citizen = getattr(job, "citizenship", None)
    if need_citizen == "US citizen":
        if citizen is False:
            out.append(("citizenship", BLOCKS, "US citizens only"))
        elif citizen is None:
            out.append(("citizenship", UNKNOWN, "US citizens only, and your profile doesn't say whether you "
                                                f"are one: {_ANSWER_IT}"))
    elif need_citizen == "US citizen or green card" and citizen is not True and authorisation.get("green_card") is not True:
        if citizen is False and authorisation.get("green_card") is False:
            out.append(("citizenship", BLOCKS, "US citizens or green card holders only"))
        else:
            out.append(("citizenship", UNKNOWN, "US citizens or green card holders only, and your profile "
                                                f"doesn't say whether you are either: {_ANSWER_IT}"))
    needs = authorisation.get("requires_sponsorship")
    stated = getattr(job, "sponsorship", None)
    if stated == "no":
        if needs is True:
            out.append(("sponsorship", BLOCKS, "will not sponsor a visa"))
        elif needs is None:
            out.append(("sponsorship", UNKNOWN, "will not sponsor a visa, and your profile doesn't say whether "
                                                f"you need sponsorship: {_ANSWER_IT}"))
    elif stated is None and needs is True:
        out.append(("sponsorship", WARNING, "doesn't say whether it sponsors a visa, and you need one"))
    return out


def eligible(job, authorisation: dict) -> tuple[bool, list[str]]:
    """Can this person take this job, from what the posting states? (yes/no, reasons not)."""
    reasons = [words for _, finding, words in assess(job, authorisation) if finding == BLOCKS]
    return not reasons, reasons


# -- a decision for one job (26 Oct 2026, Auto Apply v3.0 phase 3) ----------------------------------

ELIGIBLE, WITH_WARNINGS, NEEDS_INFORMATION = "ELIGIBLE", "ELIGIBLE_WITH_WARNINGS", "NEEDS_INFORMATION"
INELIGIBLE, UNVERIFIED = "INELIGIBLE", "UNVERIFIED"
DECISION_LABELS = {ELIGIBLE: "Eligible", WITH_WARNINGS: "Eligible, with warnings",
                   NEEDS_INFORMATION: "Needs your answer", INELIGIBLE: "Not eligible", UNVERIFIED: "Unverified"}
# Changed whenever a rule below changes what a decision says: a decision kept from before says so.
RULE_VERSION = "2026-10-26.3"       # .3: an unanswered green-card question remains unknown

_FACTS = {"clearance": "Clearance", "polygraph": "Polygraph", "citizenship": "Citizenship",
          "sponsorship": "Sponsorship"}
_SENTENCE = re.compile(r"(?<=[.;!?])\s+|\n+|\s+[-•]\s+")


def _said(text: str, *patterns) -> str | None:
    """The posting's own sentence that states it, cut short if long; None when none does."""
    for sentence in _SENTENCE.split(text or ""):
        if any(p.search(sentence) for p in patterns):
            words = " ".join(sentence.split())
            return words if len(words) <= 240 else words[:237].rstrip() + "…"
    return None


def evidence(text: str, fact: str) -> str | None:
    """Where the posting says what this fact stands on, in its own words."""
    if fact == "clearance":
        # The sentence that makes it a requirement first, as clearance() reads it; else any that names it.
        for sentence in _SENTENCE.split(text or ""):
            if _CLEARANCE_WORDS.search(sentence) and (_ACTIVE.search(sentence) or _FIELD.search(sentence)) \
                    and not (_OBTAIN.search(sentence) or _NOT_A_BAR.search(sentence)):
                return _said(sentence, _CLEARANCE_WORDS)
        return _said(text, _TS_SCI, _TOP_SECRET, _SECRET, _PUBLIC_TRUST, _ANY_CLEARANCE)
    if fact == "polygraph":
        return _said(text, _POLY)
    if fact == "citizenship":
        return _said(text, _CITIZEN)
    if fact == "sponsorship":
        return _said(_EXPORT.sub(" ", text or ""), _NO_SPONSOR, _YES_SPONSOR)
    return None


@dataclass
class Decision:
    """Whether the person can apply to a job, and how, from what is known now. An eligibility result,
    not a state of the queue: ELIGIBLE may be sent by the app itself where the person has said so;
    with warnings, or UNVERIFIED, it goes only with the person pressing Submit; NEEDS_INFORMATION
    waits for their answer; INELIGIBLE does not go. Every list is in plain words:

      reasons                why this decision, the gist of the lists below
      evidence               the posting's own words for each requirement it states
      unknowns               what could not be told, and how to settle it
      blocking_requirements  what rules it out: a requirement the posting states against the
                             person's stated answer, the posting's own state, or their own rule
      warnings               what the person should know before it goes"""

    job_id: int
    decision: str
    reasons: list[str]
    evidence: list[str]
    unknowns: list[str]
    blocking_requirements: list[str]
    warnings: list[str]
    evaluated_at: str
    rule_version: str = RULE_VERSION

    @property
    def label(self) -> str:
        return DECISION_LABELS[self.decision]

    @property
    def goes(self) -> bool:
        """It may go on a run (with the person's Submit unless sends_itself)."""
        return self.decision in (ELIGIBLE, WITH_WARNINGS, UNVERIFIED)

    @property
    def sends_itself(self) -> bool:
        """Nothing is in doubt: the app may press Submit, where the person has said it may."""
        return self.decision == ELIGIBLE

    def as_dict(self) -> dict:
        return {**asdict(self), "label": self.label}


def _stale(job, text: str) -> list[str]:
    """Requirements the posting's words state that the job's saved details leave out: saved before
    the posting was read for them, or by an older reading. Never a guess either way -- the job is
    unverified until they agree (its page puts them right)."""
    if getattr(job, "eligibility_manual", None):
        return []                                       # the person's own correction stands
    read = parse(None, text, None)
    missing = []
    if read["clearance"] and not getattr(job, "clearance", None):
        missing.append(f"a {read['clearance']} clearance")
    elif read["clearance_active"] and not getattr(job, "clearance_active", None):
        missing.append(f"an active {read['clearance']} clearance, held already")
    if read["citizenship"] and not getattr(job, "citizenship", None):
        missing.append(read["citizenship"] + " only")
    if read["sponsorship"] == "no" and getattr(job, "sponsorship", None) != "no":
        missing.append("no visa sponsorship")
    return [f"its posting states {what}, which the job's saved details leave out: check them on the job's page"
            for what in missing]


def decide(job, authorisation: dict, *, ruled_out=(), now: datetime | None = None) -> Decision:
    """The decision for one job: its posting's stated requirements (assess) against the person's
    answers, with the posting's own words for each, and `ruled_out` -- what else rules it out, from
    the run's own rules (runner.blocked_reasons: closed, marked, no link, abroad, too old)."""
    text = "\n".join(p for p in (getattr(job, "title", None), getattr(job, "requirements", None),
                                  getattr(job, "description", None)) if p)
    found = assess(job, authorisation)
    blocking = [f"{words} (the employer's requirement, against your profile)" for _, f, words in found if f == BLOCKS]
    blocking += list(ruled_out)
    asked = [words for _, f, words in found if f == UNKNOWN]
    warnings = [words for _, f, words in found if f == WARNING]
    unread = []
    has_text = bool((getattr(job, "description", None) or "").strip() or (getattr(job, "requirements", None) or "").strip())
    if not has_text and not getattr(job, "eligibility_manual", None):
        unread.append("the posting's words aren't saved, so whether it asks for a clearance, citizenship or "
                      "a visa couldn't be read: check the posting")
    elif has_text:
        unread += _stale(job, text)
    if any(fact == "sponsorship" and f == WARNING for fact, f, _ in found):
        program = sponsor_history(getattr(job, "company", "") or "")
        if program:                                     # a reasonable inference, never proof
            warnings.append(f"{job.company} has sponsored {program} visas before (official records); "
                            "that is no promise this role will")
    shown = []                                          # every requirement it states, met or not
    for fact in (f for f in _FACTS if getattr(job, f, None)):
        if getattr(job, "eligibility_manual", None):
            shown.append(f"{_FACTS[fact]}: as you set it on the job's page")
        else:
            words = evidence(text, fact)
            if words:
                shown.append(f"{_FACTS[fact]}: “{words}”")
    if blocking:
        decision, reasons = INELIGIBLE, blocking
    elif asked:
        decision, reasons = NEEDS_INFORMATION, asked
    elif unread:
        decision, reasons = UNVERIFIED, unread + warnings
    elif warnings:
        decision, reasons = WITH_WARNINGS, warnings
    else:
        decision = ELIGIBLE
        # Only who may take the job is decided here. "Nothing in the posting rules you out" (until 26 Oct
        # 2026) read as if its skills and years had been weighed too; they never are.
        reasons = ["no clearance, citizenship or visa requirement in the posting stands against your profile; "
                   "its skills, experience and qualifications are not part of this check"]
    return Decision(job_id=getattr(job, "id", 0) or 0, decision=decision, reasons=reasons, evidence=shown,
                    unknowns=asked + unread, blocking_requirements=blocking, warnings=warnings,
                    evaluated_at=(now or datetime.now(timezone.utc)).isoformat(timespec="seconds"))
