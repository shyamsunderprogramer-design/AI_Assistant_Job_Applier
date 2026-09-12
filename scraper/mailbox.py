"""Harvest company names from a mailbox.

Job-alert emails, recruiter outreach, and application confirmations name
companies that are hiring RIGHT NOW. That makes an inbox a better discovery
seed than any purchased list: a static export goes stale (the one imported here
returned 4.7% live boards, full of firms acquired years ago), while an inbox
refreshes itself.

Two ways in, because credentials are a real cost:

  IMAP        — read-only, app-password, nothing stored. Fastest to set up.
  .mbox file  — a Google Takeout export, parsed entirely offline. Slower to
                obtain, but no credentials leave the machine at all.

Read-only by construction: the IMAP session is opened with readonly=True, so a
bug here cannot delete, move, or mark a message. Nothing but extracted company
names is written to disk, and the password is never persisted by this module.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from pathlib import Path

log = logging.getLogger(__name__)

# Senders whose mail is *about* jobs. Used to narrow an IMAP search so the
# whole mailbox is never downloaded.
JOB_SENDERS = (
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "dice.com", "monster.com", "simplyhired.com", "builtin.com",
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkday.com",
    "hired.com", "wellfound.com", "angel.co", "otta.com", "levels.fyi",
    "smartrecruiters.com", "jobvite.com", "workable.com", "icims.com",
    "talent.com", "jobs.ac.uk", "seek.com",
)

JOB_SUBJECT_TERMS = (
    "job", "role", "position", "opportunity", "hiring", "opening",
    "application", "interview", "recruiter", "career", "vacancy",
)

# Phrasings that put a company name somewhere findable. Capturing group 1 is
# always the company. Deliberately conservative: a missed name costs nothing,
# a wrong one costs a wasted probe and a junk row.
COMPANY_PATTERNS = [
    # "Senior SRE at Stripe", "at Stripe, Inc.", "at Figma and at MongoDB".
    # No trailing-punctuation requirement: the capture group only takes
    # Capitalised words, so it stops at the next lowercase word on its own.
    # Requiring a comma or line end missed every digest that chains roles
    # together ("Roles at Figma and at MongoDB this week").
    re.compile(r"\bat\s+([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})"),
    # "Your application to Datadog", "applied to Ramp"
    re.compile(r"\bapplication (?:to|at|for)\s+([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})"),
    re.compile(r"\bapplied to\s+([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})"),
    # "Thanks for your interest in Cloudflare"
    re.compile(r"\binterest in\s+([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})"),
    # "Stripe is hiring", "Ramp is looking for"
    re.compile(r"^([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})\s+is\s+(?:hiring|looking for|seeking)", re.M),
    # "A message from the Snowflake recruiting team"
    re.compile(r"\bfrom (?:the\s+)?([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,2})\s+(?:recruiting|talent|hiring|careers)\b"),
]

# ATS URLs name the company in the path — the single most reliable signal in an
# inbox, because it is the slug itself rather than a guess at one.
ATS_URL_PATTERNS = [
    (re.compile(r"boards\.greenhouse\.io/([a-z0-9][a-z0-9_-]{1,60})", re.I), "greenhouse"),
    (re.compile(r"job-boards\.greenhouse\.io/([a-z0-9][a-z0-9_-]{1,60})", re.I), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([a-z0-9][a-z0-9_-]{1,60})", re.I), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([a-z0-9][a-z0-9_-]{1,60})", re.I), "ashby"),
]

# Words that look like company names in these patterns but are not.
STOPWORDS = {
    "the", "a", "an", "this", "that", "your", "our", "my", "you", "we", "i",
    "linkedin", "indeed", "glassdoor", "ziprecruiter", "dice", "monster",
    "google", "gmail", "microsoft", "apple", "yahoo", "outlook",
    "new", "view", "apply", "click", "here", "now", "today", "more", "all",
    "job", "jobs", "role", "roles", "career", "careers", "team", "company",
    "remote", "hybrid", "onsite", "full", "part", "time", "senior", "junior",
    "lead", "staff", "principal", "manager", "director", "engineer",
    "unsubscribe", "privacy", "terms", "help", "support", "email", "reply",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}


@dataclass
class MailboxFindings:
    """Company names harvested from a mailbox."""

    names: Counter = field(default_factory=Counter)
    ats_slugs: dict[str, str] = field(default_factory=dict)   # slug -> source
    messages_scanned: int = 0
    messages_matched: int = 0

    def ranked_names(self, min_mentions: int = 1) -> list[tuple[str, int]]:
        """Names by how often they appeared. Repetition is the confidence signal."""
        return [(n, c) for n, c in self.names.most_common() if c >= min_mentions]

    def summary(self) -> str:
        return (
            f"{self.messages_scanned} messages scanned, {self.messages_matched} job-related; "
            f"{len(self.names)} company names, {len(self.ats_slugs)} confirmed ATS boards"
        )


def _decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def _body_text(message) -> str:
    """Readable text from a message, HTML tags stripped."""
    parts: list[str] = []
    try:
        for part in message.walk() if message.is_multipart() else [message]:
            if part.get_content_maintype() != "text":
                continue
            try:
                payload = part.get_content()
            except Exception:
                raw = part.get_payload(decode=True)
                payload = raw.decode("utf-8", "replace") if raw else ""
            if part.get_content_subtype() == "html":
                payload = re.sub(r"<[^>]+>", " ", payload)
            parts.append(payload)
    except Exception as exc:  # a malformed message must not stop the scan
        log.debug("Could not read a message body: %s", exc)
    text = "\n".join(parts)
    return re.sub(r"[ \t\xa0]+", " ", text)


def is_job_related(sender: str, subject: str) -> bool:
    lowered_sender = (sender or "").lower()
    if any(domain in lowered_sender for domain in JOB_SENDERS):
        return True
    lowered_subject = (subject or "").lower()
    return any(term in lowered_subject for term in JOB_SUBJECT_TERMS)


def clean_company(raw: str) -> str | None:
    """Normalise a captured name, or None if it is not a company."""
    name = re.sub(r"\s+", " ", (raw or "")).strip(" .,:;|-–—")
    name = re.sub(r"['’]s$", "", name)
    if not 2 <= len(name) <= 60:
        return None
    words = name.split()
    if len(words) > 4:
        return None
    if all(w.lower() in STOPWORDS for w in words):
        return None
    if words[0].lower() in STOPWORDS and len(words) == 1:
        return None
    if not re.search(r"[A-Za-z]", name):
        return None
    # ALL-CAPS shouting ("APPLY NOW") is not a company name; real acronyms
    # (IBM, SAP) are short, so keep those.
    if name.isupper() and len(name) > 5:
        return None
    return name


def extract_from_message(subject: str, body: str, findings: MailboxFindings) -> None:
    """Pull company names and ATS slugs out of one message."""
    haystack = f"{subject}\n{body}"

    for pattern, source in ATS_URL_PATTERNS:
        for slug in pattern.findall(haystack):
            slug = slug.lower().strip("-_")
            if slug and slug not in ("embed", "api", "static", "assets"):
                findings.ats_slugs[slug] = source

    # The subject line is denser and cleaner than the body, so weight it.
    for pattern in COMPANY_PATTERNS:
        for match in pattern.findall(subject):
            name = clean_company(match)
            if name:
                findings.names[name] += 2
        for match in pattern.findall(body[:4000]):
            name = clean_company(match)
            if name:
                findings.names[name] += 1


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def scan_mbox(path: Path | str, limit: int = 0) -> MailboxFindings:
    """Scan a .mbox export (Google Takeout). Entirely offline."""
    import mailbox as mailbox_mod

    findings = MailboxFindings()
    box = mailbox_mod.mbox(str(path), factory=None)
    for message in box:
        findings.messages_scanned += 1
        subject = _decode(message.get("Subject"))
        sender = _decode(message.get("From"))
        if not is_job_related(sender, subject):
            continue
        findings.messages_matched += 1
        extract_from_message(subject, _body_text(message), findings)
        if limit and findings.messages_matched >= limit:
            break
    return findings


def scan_imap(
    email_address: str,
    app_password: str,
    host: str = "imap.gmail.com",
    folder: str = "INBOX",
    since: str | None = None,
    limit: int = 0,
    progress=None,
) -> MailboxFindings:
    """Scan a mailbox over IMAP, read-only.

    `app_password` is a Google App Password, never the account password.
    The connection is opened readonly, so nothing can be modified or deleted,
    and the password is not written anywhere by this function.
    """
    findings = MailboxFindings()
    connection = imaplib.IMAP4_SSL(host)
    try:
        connection.login(email_address, app_password)
        connection.select(folder, readonly=True)

        uids = _search_job_mail(connection, since)
        if limit:
            uids = uids[-limit:]  # newest first — a partial scan gets fresh data

        for index, uid in enumerate(uids, start=1):
            findings.messages_scanned += 1
            try:
                status, data = connection.fetch(uid, "(RFC822)")
                if status != "OK" or not data or not isinstance(data[0], tuple):
                    continue
                message = email.message_from_bytes(data[0][1], policy=email.policy.default)
            except Exception as exc:
                log.debug("Skipping message %s: %s", uid, exc)
                continue

            subject = _decode(message.get("Subject"))
            sender = _decode(message.get("From"))
            if not is_job_related(sender, subject):
                continue
            findings.messages_matched += 1
            extract_from_message(subject, _body_text(message), findings)

            if progress and index % 25 == 0:
                progress(index, len(uids), findings)
        return findings
    finally:
        try:
            connection.close()
        except Exception:
            pass
        connection.logout()


def _search_job_mail(connection, since: str | None) -> list[bytes]:
    """UIDs worth fetching.

    Searching server-side matters: fetching an entire mailbox to filter locally
    would download gigabytes to find a few hundred useful messages.
    """
    date_clause = ["SINCE", since] if since else []
    uids: list[bytes] = []
    seen: set[bytes] = set()

    for domain in JOB_SENDERS:
        try:
            status, data = connection.search(None, *date_clause, "FROM", domain)
        except Exception as exc:
            log.debug("IMAP search failed for %s: %s", domain, exc)
            continue
        if status == "OK" and data and data[0]:
            for uid in data[0].split():
                if uid not in seen:
                    seen.add(uid)
                    uids.append(uid)

    # Recruiters mail from their own domains, so also sweep by subject.
    for term in ("job", "role", "opportunity", "hiring", "interview", "application"):
        try:
            status, data = connection.search(None, *date_clause, "SUBJECT", term)
        except Exception:
            continue
        if status == "OK" and data and data[0]:
            for uid in data[0].split():
                if uid not in seen:
                    seen.add(uid)
                    uids.append(uid)

    return sorted(uids, key=lambda u: int(u))


def write_findings(
    findings: MailboxFindings, names_path: Path | str, min_mentions: int = 1
) -> tuple[Path, int]:
    """Write harvested names as a discovery input file."""
    names_path = Path(names_path)
    names_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Company names harvested from a mailbox.",
        f"# {findings.summary()}",
        "# Ordered by how often each appeared — repetition is the confidence signal.",
        "",
    ]
    for name, count in findings.ranked_names(min_mentions):
        lines.append(f"{name}    # seen {count}x" if count > 1 else name)

    names_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return names_path, len(findings.ranked_names(min_mentions))
