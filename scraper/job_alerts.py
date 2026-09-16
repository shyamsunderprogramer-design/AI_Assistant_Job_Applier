"""Read job postings out of alert emails, not just company names.

`scraper/mailbox.py` already mines an inbox, but only for company NAMES to
feed the slug prober. That misses the point of the emails: they contain the
jobs themselves, and they contain jobs this tool can reach no other way.

The evidence for that is a single line in a real inbox -- "DevOps Engineer @
Zoom". Zoom was probed against Greenhouse, Lever and Ashby and correctly found
on none of them, because Zoom uses Clinch. No amount of probing company names
would ever surface that posting, and the person applied to it from the email
having never seen it in this tool. The alert already had it.

So this reads the postings. Indeed's alert format is consistent enough to
parse exactly, checked across a sample of real messages:

    Subject:  DevOps Engineer @ Zoom
    Body:     DevOps Engineer
              Zoom
              Remote
              Salary: $98,900 - $228,700 a year
              Job type: Full-time
              View job: https://cts.indeed.com/v3/...

The subject carries "<title> @ <company>" on every message sampled, which is
the most reliable pair available: body layout varies (Salary is sometimes
absent, Schedule and Work setting are sometimes present) but the subject does
not. The body is used for what the subject cannot give -- location, salary,
job type, and the link.

These postings are stored like any other, with `source="indeed-email"`, so
they are scored, filtered, ranked and packaged by exactly the same code as a
Greenhouse posting. The only thing they cannot do is be applied to
automatically: the link is a redirect into whichever ATS the employer uses,
which is the entire reason the posting was invisible in the first place.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# "Senior DevOps Engineer (US REMOTE) @ Motorola Solutions"
SUBJECT = re.compile(r"^(?P<title>.+?)\s+@\s+(?P<company>.+?)\s*$")

# Lines the body uses as labelled fields. Anything unlabelled between the
# company and the first labelled line is the location.
LABELS = {
    "salary": re.compile(r"^Salary:\s*(.+)$", re.I),
    "job_type": re.compile(r"^Job type:\s*(.+)$", re.I),
    "schedule": re.compile(r"^Schedule:\s*(.+)$", re.I),
    "work_setting": re.compile(r"^Work setting:\s*(.+)$", re.I),
    "url": re.compile(r"^View job:\s*(\S+)", re.I),
}

# Indeed wraps every link in a tracking redirect. The id inside is stable per
# posting, which is what makes it usable as an external_id -- without one, the
# same job in tomorrow's alert would be stored a second time.
INDEED_ID = re.compile(r"cts\.indeed\.com/v3/([A-Za-z0-9_\-]{16,})")


@dataclass
class AlertJob:
    """One posting lifted out of one alert email."""

    title: str
    company: str
    url: str
    location: str | None = None
    salary: str | None = None
    job_type: str | None = None
    posted_at: datetime | None = None
    source: str = "indeed-email"
    external_id: str = ""
    description: str = ""
    extras: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.external_id:
            found = INDEED_ID.search(self.url or "")
            # The tracking blob is long; its first 48 characters are already
            # unique per posting and keep the stored id readable.
            self.external_id = found.group(1)[:48] if found else (self.url or "")[:96]


def strip_html(raw: str) -> list[str]:
    """The visible lines of an email body, in order."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = html.unescape(text)
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_alert(subject: str, body: str, received: datetime | None = None) -> AlertJob | None:
    """One posting from one Indeed alert, or None if this is not one.

    Returns None rather than raising: an inbox holds every kind of message,
    and a newsletter that happens to mention a job is not a posting.
    """
    subject = html.unescape(" ".join((subject or "").split()))
    match = SUBJECT.match(subject)
    if not match:
        return None

    title = match.group("title").strip()
    company = match.group("company").strip()
    if not title or not company:
        return None

    lines = strip_html(body)
    fields: dict[str, str] = {}
    location = None
    company_at = None

    for index, line in enumerate(lines):
        for name, pattern in LABELS.items():
            found = pattern.match(line)
            if found and name not in fields:
                fields[name] = found.group(1).strip()
                break
        else:
            # Not a labelled line. The company appears on its own line, and
            # the line straight after it is the location.
            if company_at is None and line.lower() == company.lower():
                company_at = index
            elif company_at is not None and index == company_at + 1 and location is None:
                if not any(p.match(line) for p in LABELS.values()):
                    location = line

    url = fields.get("url")
    if not url:
        return None                 # nothing to apply to; not worth storing

    return AlertJob(
        title=title,
        company=company,
        url=url,
        location=location,
        salary=fields.get("salary"),
        job_type=fields.get("job_type"),
        posted_at=received,
        extras={k: v for k, v in fields.items()
                if k in ("schedule", "work_setting") and v},
    )


def describe(job: AlertJob) -> str:
    """A short description, since an alert email carries no job description.

    Scoring reads the description, so an empty one would score every emailed
    posting at zero and bury it. What the email does give -- title, company,
    location, salary, type -- is honest and is what there is. The posting page
    itself is a redirect into an unknown ATS, and fetching it is a separate
    question from reading the email.
    """
    parts = [f"{job.title} at {job.company}"]
    if job.location:
        parts.append(f"Location: {job.location}")
    if job.salary:
        parts.append(f"Salary: {job.salary}")
    if job.job_type:
        parts.append(f"Job type: {job.job_type}")
    for key, value in (job.extras or {}).items():
        parts.append(f"{key.replace('_', ' ').title()}: {value}")
    parts.append("Source: job alert email — the full description is on the "
                 "employer's own posting.")
    return "\n".join(parts)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
