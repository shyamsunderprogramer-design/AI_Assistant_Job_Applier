"""The person's own answers to screening questions, learned as they apply.

Forms ask the same things over and over — desired salary, "Are you 18 or
older?", citizenship, notice period — in wording each employer chooses. The
profile covers the common ones; this covers the rest, and it is filled by the
person, never by this tool: when they finish a form by hand, the short answers
they gave are kept, keyed by the question's wording, and the next form asking
the same question gets the same answer.

Three limits keep that honest:

  * Exact wording only (after trimming "*" and case). A similar question is a
    different question; a legal declaration especially.
  * Short answers only. A paragraph written for one employer ("What makes you
    uniquely qualified for this role at CMT?") must never be sent to another.
  * Nothing naming the employer it was written for, in the question or answer.

The file is plain YAML the person can read and correct, and it is gitignored:
it holds their salary expectation and legal answers.
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

BANK_PATH = PROJECT_ROOT / "backend" / "config" / "answers.yaml"
MAX_ANSWER_CHARS = 80

# Filled from the profile or attached as files; never learned.
_NOT_LEARNED = ("first name", "last name", "preferred first name", "email", "phone",
                "resume", "cover letter", "attach", "enter manually", "linkedin",
                "github", "website", "portfolio", "country", "location", "city", "state", "zip",
                # True for one form only: the day it was signed, the job it was for, a start date.
                "date", "today", "signature date", "what full time job", "which position", "what position",
                "when can you start", "start date")
# A job site's own boxes, not the employer's questions: an Adzuna page's "Receive similar jobs by
# email" was learned with the person's address (22 Oct 2026), ready to sign them up for alerts.
_SITE_BOXES = re.compile(r"similar jobs|job alert|jobs? by email|newsletter|subscribe|notify me|"
                         r"email me|alerts? for", re.I)


def normalise(label: str) -> str:
    text = re.sub(r"\s+", " ", (label or "").replace("*", " ")).strip().lower()
    return text.rstrip(" ?:.")


def load(path: Path | None = None) -> dict[str, str]:
    import yaml

    target = Path(path or BANK_PATH)
    if not target.exists():
        return {}
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}
    return {normalise(k): str(v) for k, v in data.items() if v not in (None, "")}


def _company_words(company: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (company or "").lower())
            if len(w) > 2 and w not in {"inc", "llc", "ltd", "the", "corp", "group"}}


def learnable(label: str, value: str, company: str = "") -> bool:
    from backend.apply.essays import is_essay
    question, answer = normalise(label), (value or "").strip()
    if not question or not answer or len(answer) > MAX_ANSWER_CHARS:
        return False
    if any(question.startswith(p) or question == p for p in _NOT_LEARNED) or _SITE_BOXES.search(question):
        return False
    if is_essay(label):
        return False  # job-specific prose is draft material, never an exact answer for every employer
    words = _company_words(company)
    said = set(re.findall(r"[a-z0-9]+", f"{question} {answer.lower()}"))
    return not (words & said)


def remember(answers: dict[str, str], company: str = "",
             path: Path | None = None) -> list[str]:
    """Add the learnable ones to the bank. Returns the questions learned."""
    from backend.apply.filesafe import one_writer

    target = Path(path or BANK_PATH)
    with one_writer(target):                     # read, changed and written back as one (29 Oct 2026)
        bank = load(target)
        learned = []
        for label, value in answers.items():
            if learnable(label, value, company):
                key = normalise(label)
                if bank.get(key) != value.strip():
                    bank[key] = value.strip()
                    learned.append(key)
        if learned:
            _write_bank(target, bank)
    return learned


def save_answer(question: str, answer: str, path: Path | None = None) -> None:
    """One answer the person gave outright (the assistant's "remember this"), kept exactly as given."""
    from backend.apply.filesafe import one_writer

    target = Path(path or BANK_PATH)
    with one_writer(target):
        bank = load(target)
        bank[question] = answer
        _write_bank(target, bank)


def _write_bank(target: Path, bank: dict[str, str]) -> None:
    import yaml

    from backend.apply.filesafe import write_whole
    write_whole(target, "# Your own answers to screening questions, learned from forms you\n"
                        "# finished yourself. Edit or delete any line; it is reused exactly.\n"
                + yaml.safe_dump(dict(sorted(bank.items())), sort_keys=False, allow_unicode=True))
