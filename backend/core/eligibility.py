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
    r"\bcitizenship\s+(?:is\s+)?required\b", re.I)
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


def eligible(job, authorisation: dict) -> tuple[bool, list[str]]:
    """Can this person take this job, from what the posting states? (yes/no, reasons not)."""
    reasons = []
    level = getattr(job, "clearance", None)
    # An unanswered question never hides a job: only a stated answer can rule one out.
    if level and getattr(job, "clearance_active", None) and authorisation.get("security_clearance") is not None:
        need = LEVELS.index(level) + 1 if level in LEVELS else 1
        if held_level(authorisation.get("security_clearance")) < need:
            reasons.append(f"needs an active {level} clearance")
    citizen = authorisation.get("us_citizen")
    need_citizen = getattr(job, "citizenship", None)
    if need_citizen == "US citizen" and citizen is False:
        reasons.append("US citizens only")
    elif need_citizen == "US citizen or green card" and citizen is False \
            and not authorisation.get("green_card"):
        reasons.append("US citizens or green card holders only")
    if getattr(job, "sponsorship", None) == "no" and authorisation.get("requires_sponsorship") is True:
        reasons.append("will not sponsor a visa")
    return not reasons, reasons
