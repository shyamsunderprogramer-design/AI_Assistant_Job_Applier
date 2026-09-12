"""Mailbox harvesting tests — offline, no credentials, no network.

Two failure modes matter here and they pull in opposite directions. Missing a
real company costs one board. Inventing one costs a wasted probe, a junk row in
the company list, and a name that looks real enough to be trusted later. The
rejection cases below are therefore the bulk of this file.
"""

import pytest

from scraper import mailbox
from scraper.mailbox import (
    MailboxFindings,
    clean_company,
    extract_from_message,
    is_job_related,
    write_findings,
)


def harvest(*messages) -> MailboxFindings:
    findings = MailboxFindings()
    for subject, body in messages:
        extract_from_message(subject, body, findings)
    return findings


# -- which mail is even worth reading --------------------------------------

def test_known_job_senders_are_job_related():
    for sender in ("jobalerts-noreply@linkedin.com", "no-reply@indeed.com",
                   "hi@jobs.ashbyhq.com"):
        assert is_job_related(sender, "anything")


def test_subject_terms_catch_recruiters_on_their_own_domain():
    """Recruiters mail from their own domains, so the sender list misses them."""
    assert is_job_related("jane@someagency.co", "Exciting role for you")


def test_unrelated_mail_is_skipped():
    assert not is_job_related("billing@utilities.com", "Your October statement")


# -- extraction that should work -------------------------------------------

def test_role_at_company():
    names = dict(harvest(("Senior SRE at Stripe", "")).ranked_names())
    assert "Stripe" in names


def test_application_confirmation():
    names = dict(harvest(("Your application to Datadog was received", "")).ranked_names())
    assert "Datadog" in names


def test_interest_phrasing():
    names = dict(harvest(("", "Thanks for your interest in Snowflake")).ranked_names())
    assert "Snowflake" in names


def test_company_is_hiring():
    names = dict(harvest(("", "Ramp is hiring DevOps Engineers")).ranked_names())
    assert "Ramp" in names


def test_recruiting_team_phrasing():
    names = dict(harvest(("", "A message from the Cloudflare recruiting team")).ranked_names())
    assert "Cloudflare" in names


def test_a_digest_listing_several_companies():
    """Chained mentions are how weekly digests read, and a trailing-punctuation
    requirement missed every one of them."""
    names = dict(harvest(("Your jobs digest", "Roles at Figma and at MongoDB this week")).ranked_names())
    assert "Figma" in names and "MongoDB" in names


def test_multi_word_company_names():
    names = dict(harvest(("Platform Engineer at Grafana Labs", "")).ranked_names())
    assert "Grafana Labs" in names


def test_subject_mentions_outweigh_body_mentions():
    """A subject line is denser and cleaner than a marketing email body."""
    findings = harvest(("Engineer at Stripe", "some text at Datadog here"))
    assert findings.names["Stripe"] > findings.names["Datadog"]


def test_repetition_accumulates_across_messages():
    findings = harvest(("Role at Vercel", ""), ("Another role at Vercel", ""))
    assert findings.names["Vercel"] >= 4


# -- ATS URLs: the slug itself, not a guess at one --------------------------

def test_greenhouse_url_yields_a_slug():
    findings = harvest(("", "Apply: https://boards.greenhouse.io/stripe/jobs/123"))
    assert findings.ats_slugs["stripe"] == "greenhouse"


def test_job_boards_greenhouse_domain_also_works():
    findings = harvest(("", "https://job-boards.greenhouse.io/anthropic/jobs/9"))
    assert findings.ats_slugs["anthropic"] == "greenhouse"


def test_lever_and_ashby_urls():
    findings = harvest(("", "https://jobs.lever.co/palantir/x https://jobs.ashbyhq.com/ramp/y"))
    assert findings.ats_slugs["palantir"] == "lever"
    assert findings.ats_slugs["ramp"] == "ashby"


def test_ats_url_path_noise_is_ignored():
    findings = harvest(("", "https://boards.greenhouse.io/embed/job_board?for=x"))
    assert "embed" not in findings.ats_slugs


# -- rejections: where a wrong answer is expensive --------------------------

def test_marketing_shouting_is_not_a_company():
    assert clean_company("APPLY NOW TODAY") is None


def test_short_acronyms_survive_the_caps_rule():
    """IBM and SAP are real; "APPLY NOW TODAY" is not. Length separates them."""
    assert clean_company("IBM") == "IBM"


def test_ui_chrome_is_not_a_company():
    for junk in ("Click Here", "View All", "Unsubscribe", "Your Job"):
        assert clean_company(junk) is None, junk


def test_weekdays_and_months_are_not_companies():
    for junk in ("Monday", "January"):
        assert clean_company(junk) is None, junk


def test_job_vocabulary_alone_is_not_a_company():
    for junk in ("Senior Engineer", "Remote Role", "Full Time"):
        assert clean_company(junk) is None, junk


def test_the_job_boards_themselves_are_not_companies():
    for junk in ("LinkedIn", "Indeed", "Glassdoor"):
        assert clean_company(junk) is None, junk


def test_a_whole_sentence_is_not_a_company():
    assert clean_company("We Are Excited To Share This Opportunity With You") is None


def test_empty_and_punctuation_only():
    for junk in ("", "   ", "-", "..."):
        assert clean_company(junk) is None, repr(junk)


def test_possessive_is_stripped():
    assert clean_company("Stripe's") == "Stripe"


def test_trailing_punctuation_is_stripped():
    assert clean_company("Datadog,") == "Datadog"


def test_a_noisy_marketing_email_yields_nothing():
    findings = harvest(("APPLY NOW - 50 NEW JOBS", "Click here to view all jobs. Unsubscribe."))
    assert findings.ranked_names() == []


# -- output ----------------------------------------------------------------

def test_findings_are_written_ranked(tmp_path):
    findings = harvest(("Role at Vercel", ""), ("Role at Vercel", ""), ("Role at Netlify", ""))
    path, count = write_findings(findings, tmp_path / "names.txt")

    lines = [ln for ln in path.read_text().splitlines() if ln and not ln.startswith("#")]
    assert count == 2
    assert lines[0].startswith("Vercel")  # most-mentioned first


def test_min_mentions_filters_one_offs(tmp_path):
    findings = harvest(("Role at Vercel", ""), ("Role at Vercel", ""), ("Role at Netlify", ""))
    _, count = write_findings(findings, tmp_path / "names.txt", min_mentions=3)
    assert count == 1


def test_written_file_is_readable_by_discovery(tmp_path):
    from scraper.discovery import load_names_from_file

    findings = harvest(("Role at Vercel", ""))
    path, _ = write_findings(findings, tmp_path / "names.txt")
    assert "Vercel" in load_names_from_file(path)[0]


def test_summary_reports_counts():
    findings = harvest(("Role at Stripe", "https://jobs.lever.co/ramp/x"))
    findings.messages_scanned = 10
    assert "10 messages scanned" in findings.summary()


# -- checkpointing: an interrupted scan must not start over ----------------
#
# A full scan is tens of thousands of fetches over IMAP. Before checkpointing,
# a scan that died at 99% wrote nothing at all, so the whole run was repeated.

def fake_message(company: str) -> bytes:
    return (
        f"From: recruiter@{company.lower()}.com\r\n"
        f"Subject: job opportunity at {company}\r\n"
        f"\r\nWe are hiring at {company}.\r\n"
    ).encode()


class FakeIMAP:
    """The slice of imaplib that scan_imap actually uses."""

    def __init__(self, count: int, die_after: int | None = None):
        self.count = count
        self.die_after = die_after
        self.fetched: list[int] = []

    def login(self, *a):
        pass

    def select(self, *a, **k):
        pass

    def search(self, charset, *criteria):
        if "FROM" in criteria:
            return "OK", [b" ".join(str(i).encode() for i in range(1, self.count + 1))]
        return "OK", [b""]

    def fetch(self, uid, spec):
        if self.die_after is not None and len(self.fetched) >= self.die_after:
            raise KeyboardInterrupt("process killed")
        self.fetched.append(int(uid))
        return "OK", [(b"header", fake_message(f"Corp{int(uid)}"))]

    def close(self):
        pass

    def logout(self):
        pass


@pytest.fixture
def imap(monkeypatch):
    def install(conn):
        monkeypatch.setattr(mailbox.imaplib, "IMAP4_SSL", lambda host: conn)
        return conn
    return install


def test_state_round_trips(tmp_path):
    findings = harvest(("job at Acme", "Acme is hiring"))
    state = tmp_path / "s.json"
    mailbox.save_scan_state(state, findings, {1, 2, 3})

    restored, done = mailbox.load_scan_state(state)
    assert restored.names == findings.names
    assert done == {1, 2, 3}


def test_missing_state_starts_clean(tmp_path):
    findings, done = mailbox.load_scan_state(tmp_path / "absent.json")
    assert findings.messages_scanned == 0 and done == set()


def test_corrupt_state_is_ignored_not_fatal(tmp_path):
    state = tmp_path / "s.json"
    state.write_text("{ truncated", encoding="utf-8")

    findings, done = mailbox.load_scan_state(state)
    assert findings.messages_scanned == 0 and done == set()


def test_interrupted_scan_keeps_completed_work(tmp_path, imap):
    state = tmp_path / "s.json"
    conn = imap(FakeIMAP(60, die_after=25))

    with pytest.raises(KeyboardInterrupt):
        mailbox.scan_imap("a@b.c", "pw", state_path=state, checkpoint_every=10)

    saved, done = mailbox.load_scan_state(state)
    assert len(done) == 20        # the last checkpoint, not zero
    assert len(saved.names) == 20


def test_resumed_scan_refetches_nothing_already_done(tmp_path, imap):
    state = tmp_path / "s.json"
    imap(FakeIMAP(60, die_after=25))
    with pytest.raises(KeyboardInterrupt):
        mailbox.scan_imap("a@b.c", "pw", state_path=state, checkpoint_every=10)

    resumed = imap(FakeIMAP(60))
    findings = mailbox.scan_imap("a@b.c", "pw", state_path=state, checkpoint_every=10)

    assert resumed.fetched == list(range(21, 61))   # only the remainder
    assert findings.messages_scanned == 60


def test_second_scan_of_unchanged_mailbox_fetches_nothing(tmp_path, imap):
    state = tmp_path / "s.json"
    imap(FakeIMAP(10))
    mailbox.scan_imap("a@b.c", "pw", state_path=state)

    again = imap(FakeIMAP(10))
    mailbox.scan_imap("a@b.c", "pw", state_path=state)
    assert again.fetched == []


def test_scan_without_state_path_still_works(tmp_path, imap):
    conn = imap(FakeIMAP(5))
    findings = mailbox.scan_imap("a@b.c", "pw")
    assert conn.fetched == [1, 2, 3, 4, 5]
    assert findings.messages_scanned == 5


def test_search_phase_reports_progress(tmp_path, imap):
    imap(FakeIMAP(3))
    notes: list[str] = []
    mailbox.scan_imap("a@b.c", "pw", search_progress=notes.append)

    # The search phase is minutes of silence otherwise, indistinguishable
    # from a hang.
    assert len(notes) == 30
    assert "search 1/30" in notes[0]
