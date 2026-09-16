"""Pull job postings out of alert emails and store them like any other posting.

Reads the mailbox once, parses what Indeed's alerts contain, and writes the
results into the same `jobs` table as every scraped posting -- same scoring,
same filters, same web page, same application packets. The only difference is
`source = "indeed-email"`.

Why this matters more than it looks: these are jobs reachable no other way.
"DevOps Engineer @ Zoom" sat in this inbox while the scraper could not see it,
because Zoom uses Clinch and the scraper reads Greenhouse, Lever, Ashby and
Workday. Probing more company names would never have found it. The alert
already had it.

Read-only by construction, like `scraper/mailbox.py`: the IMAP session is
opened with readonly=True, so nothing here can delete, move or mark a message.

    python -m scraper.alert_import --since 30      # last 30 days
    python -m scraper.alert_import --dry-run       # parse, store nothing
"""

from __future__ import annotations

import argparse
import email
import hashlib
import imaplib
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from scraper.job_alerts import describe, parse_alert

IMAP_HOST = "imap.gmail.com"

# Senders whose alerts carry parseable postings. Others still go through the
# name harvester in scraper/mailbox.py; this is only about postings.
ALERT_SENDERS = ("indeed.com",)


def _body(message) -> str:
    parts = []
    for part in message.walk():
        if part.get_content_type() in ("text/html", "text/plain"):
            try:
                parts.append(part.get_payload(decode=True).decode("utf-8", "replace"))
            except Exception:
                continue
    return "\n".join(parts)


def _received(message) -> datetime | None:
    try:
        when = parsedate_to_datetime(message.get("Date"))
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _slug(company: str) -> str:
    """A stable per-company key, so lifecycle code can group these rows."""
    return re.sub(r"[^a-z0-9]+", "-", (company or "").lower()).strip("-") or "unknown"


def fetch_alerts(since_days: int = 30, limit: int = 0, senders=ALERT_SENDERS) -> list:
    """Every posting found in recent alert emails. Read-only."""
    address = os.getenv("MAIL_ADDRESS")
    password = os.getenv("MAIL_APP_PASSWORD")
    if not address or not password:
        raise RuntimeError(
            "MAIL_ADDRESS and MAIL_APP_PASSWORD must be set in .env.\n"
            "  The app password is 16 characters, usually written as four "
            "groups of four.")

    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%d-%b-%Y")
    found = []

    connection = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        connection.login(address, password)
        connection.select("INBOX", readonly=True)     # cannot alter anything
        for sender in senders:
            typ, data = connection.search(None, f'(FROM "{sender}" SINCE {since})')
            if typ != "OK":
                continue
            uids = data[0].split()
            if limit:
                uids = uids[-limit:]
            for uid in uids:
                typ, payload = connection.fetch(uid, "(RFC822)")
                if typ != "OK" or not payload or not payload[0]:
                    continue
                message = email.message_from_bytes(payload[0][1])
                job = parse_alert(str(message.get("Subject", "")),
                                  _body(message), _received(message))
                if job:
                    found.append(job)
    finally:
        try:
            connection.logout()
        except Exception:
            pass
    return found


def store(jobs: list) -> tuple[int, int]:
    """Write postings to the jobs table. Returns (new, already had)."""
    from db.models import Job, utcnow
    from db.session import get_session

    new = seen = 0
    with get_session() as session:
        for job in jobs:
            existing = (
                session.query(Job)
                .filter_by(source=job.source, external_id=job.external_id)
                .one_or_none()
            )
            if existing is not None:
                # Already known. Refresh last_seen so lifecycle does not treat
                # a posting that is still being advertised as gone.
                existing.last_seen_at = utcnow()
                seen += 1
                continue

            description = describe(job)
            session.add(Job(
                source=job.source,
                company=job.company,
                company_slug=_slug(job.company),
                external_id=job.external_id,
                title=job.title,
                location=job.location,
                description=description,
                requirements=description,
                application_url=job.url,
                posted_at=job.posted_at,
                content_hash=hashlib.sha256(
                    f"{job.source}{job.external_id}{job.title}".encode()).hexdigest(),
                is_open=True,
            ))
            new += 1
    return new, seen


def run(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", type=int, default=30, help="Days back to read")
    ap.add_argument("--limit", type=int, default=0, help="Newest N per sender")
    ap.add_argument("--dry-run", action="store_true", help="Parse, store nothing")
    args = ap.parse_args(argv[1:])

    from config.loader import load_config
    from db.session import init_engine

    cfg = load_config()
    init_engine(cfg.database_url)

    print(f"Reading alert emails from the last {args.since} days...")
    jobs = fetch_alerts(args.since, args.limit)
    print(f"{len(jobs)} postings parsed\n")

    for job in jobs[:15]:
        bits = " · ".join(x for x in (job.location, job.salary, job.job_type) if x)
        print(f"  {job.title[:40]:42} {job.company[:22]:24} {bits[:46]}")
    if len(jobs) > 15:
        print(f"  ... and {len(jobs) - 15} more")

    if args.dry_run:
        print("\n--dry-run: nothing stored.")
        return 0

    new, seen = store(jobs)
    print(f"\nStored: {new} new, {seen} already had.")
    if new:
        print("Next: python main.py score      (to rank them against your resume)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
