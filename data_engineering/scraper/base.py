"""Shared scraper interface. One module per ATS type, never per company."""

from __future__ import annotations

import html
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from data_engineering.scraper.http_client import PoliteClient


@dataclass(frozen=True)
class CompanyRef:
    """A company as a tenant of one ATS."""

    name: str
    slug: str
    source: str


@dataclass
class RawJob:
    """Normalised posting, identical shape across every portal."""

    source: str
    company: str
    company_slug: str
    external_id: str
    title: str
    location: str | None
    description: str | None
    application_url: str
    posted_at: datetime | None = None
    requirements: str | None = None
    job_type: str | None = None          # the board's own word: "Full-time", "Contract", "FullTime"…
    # Its description was asked for and did not come (an error, a refusal, no answer): it is still on the board,
    # and its stored text is kept rather than wiped (runner._persist); the board's relevant descriptions are
    # incomplete until a later read brings it (Nov 2026).
    detail_missing: bool = False


class Listing(list):
    """A board's listing (`stub_jobs`): every posting it names, and whether that is all of them.

    `complete` is False when the reader stopped before the end -- a page cap (Workday's 1,000, Workable's 2,000)
    -- so "not in this listing" cannot mean "gone from the board". A plain list is a complete listing."""

    def __init__(self, items=(), complete: bool = True, total: int | None = None):
        super().__init__(items)
        self.complete = complete
        self.total = total


class PortalScraper(ABC):
    """Base for every ATS scraper."""

    name: str = "base"
    # Whether the listing's ids are the postings' own durable ids, the ones stored (Nov 2026). Then a COMPLETE
    # listing is authoritative: a stored posting it no longer names is gone, even when its description was never
    # asked for because the search leaves it out. Workday's listing ids are URL paths, and the stored id is the
    # requisition id that comes only with the description -- so Workday never closes on its listing.
    durable_listing_ids: bool = False

    def __init__(self, client: PoliteClient):
        self.client = client

    @abstractmethod
    def fetch_jobs(self, company: CompanyRef) -> list[RawJob]:
        """Return every posting for one company. Raises on failure; the runner
        catches, logs to scrape_log, and continues to the next company."""

    @abstractmethod
    def board_url(self, slug: str) -> str:
        """Human-facing board URL for a slug."""

    @abstractmethod
    def board_exists(self, slug: str) -> bool:
        """True if this slug is a live board on this ATS. Used by discovery."""


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")


def html_to_text(raw: str | None) -> str | None:
    """Flatten an HTML job description to readable plain text.

    Greenhouse returns HTML-escaped markup; block tags become newlines so the
    JD stays readable in Excel and parseable for keyword scoring in Phase 3.
    """
    if not raw:
        return None
    text = html.unescape(raw)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKLINES_RE.sub("\n\n", text).strip() or None


# Once a JD switches to logistics, benefits, or legal text it has stopped
# describing the job. Collecting past this point pollutes both the scorer and
# the "Required Skills" column with office-location and EEO boilerplate.
_BOILERPLATE_MARKERS = (
    "this role will be based",
    "hybrid environment",
    "days in the office",
    "equal opportunity",
    "equal employment",
    "without regard to",
    "compensation range",
    "salary range",
    "base pay",
    "pay range",
    "total compensation",
    "benefits include",
    "our benefits",
    "we offer",
    "perks",
    "applicants must",
    "visa sponsorship",
    "background check",
    "reasonable accommodation",
    "e-verify",
    "about us",
    "why join us",
    "life at",
)

_REQ_HEADINGS = (
    "requirements",
    "qualifications",
    "what you'll need",
    "what you will need",
    "what we're looking for",
    "what we are looking for",
    "who you are",
    "basic qualifications",
    "minimum qualifications",
    "you have",
    "skills",
)


def extract_requirements(description: str | None) -> str | None:
    """Best-effort pull of the requirements section out of a JD.

    Heuristic by design — a miss returns None rather than guessing, and Phase 3
    scores against the full JD text anyway.
    """
    if not description:
        return None

    lines = description.split("\n")
    start = None
    for i, line in enumerate(lines):
        probe = line.strip().lower().rstrip(":").lstrip("- ")
        if not probe or len(probe) > 60:
            continue
        if any(probe.startswith(h) for h in _REQ_HEADINGS):
            start = i + 1
            break

    if start is None:
        return None

    collected: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        probe = stripped.lower().rstrip(":")

        # Stop as soon as the JD turns to logistics/benefits/legal text.
        if any(marker in probe for marker in _BOILERPLATE_MARKERS):
            break

        # Stop at the next section heading (short, non-bullet line).
        if (
            collected
            and stripped
            and len(probe) < 60
            and not stripped.startswith("-")
            and probe.endswith(("benefits", "about us", "compensation", "perks", "eeo"))
        ):
            break

        collected.append(stripped)
        if len("\n".join(collected)) > 4000:
            break

    result = "\n".join(collected).strip()
    return result or None
