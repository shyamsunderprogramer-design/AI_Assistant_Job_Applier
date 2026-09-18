"""Ordering applications by freshness — offline, no network, no browser.

A posting's age is the strongest thing we know about its odds: a recruiter
reads the first applications to arrive and skims the rest. So these tests pin
that freshness beats score, which is the one ordering rule that is easy to get
backwards and expensive to get wrong.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from apply.queue import BAND_NAMES, build_queue, freshness_band, summarise

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


@dataclass
class FakeJob:
    id: int = 1
    company: str = "Acme"
    title: str = "DevOps Engineer"
    application_url: str = "https://job-boards.greenhouse.io/acme/jobs/1"
    source: str = "greenhouse"
    status: str = "Not Applied"
    is_open: bool = True
    ats_match_score: float | None = 0.80
    posted_at: datetime | None = None
    found_at: datetime | None = None


def ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


# -- the bands --------------------------------------------------------------

def test_a_posting_from_this_morning_is_the_freshest_band():
    assert freshness_band(ago(hours=3), None, NOW) == 0


def test_the_bands_step_down_with_age():
    assert freshness_band(ago(days=2), None, NOW) == 1
    assert freshness_band(ago(days=5), None, NOW) == 2
    assert freshness_band(ago(days=40), None, NOW) == 3


def test_a_posting_with_no_date_but_seen_today_sits_below_dated_recent_ones():
    """Some boards never say when a posting went up. "It appeared in today's
    scrape" is real evidence, just weaker than a stated date."""
    assert freshness_band(None, ago(hours=2), NOW) == 4
    assert freshness_band(None, ago(days=9), NOW) == 5


def test_a_naive_timestamp_is_treated_as_utc_not_rejected():
    assert freshness_band(datetime(2026, 9, 15, 9, 0), None, NOW) == 0


# -- the ordering rule ------------------------------------------------------

def test_fresh_beats_a_better_score():
    """The rule that is easy to get backwards: a 95% match posted a month ago
    is worth less than an 80% match posted this morning, because four hundred
    CVs are already in front of it."""
    stale_great = FakeJob(id=1, company="Globex", ats_match_score=0.95,
                          posted_at=ago(days=30))
    fresh_good = FakeJob(id=2, company="Initech", ats_match_score=0.80,
                         posted_at=ago(hours=2))

    queue = build_queue([stale_great, fresh_good], limit=10, now=NOW)

    assert [c.job_id for c in queue] == [2, 1]


def test_score_still_decides_within_one_band():
    weaker = FakeJob(id=1, company="Globex", ats_match_score=0.70,
                     posted_at=ago(hours=5))
    better = FakeJob(id=2, company="Initech", ats_match_score=0.92,
                     posted_at=ago(hours=6))

    queue = build_queue([weaker, better], limit=10, now=NOW)

    assert [c.job_id for c in queue] == [2, 1]


def test_an_unscored_posting_sorts_last_within_its_band():
    scored = FakeJob(id=1, company="Globex", ats_match_score=0.60,
                     posted_at=ago(hours=1))
    unscored = FakeJob(id=2, company="Initech", ats_match_score=None,
                       posted_at=ago(hours=1))

    assert [c.job_id for c in build_queue([unscored, scored], limit=10, now=NOW)] == [1, 2]


# -- what never reaches the queue ------------------------------------------

def test_an_applied_job_is_never_queued_again():
    assert build_queue([FakeJob(status="Applied")], limit=10, now=NOW) == []


def test_a_closed_posting_is_not_queued():
    assert build_queue([FakeJob(is_open=False)], limit=10, now=NOW) == []


def test_a_board_with_no_filler_is_not_queued():
    """Queuing a Workday job while only Greenhouse can be filled would promise
    an application that cannot be made."""
    assert build_queue([FakeJob(source="workday")], limit=10, now=NOW) == []


def test_a_posting_with_nowhere_to_apply_is_not_queued():
    assert build_queue([FakeJob(application_url="")], limit=10, now=NOW) == []


def test_a_weak_match_can_be_excluded():
    jobs = [FakeJob(id=1, ats_match_score=0.40), FakeJob(id=2, ats_match_score=0.85)]
    queue = build_queue(jobs, limit=10, min_score=0.60, now=NOW)
    assert [c.job_id for c in queue] == [2]


def test_the_daily_cap_is_the_last_word():
    jobs = [FakeJob(id=i, company=f"Company{i}", posted_at=ago(hours=i))
            for i in range(1, 60)]
    assert len(build_queue(jobs, limit=30, now=NOW)) == 30


# -- saying what is about to happen ----------------------------------------

def test_a_run_can_describe_itself_before_it_starts():
    jobs = [FakeJob(id=1, company="Globex", posted_at=ago(hours=2)),
            FakeJob(id=2, company="Initech", posted_at=ago(hours=5)),
            FakeJob(id=3, company="Umbrella", posted_at=ago(days=2))]

    text = summarise(build_queue(jobs, limit=10, now=NOW))

    assert "2 under a day" in text and "1 1-3 days" in text


def test_an_empty_queue_says_so_plainly():
    assert summarise([]) == "nothing to apply to"


def test_every_band_has_a_name():
    assert set(BAND_NAMES) == {0, 1, 2, 3, 4, 5}


# -- one application per employer ------------------------------------------

def test_only_the_best_role_at_a_company_is_queued():
    """The one pattern an employer can reliably see is several applications
    arriving at THEIR board together. SpaceX alone has 74 open matches; without
    this, the queue would send seventy-four."""
    jobs = [
        FakeJob(id=1, company="SpaceX", ats_match_score=0.70, posted_at=ago(hours=2)),
        FakeJob(id=2, company="SpaceX", ats_match_score=0.93, posted_at=ago(hours=2)),
        FakeJob(id=3, company="SpaceX", ats_match_score=0.85, posted_at=ago(hours=2)),
    ]

    queue = build_queue(jobs, limit=10, now=NOW)

    assert len(queue) == 1
    assert queue[0].job_id == 2            # the best one, not the first seen


def test_freshness_still_decides_which_role_represents_a_company():
    """Within a company, the same rule applies: a fresher posting wins even
    with a lower score."""
    jobs = [
        FakeJob(id=1, company="Acme", ats_match_score=0.95, posted_at=ago(days=30)),
        FakeJob(id=2, company="Acme", ats_match_score=0.72, posted_at=ago(hours=3)),
    ]

    assert [c.job_id for c in build_queue(jobs, limit=10, now=NOW)] == [2]


def test_different_companies_are_all_kept():
    jobs = [FakeJob(id=1, company="Acme"), FakeJob(id=2, company="Globex"),
            FakeJob(id=3, company="Initech")]
    assert len(build_queue(jobs, limit=10, now=NOW)) == 3


def test_company_names_match_regardless_of_case_or_spacing():
    jobs = [FakeJob(id=1, company="cloudflare", posted_at=ago(hours=1)),
            FakeJob(id=2, company="  Cloudflare ", posted_at=ago(hours=1))]
    assert len(build_queue(jobs, limit=10, now=NOW)) == 1


def test_a_company_applied_to_an_hour_ago_waits():
    """Not "never again" — just not yet. Half a day between applications to the
    same employer, so a company with several good roles hears from you this
    afternoon and again tomorrow, rather than five times in a minute."""
    jobs = [FakeJob(id=1, company="Acme"), FakeJob(id=2, company="Globex")]

    queue = build_queue(jobs, limit=10, last_applied={"acme": ago(hours=1)}, now=NOW)

    assert [c.company for c in queue] == ["Globex"]


def test_the_same_company_comes_round_again_after_the_cooldown():
    """The point of spacing rather than excluding: a company with three good
    roles eventually receives three applications, one at a time."""
    jobs = [FakeJob(id=1, company="Acme"), FakeJob(id=2, company="Globex")]

    queue = build_queue(jobs, limit=10, last_applied={"acme": ago(hours=13)}, now=NOW)

    assert {c.company for c in queue} == {"Acme", "Globex"}


def test_the_cooldown_length_is_tunable():
    jobs = [FakeJob(id=1, company="Acme")]

    assert build_queue(jobs, limit=10, last_applied={"acme": ago(hours=20)},
                       cooldown_hours=24, now=NOW) == []
    assert len(build_queue(jobs, limit=10, last_applied={"acme": ago(hours=20)},
                           cooldown_hours=12, now=NOW)) == 1


def test_a_company_never_applied_to_is_free():
    jobs = [FakeJob(id=1, company="Acme")]
    assert len(build_queue(jobs, limit=10, last_applied={"acme": None}, now=NOW)) == 1
    assert len(build_queue(jobs, limit=10, last_applied={}, now=NOW)) == 1


def test_the_per_company_limit_can_be_raised_deliberately():
    jobs = [FakeJob(id=i, company="Acme", ats_match_score=0.9 - i / 100)
            for i in range(1, 6)]
    assert len(build_queue(jobs, limit=10, max_per_company=2, now=NOW)) == 2


def test_the_daily_cap_counts_companies_not_postings():
    """With one per company, a cap of 30 means thirty employers."""
    jobs = []
    for c in range(40):
        for r in range(3):
            jobs.append(FakeJob(id=c * 10 + r, company=f"Company{c}",
                                posted_at=ago(hours=r + 1)))

    queue = build_queue(jobs, limit=30, now=NOW)

    assert len(queue) == 30
    assert len({c.company for c in queue}) == 30


# -- a score measured against a title is not the same number ----------------

def test_score_basis_tells_a_posting_from_a_headline():
    """A job-alert email carries ~217 characters; a scraped posting ~6,758.
    Scoring the first against a resume lands in the twenties however well it
    fits, and presenting that beside a real score buries good jobs."""
    from resume.scorer import score_basis

    assert score_basis("x" * 6000) == "full"
    assert score_basis(
        "DevOps Engineer at Zoom\nLocation: Remote\nSalary: $98,900 - $228,700"
    ) == "title"
    assert score_basis("") == "title"
    assert score_basis(None) == "title"


# -- where a posting is, in three answers not two ---------------------------

def test_a_us_city_and_state_is_recognised():
    """The first bug: requiring the words "united states" threw away every
    posting that said only "Boston, MA" — 0 of 3,590 university campuses
    survived it."""
    from locations import is_us

    for place in ("Boston, MA", "Ann Arbor, Michigan", "Fayetteville, AR",
                  "Austin, TX 78701", "Remote - US", "US Remote"):
        assert is_us(place) is True, place


def test_a_bare_us_city_is_recognised():
    """Boards print these with no state, and there are 74 of them in one
    snapshot. Without a city list they read as foreign."""
    from locations import is_us

    for place in ("San Francisco", "Chicago", "Austin", "Plano",
                  "New York City | OnSite"):
        assert is_us(place) is True, place


def test_a_foreign_city_is_rejected_even_without_its_country():
    """The second bug, in the opposite direction. "Kfar Saba" ranked first at
    94% because the exclusion list was country-level and the posting never
    said Israel."""
    from locations import is_us
    from resume.profile import NON_US_PLACES

    assert is_us("Kfar Saba", NON_US_PLACES) is False
    assert is_us("Getafe Area", NON_US_PLACES) is False
    assert is_us("Rome Metropolitain Area | Remote", NON_US_PLACES) is False


def test_an_unknowable_location_is_kept_not_guessed():
    """"Remote" is silence, not evidence of being abroad. Dropping these was
    the original bug and must not come back through the other door."""
    from locations import is_us

    for place in ("Remote", "Hybrid", "In-Office", "", None):
        assert is_us(place) is None, place
