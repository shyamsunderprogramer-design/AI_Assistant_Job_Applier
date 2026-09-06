"""Anti-fabrication guard.

The prompt tells Claude not to invent experience. This module assumes the
prompt might not be enough and checks the output anyway — a resume that claims
a skill, employer, or metric the user doesn't have is worse than no automation
at all, and it is the user who has to defend it in an interview.

The check is deliberately conservative in one direction: it would rather raise
a false alarm (which the user reviews) than let an invention through. Anything
it flags is surfaced, and by default a flagged tailoring is rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from resume.scorer import SKILL_TERMS, STOPWORDS, tokenize

# Numeric claims: "40%", "$2M", "3x", "10,000", "5 years"
NUMERIC_RE = re.compile(r"\$?\d[\d,\.]*\s*(?:%|x\b|k\b|m\b|bn?\b|million|billion)?", re.I)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
# A capitalised token mid-sentence is usually a proper noun (employer, product).
PROPER_NOUN_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z0-9&.\-]{2,})\b", re.M)

# Skill/tech claims are the highest-risk fabrication: a tailored bullet that
# adds "Kubernetes" because the JD asked for it is the exact failure mode.
# Shares one vocabulary with the scorer so the two can never drift apart.
TECH_TERMS = SKILL_TERMS - {
    # Generic capability words in the scorer's list that aren't claims of a
    # specific tool, so rewording around them isn't fabrication.
    "api", "apis", "backend", "frontend", "fullstack", "devops", "sre", "ci", "cd",
    "architecture", "algorithms", "debugging", "profiling", "mentoring", "leadership",
    "scalability", "latency", "throughput", "sharding", "caching", "observability",
}

# Words that are capitalised for reasons other than being a proper noun.
COMMON_CAPITALISED = {
    "I", "A", "The", "And", "Or", "But", "For", "With", "In", "On", "At", "To", "By",
    "Led", "Built", "Designed", "Developed", "Managed", "Delivered", "Improved", "Reduced",
    "Increased", "Created", "Implemented", "Owned", "Drove", "Shipped", "Launched",
    "Collaborated", "Partnered", "Migrated", "Automated", "Optimized", "Optimised",
    "Architected", "Engineered", "Maintained", "Scaled", "Refactored", "Analyzed",
    "Analysed", "Coordinated", "Mentored", "Supported", "Streamlined", "Spearheaded",
    "Senior", "Junior", "Lead", "Staff", "Principal", "Engineer", "Software", "Data",
    "Summary", "Experience", "Education", "Skills", "Projects", "Present", "Current",
}


@dataclass
class Violation:
    kind: str          # skill | metric | year | entity
    value: str
    context: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.value!r} — in: {self.context[:90]}"


@dataclass
class GuardResult:
    ok: bool
    violations: list[Violation] = field(default_factory=list)

    def report(self) -> str:
        if self.ok:
            return "No fabrication detected."
        lines = [f"{len(self.violations)} unsupported claim(s):"]
        lines.extend(f"  {v}" for v in self.violations)
        return "\n".join(lines)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def _base_vocabulary(base_text: str) -> set[str]:
    return set(tokenize(base_text))


def _digit_runs(text: str) -> set[str]:
    """Every distinct number in the text, comma-separators removed.

    Compared as whole runs, never as substrings of a concatenated blob — "92"
    must not be considered supported just because the base resume contains
    "2019" and "2021".
    """
    return {run.lstrip("0") or "0" for run in re.findall(r"\d+", text.replace(",", ""))}


def check_no_fabrication(base_text: str, tailored_text: str) -> GuardResult:
    """Flag claims in `tailored_text` not evidenced in `base_text`."""
    base_norm = _normalise(base_text)
    base_vocab = _base_vocabulary(base_text)
    base_numbers = _digit_runs(base_text)
    violations: list[Violation] = []

    for line in (tailored_text or "").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        # 1. Skills/technologies the base resume never mentions.
        for token in tokenize(stripped):
            if token in TECH_TERMS and token not in base_vocab:
                violations.append(Violation("skill", token, stripped))

        # 2. Metrics — a number that isn't in the base resume is invented.
        for match in NUMERIC_RE.findall(stripped):
            value = match.strip()
            if not value or not any(c.isdigit() for c in value):
                continue
            for run in _digit_runs(value):
                if run not in base_numbers:
                    violations.append(Violation("metric", value, stripped))
                    break

        # 3. Dates.
        for year in YEAR_RE.findall(stripped):
            if year not in base_norm:
                violations.append(Violation("year", year, stripped))

        # 4. Employers / products / institutions.
        for noun in PROPER_NOUN_RE.findall(stripped):
            if noun in COMMON_CAPITALISED or noun.lower() in STOPWORDS:
                continue
            if noun.lower() not in base_vocab:
                violations.append(Violation("entity", noun, stripped))

    # De-duplicate on (kind, value) — one report per invented thing.
    seen: set[tuple[str, str]] = set()
    unique: list[Violation] = []
    for violation in violations:
        key = (violation.kind, violation.value.lower())
        if key not in seen:
            seen.add(key)
            unique.append(violation)

    return GuardResult(ok=not unique, violations=unique)
