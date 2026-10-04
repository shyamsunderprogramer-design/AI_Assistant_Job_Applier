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


def state_codes(text: str) -> str:
    """"Kakinada, Ap, India" -> "Kakinada, AP, India": a state code between commas."""
    return re.sub(r"(?<=, )([A-Z][a-z])(?=, [A-Z])", lambda m: m.group(1).upper(), text or "")


# Model-flavoured words and the plain ones a person writes instead. Verb forms
# only, matched as whole words, so "CPU utilization" and "leverage ratio" in a
# finance line are left alone. Swapping a verb never changes what was done.
PLAIN = [
    (r"\bspearheaded\b", "led"), (r"\bspearheading\b", "leading"), (r"\bspearheads\b", "leads"),
    (r"\bspearhead\b", "lead"),
    (r"\butilized\b", "used"), (r"\butilizing\b", "using"), (r"\butilizes\b", "uses"),
    (r"\butilize\b", "use"),
    (r"\bleveraged\b", "used"), (r"\bleveraging\b", "using"),
    (r"\brobust\b", "reliable"), (r"\bseamlessly\b", "smoothly"), (r"\bseamless\b", "smooth"),
    (r"\bcutting-edge\b", "modern"), (r"\bacumen\b", "skills"),
    (r"\bmeticulously\b", "carefully"), (r"\bmeticulous\b", "careful"),
    (r"\bDynamic (?=[A-Z])", ""),        # "Dynamic Senior Engineer" -> "Senior Engineer"
]


def plain_words(text: str) -> str:
    """Swap model-flavoured words for plain ones, keeping a capital where there was one."""
    def swap(pattern: str, plain: str, value: str) -> str:
        def repl(m):
            word = m.group(0)
            if not plain:
                return ""
            return plain[:1].upper() + plain[1:] if word[:1].isupper() else plain
        return re.sub(pattern, repl, value, flags=re.I if not pattern.startswith(r"\bDynamic") else 0)
    for pattern, plain in PLAIN:
        text = swap(pattern, plain, text)
    return text


# Words that mark text as model-written. The prompts forbid introducing them;
# the review note names any that got through.
TELLS = ("spearhead", "leverag", "utilize", "utilizing", "synerg", "dynamic ", "results-driven",
         "passionate", "cutting-edge", "seamless", "robust", "holistic", "delve",
         "acumen", "pivotal", "meticulous", "transformative", "proven track record",
         "fast-paced", "tapestry", "elevate", "empower", "adept")


def tells_in(text: str) -> list[str]:
    low = (text or "").lower()
    return [t.strip() for t in TELLS if t in low]
