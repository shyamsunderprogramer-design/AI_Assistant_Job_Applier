"""The application history keeps days as they were where the person is (A1.1.1, 10 Nov 2026). Central Time, with a
controlled clock (conftest `local_clock`): from 6 pm (7 pm in summer) the UTC date is already tomorrow, and an
application made that evening was compared by its UTC date -- that same evening's step was refused as "before you
applied". Made-up jobs in a database of the test's own."""

from datetime import datetime, timezone

import pytest

from backend.apply import tracker
from data_engineering.db.models import Job
from data_engineering.db.session import get_session
from qa.tests.test_marked_applied import db  # noqa: F401

UTC = timezone.utc


def applied_at(job_id, instant):
    with get_session() as s:
        j = s.get(Job, job_id)
        j.applied_at, j.status = instant, "Applied"


@pytest.mark.parametrize("now,applied,local_day", [
    (datetime(2026, 11, 11, 1, 30, tzinfo=UTC), datetime(2026, 11, 11, 1, 0, tzinfo=UTC), "2026-11-10"),  # 7:30 pm CST
    (datetime(2026, 11, 11, 5, 59, 59, tzinfo=UTC), datetime(2026, 11, 11, 5, 58, tzinfo=UTC), "2026-11-10"),  # 11:59 pm
    (datetime(2026, 11, 11, 0, 0, 0, tzinfo=UTC), datetime(2026, 11, 11, 0, 0, 0, tzinfo=UTC), "2026-11-10"),  # 6 pm
    (datetime(2026, 7, 15, 0, 30, tzinfo=UTC), datetime(2026, 7, 15, 0, 15, tzinfo=UTC), "2026-07-14"),  # 7:30 pm CDT
])
def test_a_step_on_the_evening_you_applied_is_kept(db, local_clock, now, applied, local_day):
    assert local_clock(now).isoformat() == local_day
    applied_at(2, applied)
    tracker.add_stage(2, "recruiter_screen", on=local_day)
    assert tracker._stages()["2"][0]["at"] == local_day


def test_the_day_before_you_applied_is_still_refused(db, local_clock):
    local_clock(datetime(2026, 11, 11, 1, 30, tzinfo=UTC))               # 7:30 pm, 10 Nov, Central
    applied_at(2, datetime(2026, 11, 11, 1, 0, tzinfo=UTC))              # applied 7 pm, 10 Nov
    with pytest.raises(ValueError, match="before you applied"):
        tracker.add_stage(2, "recruiter_screen", on="2026-11-09")


def test_tomorrow_is_refused_and_today_is_not_in_the_evening(db, local_clock):
    local_clock(datetime(2026, 11, 11, 1, 30, tzinfo=UTC))               # 7:30 pm, 10 Nov; UTC says 11 Nov
    with pytest.raises(ValueError, match="future"):
        tracker.add_stage(3, "recruiter_screen", on="2026-11-11")
    tracker.add_stage(3, "recruiter_screen", on="2026-11-10")


def test_just_after_midnight_the_new_day_is_today(db, local_clock):
    local_clock(datetime(2026, 11, 11, 6, 0, 1, tzinfo=UTC))             # 12:00:01 am, 11 Nov, Central
    applied_at(2, datetime(2026, 11, 11, 5, 59, tzinfo=UTC))             # 11:59 pm, 10 Nov
    tracker.add_stage(2, "recruiter_screen", on="2026-11-10")
    tracker.add_stage(2, "recruiter_screen", on="2026-11-11")
    with pytest.raises(ValueError, match="future"):
        tracker.add_stage(2, "recruiter_screen", on="2026-11-12")


def test_the_days_since_you_applied_count_local_days(db, local_clock):
    local_clock(datetime(2026, 11, 11, 1, 30, tzinfo=UTC))               # 7:30 pm, 10 Nov
    applied_at(2, datetime(2026, 11, 11, 1, 0, tzinfo=UTC))              # half an hour ago, the same local day
    assert tracker.application(2)["days"] == 0
