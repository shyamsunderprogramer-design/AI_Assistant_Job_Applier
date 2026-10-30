"""Ordering applications — offline, no network, no browser.

The person's order since 23 Oct 2026: the jobs they saved to the Watch list, then roles at
companies they watch, then the best fit of the rest; at the same fit, the fresher posting.
Until then freshness led and fit broke ties, so these tests pin the turn-around.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from backend.apply.queue import BAND_NAMES, build_queue, freshness_band, summarise

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

def test_the_best_fit_goes_first_whatever_its_age():
    """23 Oct 2026, "most fitted to jobs": a 95% match from a month ago goes before an 80% one
    from this morning. Until then it was the other way round."""
    month_old_great = FakeJob(id=1, company="Globex", ats_match_score=0.95,
                              posted_at=ago(days=30))
    fresh_good = FakeJob(id=2, company="Initech", ats_match_score=0.80,
                         posted_at=ago(hours=2))

    queue = build_queue([month_old_great, fresh_good], limit=10, now=NOW)

    assert [c.job_id for c in queue] == [1, 2]


def test_at_the_same_fit_the_fresher_posting_goes_first():
    """The same percent on the page, so freshness decides: early applications get read."""
    older = FakeJob(id=1, company="Globex", ats_match_score=0.874, posted_at=ago(days=20))
    newer = FakeJob(id=2, company="Initech", ats_match_score=0.871, posted_at=ago(hours=4))

    assert [c.job_id for c in build_queue([older, newer], limit=10, now=NOW)] == [2, 1]


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
    """Queuing a Lever job while only Greenhouse and Workday can be filled would
    promise an application that cannot be made."""
    assert build_queue([FakeJob(source="lever")], limit=10, now=NOW) == []
    assert len(build_queue([FakeJob(source="workday")], limit=10, now=NOW)) == 1


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


# -- the Watch list first ------------------------------------------------------
# 23 Oct 2026, the person: "auto apply selected means watchlist like and most fitted to jobs".

def test_saved_jobs_go_first_then_watched_companies_then_the_best_fit():
    best = FakeJob(id=1, company="Globex", ats_match_score=0.97)
    watched = FakeJob(id=2, company="Initech", ats_match_score=0.70)
    saved = FakeJob(id=3, company="Umbrella", ats_match_score=0.65)

    queue = build_queue([best, watched, saved], limit=10, saved={3}, watched={2}, now=NOW)

    assert [(c.job_id, c.group_name) for c in queue] == [
        (3, "saved by you"), (2, "at a company you watch"), (1, "best fit")]


def test_a_saved_job_goes_at_any_fit_but_a_watched_companys_role_needs_the_usual_fit():
    """The person chose a saved job; a watched company's weak match is still a weak match."""
    saved_weak = FakeJob(id=1, company="Globex", ats_match_score=0.40)
    watched_weak = FakeJob(id=2, company="Initech", ats_match_score=0.40)
    saved_unscored = FakeJob(id=3, company="Umbrella", ats_match_score=None)

    queue = build_queue([saved_weak, watched_weak, saved_unscored], limit=10, min_score=0.60,
                        saved={1, 3}, watched={2}, now=NOW)

    assert [c.job_id for c in queue] == [1, 3]


def test_every_other_rule_holds_for_a_saved_job():
    """Saved is not a pass: applied, closed, a board without a filler, past four months."""
    jobs = [FakeJob(id=1, company="A", status="Applied"), FakeJob(id=2, company="B", is_open=False),
            FakeJob(id=3, company="C", source="jobvite"), FakeJob(id=4, company="D", posted_at=ago(days=200))]
    assert build_queue(jobs, limit=10, saved={1, 2, 3, 4}, max_age_days=FOUR_MONTHS, now=NOW) == []


def test_a_saved_job_represents_its_company():
    jobs = [FakeJob(id=1, company="Acme", ats_match_score=0.95),
            FakeJob(id=2, company="Acme", ats_match_score=0.62)]
    assert [c.job_id for c in build_queue(jobs, limit=10, saved={2}, now=NOW)] == [2]


# -- how old a posting may be ------------------------------------------------
# 23 Oct 2026, the person: "I don't want more than 4 months job postings".

FOUR_MONTHS = 4 * 30.44


class Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def test_a_posting_past_four_months_is_not_queued():
    """Without the limit the best fit leads at any age, so a 95% match from a year ago went
    ahead of a 75% one from last week."""
    jobs = [FakeJob(id=1, company="Globex", ats_match_score=0.95, posted_at=ago(days=365)),
            FakeJob(id=2, company="Initech", ats_match_score=0.75, posted_at=ago(days=10)),
            FakeJob(id=3, company="Umbrella", ats_match_score=0.90, posted_at=ago(days=118))]

    assert [c.job_id for c in build_queue(jobs, limit=10, now=NOW)] == [1, 3, 2]
    assert [c.job_id for c in build_queue(jobs, limit=10, max_age_days=FOUR_MONTHS, now=NOW)] == [3, 2]


def test_an_undated_posting_counts_from_when_it_was_first_seen():
    """First seen five months ago and still up: five months old at least."""
    long_seen = FakeJob(id=1, company="Globex", found_at=ago(days=150))
    just_met = FakeJob(id=2, company="Initech", found_at=ago(days=2))

    assert [c.job_id for c in build_queue([long_seen, just_met], limit=10,
                                          max_age_days=FOUR_MONTHS, now=NOW)] == [2]


def test_a_posting_with_no_date_at_all_is_kept():
    """Not knowing a posting's age is not evidence that it is old."""
    assert len(build_queue([FakeJob()], limit=10, max_age_days=FOUR_MONTHS, now=NOW)) == 1


def test_the_limit_comes_from_config_and_zero_lifts_it():
    from pytest import approx

    from backend.apply.queue import age_limit_days

    assert age_limit_days(Cfg()) == approx(FOUR_MONTHS)                   # the default: 4
    assert age_limit_days(Cfg({"apply.max_age_months": 6})) == approx(6 * 30.44)
    assert age_limit_days(Cfg({"apply.max_age_months": 0})) is None


def test_a_posting_past_the_limit_says_how_old_in_days():
    """"4 mo, over the 4-month limit" would read as a contradiction: 124 days says it."""
    from backend.apply.queue import age_note

    assert age_note(FakeJob(posted_at=ago(days=124)), 4, NOW) == \
        "posted 124 days ago, over the 4-month limit"
    assert age_note(FakeJob(found_at=ago(days=150)), 4, NOW) == \
        "first seen 150 days ago, over the 4-month limit"


# -- saying what is about to happen ----------------------------------------

def test_a_run_can_describe_itself_before_it_starts():
    jobs = [FakeJob(id=1, company="Globex", posted_at=ago(hours=2)),
            FakeJob(id=2, company="Initech", posted_at=ago(hours=5)),
            FakeJob(id=3, company="Umbrella", posted_at=ago(days=2))]

    text = summarise(build_queue(jobs, limit=10, saved={3}, now=NOW))

    assert "1 saved by you · 2 best fit" in text
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


def test_the_best_fit_represents_a_company():
    """Within a company, the same rule applies: its best fit goes, however old."""
    jobs = [
        FakeJob(id=1, company="Acme", ats_match_score=0.95, posted_at=ago(days=30)),
        FakeJob(id=2, company="Acme", ats_match_score=0.72, posted_at=ago(hours=3)),
    ]

    assert [c.job_id for c in build_queue(jobs, limit=10, now=NOW)] == [1]


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
    from ml.resume.scorer import score_basis

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
    from backend.core.locations import is_us

    for place in ("Boston, MA", "Ann Arbor, Michigan", "Fayetteville, AR",
                  "Austin, TX 78701", "Remote - US", "US Remote"):
        assert is_us(place) is True, place


def test_a_bare_us_city_is_recognised():
    """Boards print these with no state, and there are 74 of them in one
    snapshot. Without a city list they read as foreign."""
    from backend.core.locations import is_us

    for place in ("San Francisco", "Chicago", "Austin", "Plano",
                  "New York City | OnSite"):
        assert is_us(place) is True, place


def test_a_foreign_city_is_rejected_even_without_its_country():
    """The second bug, in the opposite direction. "Kfar Saba" ranked first at
    94% because the exclusion list was country-level and the posting never
    said Israel."""
    from backend.core.locations import is_us
    from ml.resume.profile import NON_US_PLACES

    assert is_us("Kfar Saba", NON_US_PLACES) is False
    assert is_us("Getafe Area", NON_US_PLACES) is False
    assert is_us("Rome Metropolitain Area | Remote", NON_US_PLACES) is False


def test_an_unknowable_location_is_kept_not_guessed():
    """"Remote" is silence, not evidence of being abroad. Dropping these was
    the original bug and must not come back through the other door."""
    from backend.core.locations import is_us

    for place in ("Remote", "Hybrid", "In-Office", "", None):
        assert is_us(place) is None, place


def test_a_us_place_is_not_taken_for_the_foreign_one_it_shares_a_name_with():
    """The third bug. A foreign name was checked before anything American, so "Albuquerque, New
    Mexico, United States" was Mexico, "Dublin, OH" Ireland and "Vancouver, WA" Canada -- and
    every fetch dropped them."""
    from backend.core.locations import is_us
    from ml.resume.profile import NON_US_PLACES

    for place in ("Albuquerque, New Mexico, United States", "New Mexico", "Dublin, OH, Remote",
                  "Hybrid - Vancouver, WA", "Paris, TX"):
        assert is_us(place, NON_US_PLACES) is True, place
    for place in ("Mexicali, Baja California, Mexico", "Bengaluru, IN", "Cambridge, United Kingdom",
                  "Bogotá, D.C., Colombia", "Vancouver, BC"):
        assert is_us(place, NON_US_PLACES) is False, place


def test_a_posting_open_in_a_us_place_among_others_is_a_us_posting():
    from backend.core.locations import is_us
    from ml.resume.profile import NON_US_PLACES

    for place in ("Remote (United States | Canada)", "Chicago; London; New York", "New York, London",
                  "Remote Canada | Remote United States", "Canada/US | Remote"):
        assert is_us(place, NON_US_PLACES) is True, place
    assert is_us("Rome Metropolitain Area | Remote", NON_US_PLACES) is False


def test_the_ways_boards_write_a_state_code():
    from backend.core.locations import is_us
    from ml.resume.profile import NON_US_PLACES

    for place in ("PA, Remote", "TX-GRAND PRAIRIE, 2505 N HWY 360", "AZ_Mesa_HQ", "Whippany NJ 07981",
                  "Remote - MA", "Remote-CA", "DC | Remote", "Hybrid - Local to D.C. metro area"):
        assert is_us(place, NON_US_PLACES) is True, place
    for words in ("In-Office", "Remote OR Hybrid", "Remote OK", "Remote - No clearance required"):
        assert is_us(words, NON_US_PLACES) is None, words


def test_remote_in_front_of_a_place_abroad_is_still_abroad():
    """"Remote" or "Hybrid" first kept whatever followed: "Hybrid (Santiago, Las Condes)"."""
    from backend.core.locations import is_us
    from ml.resume.profile import NON_US_PLACES

    for place in ("Hybrid (Santiago, Las Condes)", "Remote (Time zone: CET (+/- 3 hours))",
                  "Hybrid Remote , Bandung Wetan, Kota Bandung, Jawa Barat", "Remote, Ontario"):
        assert is_us(place, NON_US_PLACES) is False, place
    for vague in ("Global | Remote", "AMER", "North America | Remote", "Remote (Multiple Locations)",
                  "Hybrid - 3 days a week", "Remote - Worldwide"):
        assert is_us(vague, NON_US_PLACES) is None, vague


def test_manilas_national_capital_region_is_not_washingtons():
    """23 Oct 2026: "Taguig City, National Capital Region (Manila)" was kept as American and
    rose to the next ten to apply to, the region's name being the DC area's too."""
    from backend.core.locations import is_us
    from ml.resume.profile import NON_US_PLACES

    for place in ("Taguig City, National Capital Region (Manila)", "Makati, Metro Manila", "Manila, Philippines"):
        assert is_us(place, NON_US_PLACES) is False, place
    for place in ("Washington, DC (National Capital Region)", "National Capital Region"):
        assert is_us(place, NON_US_PLACES) is True, place
