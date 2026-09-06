"""Keyword/role filtering. Pure functions — unit-tested without network.

Matching is WORD-BOUNDARY, not bare substring. That distinction is not
cosmetic: a substring match on "intern" drops "Internal Tools Engineer", and a
substring match on the location "us" keeps "Aarhus, Denmark", "Vilnius,
Lithuania", and "Sydney, Australia". Both were happening in real runs.

A keyword may still contain spaces or punctuation ("full stack", "c++", "vp of
engineering") — the boundary is applied to the ends of the whole phrase, and
only where the adjacent character is alphanumeric.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from scraper.base import RawJob


@lru_cache(maxsize=2048)
def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Compile one keyword into a boundary-aware pattern.

    `\\b` is wrong for keywords that start or end in punctuation ("c++", "vp "),
    so the boundary is only asserted on the side that is actually alphanumeric.
    """
    escaped = re.escape(keyword)
    prefix = r"(?<![a-z0-9])" if keyword[:1].isalnum() else ""
    suffix = r"(?![a-z0-9])" if keyword[-1:].isalnum() else ""
    return re.compile(prefix + escaped + suffix)


def matches_keyword(text: str, keyword: str) -> bool:
    """True if `keyword` occurs in `text` as a whole word/phrase."""
    if not text or not keyword:
        return False
    return bool(_keyword_pattern(keyword).search(text.lower()))


def matches_any(text: str, keywords: list[str]) -> bool:
    return any(matches_keyword(text, kw) for kw in keywords)


@dataclass
class JobFilter:
    title_keywords: list[str] = field(default_factory=list)
    exclude_title_keywords: list[str] = field(default_factory=list)
    location_keywords: list[str] = field(default_factory=list)
    exclude_location_keywords: list[str] = field(default_factory=list)
    description_required_keywords: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg) -> "JobFilter":
        return cls(
            title_keywords=_lower(cfg.get("filters.title_keywords", [])),
            exclude_title_keywords=_lower(cfg.get("filters.exclude_title_keywords", [])),
            location_keywords=_lower(cfg.get("filters.location_keywords", [])),
            exclude_location_keywords=_lower(cfg.get("filters.exclude_location_keywords", [])),
            description_required_keywords=_lower(
                cfg.get("filters.description_required_keywords", [])
            ),
        )

    def reject_reason(self, job: RawJob) -> str | None:
        """Why this job was dropped, or None if it was kept.

        Returning the reason rather than a bool makes a filtering run
        explainable — `main.py why` uses it to show why a posting vanished.
        """
        title = (job.title or "").lower()

        if self.title_keywords and not matches_any(title, self.title_keywords):
            return "title matched no title_keywords"

        for kw in self.exclude_title_keywords:
            if matches_keyword(title, kw):
                return f"title excluded by {kw!r}"

        location = (job.location or "").lower()
        if location:
            # Exclusion wins over inclusion: "Remote - India" matches the
            # include term "remote" but is still not a US role.
            for kw in self.exclude_location_keywords:
                if matches_keyword(location, kw):
                    return f"location excluded by {kw!r}"
            if self.location_keywords and not matches_any(location, self.location_keywords):
                return "location matched no location_keywords"
        # An empty location can't be ruled out, so keep it for manual review.

        if self.description_required_keywords:
            body = (job.description or "").lower()
            if not matches_any(body, self.description_required_keywords):
                return "description missing all required keywords"

        return None

    def matches(self, job: RawJob) -> bool:
        return self.reject_reason(job) is None

    def apply(self, jobs: list[RawJob]) -> list[RawJob]:
        return [job for job in jobs if self.matches(job)]


def _lower(values) -> list[str]:
    return [str(v).lower().strip() for v in (values or []) if str(v).strip()]
