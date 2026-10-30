"""Each job's title nearest the role applied for, from the titles the person lists for it.

Some jobs have more than one true title -- "CLOUD ENGINEER" and "CLOUD ARCHITECT / ENGINEER" for
the same years -- and the person kept a resume for each (21 Oct 2026). Listed once in
ml/resume/titles.yaml (gitignored), a tailoring uses the one nearest the posting. Only listed
titles are ever used: none is made up for a posting, and the employer and dates stay as written.

    - role: "CLOUD ENGINEER, CONTOSO LTD, USA  \tJan 2015 – Jun 2017"
      titles: ["CLOUD ENGINEER", "CLOUD ARCHITECT / ENGINEER"]

The title the resume has now goes in the list too. The headline under the name works the same
way, each choice a whole line:

    - role: HEADLINE
      titles: ["Sr. DevOps / Azure DevOps Engineer | Azure & AWS | Terraform", "..."]
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

TITLES_PATH = PROJECT_ROOT / "ml" / "resume" / "titles.yaml"
HEADLINE = "HEADLINE"


def load(path: Path | None = None) -> list[dict]:
    import yaml
    try:
        data = yaml.safe_load(Path(path or TITLES_PATH).read_text(encoding="utf-8")) or []
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("role") and len(d.get("titles") or []) > 1]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9+#.-]*", (text or "").lower()) if len(w) > 2}


def nearest(titles: list[str], posting_title: str, jd_text: str, own: str) -> str:
    """The title sharing most words with the posting, a word of its title counting three times one
    of its text. A word matches its longer forms ("architect" is in "architecture"). On a tie, `own`."""
    head, body = _words(posting_title), _words(jd_text)

    def score(title: str) -> int:
        def hits(words):
            return sum(any(p == w or (len(w) >= 5 and p.startswith(w)) for p in words) for w in _words(title))
        return 3 * hits(head) + hits(body)
    best = max(titles, key=score)
    return own if score(best) == score(own) else best


def apply(resume, posting_title: str, jd_text: str, path: Path | None = None) -> list[str]:
    """Swap each listed job's title, in this parsed resume, for the listed one nearest the posting.

    Returns what changed, as notes for the run ("CLOUD ARCHITECT / ENGINEER, not CLOUD ENGINEER").
    """
    plain = lambda text: re.sub(r"\s+", " ", text or "").strip()      # noqa: E731
    changed = []
    for entry in load(path):
        titles = [str(t).strip() for t in entry["titles"] if str(t).strip()]
        for section in resume.sections:
            for i, line in enumerate(section.lines):
                text = line.strip()
                if entry["role"] == HEADLINE:
                    if section.heading != "HEADER":
                        continue
                    own = next((t for t in titles if plain(t) == plain(text)), None)
                    rest = ""
                else:
                    if plain(text) != plain(entry["role"]):
                        continue
                    # The title written now: the longest listed one the job line starts with.
                    own = next((t for t in sorted(titles, key=len, reverse=True)
                                if text.upper().startswith(t.upper())), None)
                    rest = text[len(own):] if own else ""
                if own is None:
                    continue
                new = nearest(titles, posting_title, jd_text, own)
                if new == own:
                    continue
                section.lines[i] = line.replace(text, new + rest, 1)
                resume.raw_text = resume.raw_text.replace(text, new + rest, 1)
                changed.append(f"{new}, not {own}")
    return changed
