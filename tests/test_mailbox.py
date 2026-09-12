"""Mailbox harvesting tests — offline, no credentials, no network.

Two failure modes matter here and they pull in opposite directions. Missing a
real company costs one board. Inventing one costs a wasted probe, a junk row in
the company list, and a name that looks real enough to be trusted later. The
rejection cases below are therefore the bulk of this file.
"""

import pytest

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
