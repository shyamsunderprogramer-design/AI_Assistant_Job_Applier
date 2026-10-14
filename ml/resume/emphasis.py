"""Which of the posting's keywords to set in bold, and where.

The person's format asks for selected key phrases in bold in the summary and
the bullets. Selected, not every match: a recruiter's eye should land on the
tools and skills the posting asked for -- Kubernetes, Terraform, CI/CD --
not on "management" or "best", which the keyword match also counts. Nothing
is added to the text; only the weight of words already there changes.
"""

from __future__ import annotations

import re

PER_LINE = 3          # at most this many bold terms in one bullet or paragraph


def terms(jd_text: str, resume_text: str, company: str | None = None) -> list[str]:
    """The posting's keywords the resume shows, kept when they name a tool or skill.

    A term counts as one when it is several words ("infrastructure as code")
    or the resume writes it capitalised somewhere other than the start of a
    sentence (Kubernetes, Splunk, DevOps): a name, not an ordinary word.
    """
    from ml.resume.ats import compare
    try:
        matched = compare(resume_text, resume_text, jd_text, company=company).get("matched") or []
    except Exception:
        return []
    kept = []
    for term in matched:
        if " " in term or _named(term, resume_text):
            kept.append(term)
    if "ci" in matched and "cd" in matched:
        kept = [t for t in kept if t not in ("ci", "cd")] + ["ci/cd"]
    return sorted(set(kept), key=lambda t: (-len(t), t))


# Words a posting and a resume share that name no tool or skill. Capitalised
# in a resume's group titles ("Application Development & ..."), they read as
# names and every other bullet came out half bold.
GENERIC = {"management", "practices", "application", "applications", "development", "engineering",
           "architecture", "infrastructure", "build", "tools", "design", "deployment", "platform",
           "environment", "environments", "best", "lifecycle", "security", "systems", "services",
           "solutions", "support", "operations", "performance", "data", "cloud"}


_SMALL = {"and", "of", "for", "to", "with", "in", "the", "as", "on", "a", "an", "&", "/", "-"}


def _title_line(line: str) -> bool:
    """"Application Development & Delivery": every word capitalised, a group title."""
    if "," in line:
        return False                      # "Streaming: DynamoDB, Redis, Kafka" is a list of names
    words = [w for w in re.findall(r"[A-Za-z][\w.+#-]*", line) if w.lower() not in _SMALL]
    return bool(words) and all(w[0].isupper() for w in words)


_CAPITAL = re.compile(r"[A-Za-z]*[A-Z][\w+#-]*")


def _named(term: str, text: str) -> bool:
    """Capitalised inside a sentence or a list as a name of its own -- not at its start, not in a title,
    not a group's label, and not one word of a longer name.

    "Control" in "Version Control & Artifact Management:" and "Server" in "MS SQL Server" are words of
    a label and of another name: counted as names, "access **control**" and "vCenter **Server**" came
    out bold (Oct 2026).
    """
    if term in GENERIC:
        return False
    for line in text.splitlines():
        if _title_line(line):
            continue
        label, sep, rest = line.partition(":")
        if sep and 1 <= len(label.split()) <= 7:
            line = "_" * (len(label) + 1) + rest          # a skills row's label names a group
        for m in re.finditer(rf"(?<![\w/]){re.escape(term)}(?![\w])", line, re.I):
            word = m.group(0)
            if word == word.lower() or not line[:m.start()].strip(" \u2022-*\t"):
                continue
            if word.isupper() or not word[1:].islower():
                return True                               # written its own way: AWS, DevOps, GitHub
            before, after = line[:m.start()], line[m.end():]
            words_before = before.rstrip().split(" ")
            # The word before, unless it opens the line: "Ran Kubernetes clusters" is a sentence.
            joined = (before.endswith(" ") and len(" ".join(words_before[:-1]).strip(" _\u2022-*\t")) > 0
                      and _CAPITAL.fullmatch(words_before[-1] or "")) or \
                     (after.startswith(" ") and _CAPITAL.match(after[1:]) is not None)
            if not joined:
                return True                               # Kubernetes, Terraform, Jenkins: a name alone
    return False


def spans(text: str, keywords: list[str], limit: int = PER_LINE,
          used: set[str] | None = None) -> list[tuple[str, bool]]:
    """The text cut into (piece, bold) runs: each keyword's first use, at most `limit`.

    `used` holds the terms already set in bold nearby (in this job's bullets): a term is bold once
    there, not in every bullet -- "Jenkins" and "CI/CD" bold in line after line made the page a pattern.
    """
    if not keywords or not text:
        return [(text, False)]
    found: list[tuple[int, int, frozenset]] = []
    own = {k.lower() for k in keywords}
    for term in sorted(keywords, key=len, reverse=True):   # "infrastructure as code" before "code"
        if used is not None and term.lower() in used:
            continue
        m = re.search(rf"(?<![\w/]){re.escape(term)}(?![\w/])", text, re.I)
        if not m:
            continue
        end = m.end()
        # The whole name, not part of it: "Azure Key Vault", "Spring Boot".
        while nxt := re.match(r" [A-Z][\w+#-]*(?![\w/])", text[end:]):
            end += len(nxt.group(0))
        start = m.start()
        # ...and from its start: "Azure DevOps", not "Azure **DevOps**". A line's first word is no name's.
        # A word that is a keyword of its own stays its own: "Azure" and "CI/CD", not "Azure CI/CD".
        while (prev := re.search(r"(?<![\w/])([A-Z][\w+#-]*|[a-z]+[A-Z][\w+#-]*) $", text[:start])) \
                and text[:prev.start(1)].strip(" \u2022-*\t") and prev.group(1).lower() not in own:
            start = prev.start(1)
        terms_here = {term.lower()}
        # Overlapping another keyword's span: one phrase, "Azure DevOps".
        for a, b, held in [x for x in found if x[0] < end and start < x[1]]:
            found.remove((a, b, held))
            start, end, terms_here = min(a, start), max(b, end), terms_here | held
        found.append((start, end, frozenset(terms_here)))
    found = sorted(found)[:limit]
    if used is not None:
        for _, _, held in found:
            used.update(held)
    out, at = [], 0
    for a, b, _ in found:
        if a > at:
            out.append((text[at:a], False))
        out.append((text[a:b], True))
        at = b
    if at < len(text):
        out.append((text[at:], False))
    return out
