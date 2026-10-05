"""Cut a master resume down to the posting in front of it, and to a page limit.

The base resume is a MASTER resume: everything the person has done, far longer
than any one application should be (3,700 words — nine PDF pages). Tailoring
is SELECTION from it, and what to select depends on the job description, not
on a fixed rule about which sections to keep.

A model is asked to select, but a model does not reliably honour a length, so
the decision is made here, deterministically, from the posting:

  * Every candidate line — each experience bullet, each line of the expertise,
    highlights and skills sections — is scored by how much of THIS posting's
    vocabulary it carries (`scorer.jd_keyword_weights`), per word, so a short
    precise bullet beats a long vague one.
  * Recent work counts for more than old work; a line with a measured result
    counts for more than one without; a line the model rewrote for this
    posting counts for more; a bare list of tool names counts for less,
    because it repeats the skills and tells a reader nothing done.
  * The lowest-scoring lines go first until the rendered PDF fits the limit.

What is never cut: the header, the summary, education, and every role line —
employer, title and dates are the person's record. Each role keeps at least
`MIN_BULLETS_PER_ROLE` bullets, so no job reads as a gap.

Nothing is ever added or reworded here. Every removal is recorded in
`result.omitted` with its reason, so the review note shows exactly what was
left out and why.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

from ml.resume.scorer import content_terms, jd_keyword_weights
from ml.resume.writer import _is_role_line, _is_subheading, strip_bullet

log = logging.getLogger(__name__)

MIN_BULLETS_PER_ROLE = 2
# What a term is worth the second time a kept line shows it, relative to the
# first. Ranked alone, the fourth Jenkins bullet beat the only Splunk one.
REPEAT = 0.35
KEEP_SECTIONS = ("header", "summary", "objective", "profile", "education")
_METRIC = re.compile(r"\d+(\.\d+)?\s?%|\d+\+|\$\s?\d|\d{2,}")
TRIM_REASON = "left out to fit {pages} pages: less relevant to this posting than what was kept"


@dataclass
class Line:
    text: str               # verbatim, as in the base resume
    section: str
    role: int | None        # 0 = most recent role; None outside experience
    words: int
    score: float
    # A title over a group of lines ("Environment:", "Infrastructure as Code
    # (IaC) & Automation") carries its lines here. It is never scored: it stays
    # while any line under it stays, and goes with the last of them.
    children: list[str] | None = None


def _looks_like_title(line: str) -> bool:
    """Short, no full stop, no result: a label over lines, not a line of work.

    The parser's own subheading test misses titles with a slash or brackets
    ("Monitoring / Observability / Reliability Engineering"), and a missed
    title kept on its own prints as a bullet with nothing under it.
    """
    stripped = line.strip()
    body = strip_bullet(stripped).rstrip(":")
    return (len(body.split()) <= 8 and not stripped.endswith(".")
            and not _METRIC.search(body) and not _is_list(body))


def posting_terms(jd_text: str, requirements: str | None = None,
                  company: str | None = None) -> dict[str, float]:
    """Every term the posting uses, weighted — not only its top 60.

    Selection with the scorer's top-60 list cut ten things a SpaceX posting
    asked for and the master resume had (VMware vSphere, on-prem, Splunk,
    disaster recovery, capacity planning, ...): two-word skills were not in it
    at all, and single ones fell below the cut. Here every word and every
    two-word phrase of the posting counts, by how often it appears, with
    known skills weighted up as the scorer does.
    """
    import math
    from collections import Counter

    from ml.resume.scorer import SKILL_TERMS, content_terms

    text = f"{jd_text}\n{requirements or ''}"
    skip = set(content_terms(company or ""))
    words = [w for w in content_terms(text) if len(w) >= 3 and w not in skip]
    counts = Counter(words)
    counts.update(f"{a} {b}" for a, b in zip(words, words[1:]))
    weights = {term: 1 + math.log(n) for term, n in counts.items()}
    for term in list(weights):
        if term in SKILL_TERMS:
            weights[term] *= 2.5
        elif " " in term:
            weights[term] *= 1.5          # a phrase match is stronger evidence than a word
    # The scorer's own list keeps its extra weight on the terms it singles out.
    for term, w in jd_keyword_weights(jd_text, requirements, company, top_n=60).items():
        weights[term] = max(weights.get(term, 0.0), w)
    # The terms an ATS match is measured on (ml/resume/ats.py) count most:
    # the first line carrying each is worth keeping over any repeat, so the
    # two-page cut does not lower the match the tailoring was meant to raise
    # -- the Netflix resume came out 66% -> 63% before this.
    from ml.resume.ats import requirements_only
    top = max(weights.values() or [1.0])
    for term in jd_keyword_weights(requirements_only(jd_text),
                                   requirements_only(requirements) if requirements else None,
                                   company, top_n=40):
        weights[term] = max(weights.get(term, 0.0), top * 1.5)
    return weights


def _is_list(line: str) -> bool:
    return line.count("|") >= 5 or line.count(",") >= 8


def candidates(base, result, weights: dict[str, float]) -> list[Line]:
    rewrites = {(b.get("original") or "").strip(): (b.get("tailored") or "").strip()
                for b in result.bullets if b.get("original")}
    lines: list[Line] = []
    for section in base.sections:
        heading = section.heading.strip()
        if heading.lower().startswith(KEEP_SECTIONS) or heading == "HEADER":
            continue
        role = None
        roles_seen = 0
        title: Line | None = None
        for raw in section.lines:
            text = raw.strip()
            if not text:
                continue
            if _is_role_line(text):
                role = roles_seen
                roles_seen += 1
                title = None
                continue
            if _is_subheading(text):
                title = None
                continue
            body = strip_bullet(text)
            if _looks_like_title(text):
                title = Line(text, heading, role, len(body.split()), 0.0, children=[])
                lines.append(title)
                continue
            shown = rewrites.get(text) or rewrites.get(body) or body
            lines.append(Line(text, heading, role, len(shown.split()),
                              _score(shown, weights, role, rewritten=shown != body)))
            if title is not None:
                title.children.append(text)
    # A "title" with nothing under it was a short line of content after all.
    for line in lines:
        if line.children == []:
            line.children = None
            line.score = _score(strip_bullet(line.text), weights, line.role, rewritten=False)
    _by_coverage(lines, weights, rewrites)
    return lines


def _by_coverage(lines: list[Line], weights: dict[str, float], rewrites: dict[str, str]) -> None:
    """Re-score each line by what it adds to the lines ranked above it.

    Scored alone, every line about the posting's commonest skill outranks
    the one line about a skill it asks for once, and a two-page resume says
    "Jenkins" six times and "Splunk" never. Picked greedily instead, a line
    is worth what it adds -- the posting's range comes through first.
    Titles keep their own score; they go only with their lines.
    """
    pool = [l for l in lines if l.children is None]
    shown = {id(l): rewrites.get(l.text.strip()) or rewrites.get(strip_bullet(l.text.strip()))
             or strip_bullet(l.text) for l in pool}
    rewritten = {id(l): shown[id(l)] != strip_bullet(l.text) for l in pool}
    matched = {id(l): _matched(shown[id(l)], weights) for l in pool}
    covered: dict[str, int] = {}
    rank = len(pool)
    while pool:
        gains = [(_score(shown[id(l)], weights, l.role, rewritten=rewritten[id(l)],
                         covered={t: covered.get(t, 0) for t in matched[id(l)]}), l) for l in pool]
        gain, best = max(gains, key=lambda g: g[0])
        # Order is what the trim reads; the rank keeps it strict even when
        # two lines add the same, and the gain keeps the floor meaningful.
        best.score = gain + rank * 1e-6
        rank -= 1
        pool.remove(best)
        for term in matched[id(best)]:
            covered[term] = covered.get(term, 0) + 1


def _matched(text: str, weights: dict[str, float]) -> set[str]:
    """The posting's terms this line carries."""
    terms = set(content_terms(text))
    lowered = text.lower()
    return {t for t in weights if (t in terms if " " not in t else t in lowered)}


def _score(text: str, weights: dict[str, float], role: int | None, *, rewritten: bool,
           covered: dict[str, int] | None = None) -> float:
    """How much of the posting this line carries, per word.

    `covered` counts how many better lines already carry each term; each
    repeat is worth `REPEAT` of the one before, so a skill already shown
    adds little and one not yet shown adds its full weight.
    """
    covered = covered or {}
    hits = sum(weights[t] * REPEAT ** covered.get(t, 0) for t in _matched(text, weights))
    # The small constant lets the other signals rank lines this posting does
    # not mention at all: when a role must keep two lines, a measured result
    # beats a chore. It is well below one keyword's weight, so any real match
    # still wins.
    score = (hits + 0.25) / math.sqrt(max(len(text.split()), 6))
    if _METRIC.search(text):
        score *= 1.25
    if rewritten:
        score *= 1.3
    if _is_list(text):
        score *= 0.6
    if role is not None:
        score *= (1.5, 1.2, 1.0)[role] if role < 3 else 0.8
    return score


def _key(text: str) -> str:
    """A line's identity across readers: bullets, case and spacing set aside."""
    return " ".join((text or "").lower().lstrip("-•*·▪◦–— ").split())[:80]


def _protected(line: Line, dropped: set[str], lines: list[Line]) -> bool:
    """Would dropping this take a role below its minimum?"""
    if line.role is None or line.children is not None:
        return False
    kept = [l for l in lines if l.role == line.role and l.children is None
            and not _is_list(l.text) and l.text not in dropped]
    return line.text in {l.text for l in kept} and len(kept) <= MIN_BULLETS_PER_ROLE


def _with_labels(dropped: set[str], lines: list[Line]) -> set[str]:
    """A title goes when every line under it has gone."""
    return dropped | {l.text for l in lines
                      if l.children and all(c in dropped for c in l.children)}


def count_pages(base, result) -> int:
    """Pages in the PDF this result renders to — measured, not estimated."""
    import tempfile
    from pathlib import Path

    from pypdf import PdfReader

    from ml.resume.pdf import write_pdf

    # A measurement is not an output: keep the writer's "Wrote ..." line out
    # of the log, where it filled the apply run's progress box.
    pdf_log = logging.getLogger("ml.resume.pdf")
    level = pdf_log.level
    pdf_log.setLevel(logging.WARNING)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "measure.pdf"
            write_pdf(base, result, path)
            return len(PdfReader(str(path)).pages)
    finally:
        pdf_log.setLevel(level)


def fit_to_pages(base, result, jd_text: str, *, requirements: str | None = None,
                 company: str | None = None, max_pages: int = 2,
                 pages=count_pages, focus_terms: list[str] | None = None,
                 keep: set[str] | None = None) -> int:
    """Trim `result` in place so the rendered resume fits `max_pages`.

    Returns the page count it ends at (it may exceed the limit if the
    protected lines alone do not fit). `pages` is injectable for tests.
    `keep` holds lines that prove one of the posting's requirements: the
    model's own omissions never take them, and the page trim takes them last.
    Without it a tailored resume showed fewer requirements than the original
    (25% -> 21%): lines were dropped for space that were the evidence.
    """
    if max_pages <= 0:
        return pages(base, result)
    weights = posting_terms(jd_text or "", requirements, company)
    # Terms "Align closer" asked for outweigh everything else, so the line
    # that shows each one is among the last to be cut.
    for term in focus_terms or []:
        weights[term.lower()] = max(weights.values() or [1.0]) * 3
    lines = candidates(base, result, weights)
    by_text = {l.text: l for l in lines}
    keys = {_key(k) for k in keep or ()}
    evidence = {l.text for l in lines if _key(l.text) in keys}

    # The model's own omissions go first, as far as the per-role floor allows.
    asked = {(e.get("original") or "").strip() for e in result.omitted if isinstance(e, dict)}
    dropped: set[str] = set()
    for line in sorted((by_text[t] for t in asked
                        if t in by_text and by_text[t].children is None and t not in evidence),
                       key=lambda l: l.score):
        if not _protected(line, dropped, lines):
            dropped.add(line.text)

    reasons = {(e.get("original") or "").strip(): e.get("reason", "")
               for e in result.omitted if isinstance(e, dict)}

    def apply() -> None:
        final = _with_labels(dropped, lines)
        result.omitted = [{"original": t,
                           "reason": reasons.get(t) or TRIM_REASON.format(pages=max_pages)}
                          for t in sorted(final, key=lambda t: by_text[t].score if t in by_text else 0)]
        result.fitted = True

    apply()
    current = pages(base, result)
    if current <= max_pages:
        return current

    # Estimate how many words to lose from the first render, then confirm by
    # rendering: a line's cost in pages depends on how it wraps.
    total = sum(l.words for l in lines if l.text not in dropped) or 1
    budget = total * max_pages / current * 0.97
    queue = sorted((l for l in lines if l.text not in dropped and l.children is None),
                   key=lambda l: (l.text in evidence, l.score))
    kept_words = total
    for line in queue:
        if kept_words <= budget:
            break
        if _protected(line, dropped, lines):
            continue
        dropped.add(line.text)
        kept_words -= line.words
    apply()
    current = pages(base, result)

    remaining = [l for l in queue if l.text not in dropped]
    while current > max_pages and remaining:
        step = 0
        for line in list(remaining):
            remaining.remove(line)
            if _protected(line, dropped, lines):
                continue
            dropped.add(line.text)
            step += 1
            if step == 3:
                break
        if step == 0:
            break
        apply()
        current = pages(base, result)
    if current > max_pages:
        log.warning("Resume still %d pages after trimming everything allowed", current)
        return current

    # The word estimate overshoots, leaving the last page part-empty. Give the
    # space back to the most relevant lines that were cut, one at a time, as
    # long as the page count holds. Never lines the model itself left out.
    misses = 0
    floor = 0.25 / math.sqrt(6) * 1.5 * 1.25 * 1.3   # the most a no-match line can score
    for line in sorted((by_text[t] for t in list(dropped)
                        if t in by_text and t not in asked and by_text[t].children is None
                        and by_text[t].score > floor),
                       key=lambda l: -l.score):
        dropped.discard(line.text)
        apply()
        if pages(base, result) > max_pages:
            dropped.add(line.text)
            apply()
            misses += 1
            if misses >= 4:
                break
    return pages(base, result)
