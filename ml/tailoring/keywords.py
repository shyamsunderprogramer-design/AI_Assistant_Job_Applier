"""The posting's keywords, one by one: where the resume shows each, and what an edit would cost.

The ATS score is one number. Behind it is a list of words the posting weighs,
and the person needs that list to act on: which keywords the tailored resume
shows and on which lines, which ones the master had but tailoring cut (put
back with one press), and which ones the person has never claimed (a gap,
never written in for them).

The same tokens and phrases as the score (ml/resume/scorer.py), so the
tracker and the number never disagree about whether a word is there.
"""

from __future__ import annotations

import re

SHOWN, LOST, MISSING = "shown", "lost", "missing"


def _has(text: str) -> set[str]:
    from ml.resume.scorer import _phrases_present, content_terms
    return set(content_terms(text)) | _phrases_present(text)


def _lines(text: str) -> list[str]:
    return [l.strip() for l in (text or "").splitlines() if len(l.strip()) > 2]


def _mentions(term: str, jd_text: str) -> int:
    return len(re.findall(rf"(?<![a-z0-9]){re.escape(term)}", (jd_text or "").lower()))


def weights(jd_text: str, company: str | None = None) -> dict[str, float]:
    """The posting's scored terms and their weights, without the ones no ATS counts."""
    from ml.resume.ats import _noise, requirements_only
    from ml.resume.scorer import jd_keyword_weights
    jd = requirements_only(jd_text)
    return {t: w for t, w in jd_keyword_weights(jd, company=company, top_n=40).items()
            if not _noise(t, jd, company)}


def track(master_text: str, tailored_text: str, jd_text: str, company: str | None = None) -> dict:
    """Every scored keyword: shown (and where), lost in tailoring (and the master's
    lines that had it), or missing from the person's resume altogether."""
    from ml.resume.emphasis import GENERIC, _named
    terms = weights(jd_text, company)
    master_has, tailored_has = _has(master_text), _has(tailored_text)
    tailored_lines, master_lines = _lines(tailored_text), _lines(master_text)
    rows = []
    for term, weight in terms.items():
        status = SHOWN if term in tailored_has else LOST if term in master_has else MISSING
        rows.append({
            "term": term,
            "weight": round(weight, 3),
            "mentions": _mentions(term, jd_text),
            # A tool or named skill, by the rule the bold keywords use: not "management".
            "tool": term not in GENERIC and (" " in term or _named(term, master_text)),
            "status": status,
            "where": [l for l in tailored_lines if term in _has(l)][:4] if status == SHOWN else [],
            "master_lines": [l for l in master_lines if term in _has(l)][:3] if status == LOST else [],
        })
    order = {SHOWN: 1, LOST: 0, MISSING: 2}
    rows.sort(key=lambda r: (order[r["status"]], -r["weight"]))
    total = sum(terms.values()) or 1
    return {
        "keywords": rows,
        "counts": {s: sum(1 for r in rows if r["status"] == s) for s in (SHOWN, LOST, MISSING)},
        "coverage": round(100 * sum(terms[r["term"]] for r in rows if r["status"] == SHOWN) / total),
    }


def check_edits(tailored_text: str, edits: dict[str, str], jd_text: str,
                company: str | None = None) -> dict:
    """What these edits would do to the keywords, before they are saved.

    `edits` maps a line's current text to its new text ("" removes it). A
    keyword is lost when the edited lines held it and no line left does --
    a keyword that is still on another line is not lost, however many times
    it is edited out.
    """
    terms = weights(jd_text, company)
    # An edited line may not match a whole extracted line (a wrapped bullet):
    # apply the edit inside the text too.
    text_after = tailored_text
    for before, new in edits.items():
        text_after = text_after.replace(before, new)
    had, has = _has(tailored_text), _has(text_after)
    lost = [t for t in terms if t in had and t not in has]
    gained = [t for t in terms if t not in had and t in has]
    by_line = {}
    for before, new in edits.items():
        removed = [t for t in lost if t in _has(before) and t not in _has(new)]
        if removed:
            by_line[before] = removed
    total = sum(terms.values()) or 1
    coverage = lambda present: round(100 * sum(w for t, w in terms.items() if t in present) / total)
    return {"lost": lost, "gained": gained, "by_line": by_line,
            "coverage_before": coverage(had), "coverage_after": coverage(has)}
