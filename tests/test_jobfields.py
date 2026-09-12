"""Posting-field parsing — every string here was taken from a real posting.

The rejections are the point. A blank salary sends you to read the posting; a
confidently wrong one sends you to an interview about the wrong money.
"""

from jobfields import (
    HYBRID,
    ONSITE,
    REMOTE,
    UNSTATED,
    experience_label,
    experience_years,
    salary,
    salary_label,
    workplace,
    workplace_label,
)


# -- salary: the four formats boards actually emit --------------------------

def test_greenhouse_em_dash_with_currency():
    got = salary("$285,000—$325,000 USD")
    assert (got.low, got.high, got.currency, got.period) == (285000, 325000, "USD", "year")


def test_ashby_k_suffix_with_decimals():
    got = salary("Compensation: $140.8K – $231K • Offers Equity • Multiple Ranges")
    assert (got.low, got.high) == (140800, 231000)


def test_plain_hyphen_with_the_word_annually():
    got = salary("The US base salary range for this full-time position is "
                 "$180,000 - $240,000 annually + equity + benefits")
    assert (got.low, got.high) == (180000, 240000)


def test_the_word_to():
    got = salary("This position has a base salary range between $135,000 to $145,000")
    assert (got.low, got.high) == (135000, 145000)


def test_no_currency_suffix_still_works_with_context():
    got = salary("Base salary range: $174,915—$209,906")
    assert (got.low, got.high) == (174915, 209906)


# -- salary: what must NOT be read as pay -----------------------------------

def test_a_retirement_match_is_not_a_salary():
    assert salary("401(k) with company match of $3,000 - $5,000 per year") is None


def test_a_funding_round_is_not_a_salary():
    assert salary("We raised $50M - $80M in our Series C") is None


def test_a_pair_of_unrelated_numbers_is_not_a_salary():
    # No currency stamp and nothing nearby calling it pay.
    assert salary("Serving 100,000 - 250,000 requests per second") is None


def test_a_posting_with_no_numbers_at_all():
    assert salary("Competitive compensation and generous benefits") is None


def test_an_hourly_rate_keeps_its_clock():
    got = salary("The hourly pay range for this role is $65 - $85 per hour")
    assert (got.low, got.high, got.period) == (65, 85, "hour")


def test_a_reversed_range_is_rejected():
    assert salary("salary $240,000 - $180,000") is None


# -- experience -------------------------------------------------------------

def test_plus_form():
    assert experience_years("- 8+ years of industry software engineering") == (8, None)


def test_range_form():
    assert experience_years("3-5 years of experience with enterprise platforms") == (3, 5)


def test_range_with_trailing_plus():
    assert experience_years("5-7+ years experience with Continuous Integration") == (5, 7)


def test_the_binding_bar_wins_not_the_smallest():
    # Requirements are conjunctive: a posting asking for 8 years of one thing
    # and 3 of another wants someone with eight.
    text = ("8+ years of experience managing fleets. "
            "3+ years of experience with Kubernetes required.")
    assert experience_years(text) == (8, None)


def test_years_without_experience_context_are_ignored():
    assert experience_years("Celebrating 10 years of serving customers") == (None, None)


def test_no_years_mentioned():
    assert experience_years("Strong background in distributed systems") == (None, None)


# -- workplace: from the location, never the blurb --------------------------

def test_plain_remote():
    assert workplace("Remote - USA") == REMOTE


def test_a_city_is_onsite():
    assert workplace("Oakland, California, United States") == ONSITE


def test_hybrid_wins_when_a_posting_lists_everything():
    assert workplace("San Francisco | Remote | Hybrid") == HYBRID


def test_remote_first_boilerplate_is_not_a_remote_job():
    # "Remote-Friendly (Travel-Required)" is a culture claim, not an arrangement.
    assert workplace("Remote-Friendly (Travel-Required) | San Francisco, CA") == ONSITE


def test_a_bare_country_is_not_an_office():
    # Absence of the word "remote" is not evidence of a desk.
    assert workplace("United States") is None


def test_ashby_structured_fields_win():
    assert workplace("Somewhere, CA", is_remote=True) == REMOTE
    assert workplace("Somewhere, CA", workplace_type="Hybrid") == HYBRID


def test_nothing_at_all():
    assert workplace("") is None
    assert workplace(None) is None


# -- rendering: silence has to look different from "not checked" ------------

def test_absent_values_say_so():
    assert salary_label(None, None) == UNSTATED
    assert experience_label(None, None) == UNSTATED
    assert workplace_label(None) == UNSTATED


def test_present_values_are_compact():
    assert salary_label(180000, 240000) == "$180K–$240K"
    assert experience_label(5, None) == "5+ yrs"
    assert experience_label(3, 5) == "3–5 yrs"
    assert workplace_label(REMOTE) == "remote"


# -- currency: a bare "$" on a non-US range is a 30% lie --------------------

def test_canadian_dollars_are_named():
    got = salary("The base pay range for this position is $154,000-$200,000 CAD/year")
    assert got.currency == "CAD"
    assert got.label() == "C$154K–C$200K"


def test_pounds_parse_and_render():
    got = salary("The salary for this role is £80,000 - £110,000 per year")
    assert (got.currency, got.label()) == ("GBP", "£80K–£110K")


def test_a_currency_symbol_is_itself_evidence_of_pay():
    # No English pay words anywhere, but the symbol settles it.
    got = salary("Salaire annuel de €90,000 to €120,000")
    assert (got.currency, got.low, got.high) == ("EUR", 90000, 120000)


# -- experience: a company's own age is not a requirement -------------------

def test_company_history_is_not_a_requirement():
    # Real text that produced a "40+ yrs" requirement before this guard.
    assert experience_years(
        "operating in the multi-trillion-dollar corporate finance arena. "
        "With more than 40 years of experience in the industry"
    ) == (None, None)


def test_nearly_n_years_is_also_the_company_talking():
    assert experience_years(
        "testing their networks against real-world threats. "
        "With nearly 20 years of offensive security experience"
    ) == (None, None)


def test_combined_team_experience_is_not_a_requirement():
    assert experience_years(
        "Our team has 50 years of combined experience building platforms"
    ) == (None, None)


def test_a_real_requirement_next_to_company_prose_still_counts():
    text = ("Founded 12 years ago, we build developer tools. "
            "Requirements: 6+ years of experience in platform engineering.")
    assert experience_years(text) == (6, None)


# -- non-breaking spaces, which HTML leaves everywhere ----------------------

def test_a_non_breaking_space_does_not_hide_company_history():
    # Verbatim from a posting: the NBSP in "With\xa0nearly" defeated the guard
    # and turned a company's age into a 20-year requirement.
    assert experience_years(
        "networks against real-world threats. With nearly 20 years of "
        "industry contributions"
    ) == (None, None)


def test_a_non_breaking_space_does_not_hide_a_salary():
    got = salary("Compensation: $140.8K – $231K • Offers Equity")
    assert (got.low, got.high) == (140800, 231000)


def test_a_non_breaking_space_does_not_hide_a_requirement():
    assert experience_years("degree or equivalent with 15+ Years of experience") == (15, None)
