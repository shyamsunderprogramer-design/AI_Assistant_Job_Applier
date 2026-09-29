"""Small typing fixes, so a document reads as typed by a person.

Applied to everything the resume and letter writers print, the person's own
lines included -- these change spacing and punctuation, never a word:

  "45 %" -> "45%"   "11 + years" -> "11+ years"   "~ 30%" -> "~30%"
  an em dash -> " - "   (the long dash is the best-known sign of model prose)
  runs of spaces -> one
"""

from __future__ import annotations

import re


def typeset(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"(\d)\s+%", r"\1%", text)
    text = re.sub(r"(\d)\s+\+(?=[\s,.;:)]|$)", r"\1+", text)
    text = re.sub(r"~\s+(?=\d)", "~", text)
    text = re.sub(r"\s*—\s*", " - ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


# Words that mark text as model-written. The prompts forbid introducing them;
# the review note names any that got through.
TELLS = ("spearhead", "leverag", "utiliz", "synerg", "dynamic ", "results-driven",
         "passionate", "cutting-edge", "seamless", "robust", "holistic", "delve",
         "acumen", "pivotal", "meticulous", "transformative", "proven track record",
         "fast-paced", "tapestry", "elevate", "empower", "adept")


def tells_in(text: str) -> list[str]:
    low = (text or "").lower()
    return [t.strip() for t in TELLS if t in low]
