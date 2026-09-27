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

Read-only by construction, like `data_engineering/scraper/mailbox.py`: the IMAP session is
opened with readonly=True, so nothing here can delete, move or mark a message.

    python -m data_engineering.scraper.alert_import --since 30      # last 30 days
    python -m data_engineering.scraper.alert_import --dry-run       # parse, store nothing
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

from data_engineering.scraper.job_alerts import describe, parse_alert

IMAP_HOST = "imap.gmail.com"

# Senders whose alerts carry parseable postings. Others still go through the
# name harvester in data_engineering/scraper/mailbox.py; this is only about postings.
from data_engineering.scraper.alert_parsers import PARSERS

# Indeed: one job per email (parse_alert). The rest carry many per email.
ALERT_SENDERS = ("indeed.com", *PARSERS)


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
    box = _Mailbox(address, password)
    try:
        for sender in senders:
            uids = box.search(f'(FROM "{sender}" SINCE {since})')
            if limit:
                uids = uids[-limit:]
            many = PARSERS.get(sender)
            for uid in uids:
                raw = box.fetch(uid)
                if raw is None:
                    continue
                message = email.message_from_bytes(raw)
                if many is not None:          # Dice, Glassdoor, ZipRecruiter, Monster: many jobs per email
                    found.extend(many(_body(message), _received(message)))
                    continue
                job = parse_alert(str(message.get("Subject", "")),
                                  _body(message), _received(message))
                if job:
                    found.append(job)
    finally:
        box.close()
    return found


class _Mailbox:
    """A read-only IMAP session that survives Gmail dropping the connection.

    Reading a month of alerts means a few hundred fetches, and Gmail closes a
    busy connection now and then ("socket error: EOF"), which used to lose the
    whole run. UIDs are used rather than sequence numbers so that a reconnect
    carries on at the same message; each fetch is retried after reconnecting.
    """

    RETRIES = 3

    def __init__(self, address: str, password: str):
        self.address, self.password, self.conn = address, password, None
        self._connect()

    def _connect(self) -> None:
        self.close()
        self.conn = imaplib.IMAP4_SSL(IMAP_HOST, timeout=30)
        self.conn.login(self.address, self.password)
        self.conn.select("INBOX", readonly=True)     # cannot alter anything

    def _retry(self, action):
        import time
        for attempt in range(self.RETRIES):
            try:
                return action()
            except (imaplib.IMAP4.abort, OSError):
                if attempt == self.RETRIES - 1:
                    raise
                time.sleep(2 * (attempt + 1))
                self._connect()

    def search(self, criteria: str) -> list[bytes]:
        typ, data = self._retry(lambda: self.conn.uid("SEARCH", None, criteria))
        return data[0].split() if typ == "OK" and data and data[0] else []

    def fetch(self, uid: bytes) -> bytes | None:
        typ, payload = self._retry(lambda: self.conn.uid("FETCH", uid, "(BODY.PEEK[])"))
        if typ != "OK" or not payload or not isinstance(payload[0], tuple):
            return None
        return payload[0][1]

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None


def store(jobs: list) -> tuple[int, int]:
    """Write postings to the jobs table. Returns (new, already had)."""
    from data_engineering.db.models import Job, utcnow
    from data_engineering.db.session import get_session

    new = seen = 0
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    with get_session() as session:
        # The same posting reaches the inbox from several sites (Leidos arrives
        # from both Glassdoor and ZipRecruiter) and is often already on a board
        # this tool scrapes. One row is enough; the board's copy is the better one.
        elsewhere = {(src, norm(c), norm(ti)) for src, c, ti in
                     session.query(Job.source, Job.company, Job.title).filter(Job.is_open.is_(True)).all()}
        for job in jobs:
            if any(src != job.source and c == norm(job.company) and ti == norm(job.title)
                   for src, c, ti in elsewhere):
                continue
            elsewhere.add((job.source, norm(job.company), norm(job.title)))
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

    from backend.config.loader import load_config
    from data_engineering.db.session import init_engine

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
