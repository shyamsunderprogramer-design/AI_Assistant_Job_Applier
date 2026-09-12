"""Posting-age tests — pure functions, no clock of their own.

Every case pins `now`, because a test that reads the real clock passes at
3pm and fails at midnight.
"""

from datetime import datetime, timedelta, timezone

from jobage import age_days, age_label, age_of, humanize

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


class FakeJob:
    """Just the two date fields the helper reads."""

    def __init__(self, posted_at=None, found_at=None):
        self.posted_at = posted_at
        self.found_at = found_at


def ago(**kw) -> timedelta:
    return timedelta(**kw)


# -- the scale, unit by unit ------------------------------------------------

def test_seconds_read_as_just_now():
    assert humanize(ago(seconds=30)) == "just now"


def test_minutes():
    assert humanize(ago(minutes=1)) == "1 min"
    assert humanize(ago(minutes=42)) == "42 min"


def test_hours():
    assert humanize(ago(minutes=90)) == "1 hr"
    assert humanize(ago(hours=13)) == "13 hr"


def test_days_are_singular_at_one():
    assert humanize(ago(days=1)) == "1 day"
    assert humanize(ago(days=3)) == "3 days"


def test_weeks():
    assert humanize(ago(days=7)) == "1 wk"
    assert humanize(ago(days=21)) == "3 wks"


def test_months():
    assert humanize(ago(days=60)) == "1 mo"
    assert humanize(ago(days=210)) == "6 mo"


def test_years_keep_a_decimal_so_abandoned_reads_as_abandoned():
    assert humanize(ago(days=1994)) == "5.5 yr"
    assert humanize(ago(days=400)) == "1.1 yr"


def test_a_decade_drops_the_decimal():
    assert humanize(ago(days=4200)) == "11 yr"


def test_the_scale_is_continuous():
    # No gap should produce an empty or absurd label anywhere in the span.
    step = timedelta(seconds=45)
    while step < timedelta(days=4000):
        assert humanize(step).strip(), step
        step *= 2


# -- what counts as the posting's date --------------------------------------

def test_posted_at_is_preferred():
    job = FakeJob(posted_at=NOW - ago(days=3), found_at=NOW - ago(days=90))
    assert age_label(job, NOW) == "3 days"


def test_missing_posted_at_falls_back_to_when_we_found_it():
    # Some boards publish no date; first-seen is a floor, so the age is marked
    # approximate rather than stated as fact.
    job = FakeJob(posted_at=None, found_at=NOW - ago(days=10))
    assert age_label(job, NOW) == "~1 wk"


def test_a_stated_posting_date_is_never_marked_approximate():
    job = FakeJob(posted_at=NOW - ago(days=10))
    assert age_label(job, NOW) == "1 wk"


def test_no_date_at_all_is_blank_not_zero():
    job = FakeJob()
    assert age_of(job, NOW) is None
    assert age_label(job, NOW) == ""
    assert age_days(job, NOW) is None


def test_naive_timestamps_do_not_explode():
    # SQLite hands DateTime columns back naive; comparing them to an aware
    # `now` raises TypeError unless normalised first.
    job = FakeJob(posted_at=datetime(2026, 9, 5, 12, 0))
    assert age_label(job, NOW) == "1 wk"


def test_a_future_date_is_not_a_negative_age():
    job = FakeJob(posted_at=NOW + ago(days=2))
    assert age_label(job, NOW) == "just now"


# -- the number the filter uses ---------------------------------------------

def test_age_days_is_a_number_for_filtering():
    job = FakeJob(posted_at=NOW - ago(days=90))
    assert round(age_days(job, NOW)) == 90
