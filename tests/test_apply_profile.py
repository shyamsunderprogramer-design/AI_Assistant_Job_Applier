"""The applicant profile, and what it refuses to answer — offline.

The tests that matter here are the negative ones. An application form is one
of the few places where a plausible wrong answer is worse than a blank, and
the profile's job is to know the difference.
"""

import pytest

from apply.profile import Applicant, ProfileIncomplete, check_ready, load


def complete(**overrides) -> Applicant:
    base = Applicant(
        personal={"first_name": "Dev", "last_name": "P", "email": "d@example.com",
                  "phone": "+1 555 0100"},
        authorisation={"authorised_to_work": True, "requires_sponsorship": False},
        links={"linkedin": "https://linkedin.com/in/dev"},
        employment={"notice_period": "2 weeks", "willing_to_relocate": False},
        preferences={"referral_source": "Company website"},
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


# -- what must be present before a form is opened --------------------------

def test_a_complete_profile_is_ready():
    assert check_ready(complete()) == []


def test_missing_basics_are_all_reported_at_once():
    """Returned rather than raised, so someone fixes the file once."""
    bare = Applicant(authorisation={"authorised_to_work": True,
                                    "requires_sponsorship": False})
    problems = check_ready(bare)

    assert len(problems) >= 4
    assert any("first name" in p for p in problems)
    assert any("email" in p for p in problems)


def test_a_malformed_email_is_caught_before_fifty_forms_get_it():
    problems = check_ready(complete(personal={
        "first_name": "Dev", "last_name": "P", "email": "dev-at-example",
        "phone": "+1 555 0100"}))
    assert any("does not look like an email" in p for p in problems)


# -- the declarations, which have no safe default --------------------------

def test_unset_work_authorisation_stops_the_run():
    """A legal declaration on a real application. There is no common answer
    worth guessing: the run stops instead."""
    problems = check_ready(complete(authorisation={"requires_sponsorship": False}))

    assert any("authorised_to_work" in p for p in problems)
    assert any("will not be guessed" in p for p in problems)


def test_unset_sponsorship_stops_the_run():
    problems = check_ready(complete(authorisation={"authorised_to_work": True}))
    assert any("requires_sponsorship" in p for p in problems)


def test_false_is_a_real_answer_not_a_missing_one():
    """Needing sponsorship is an answer. `False` must not read as unset."""
    assert check_ready(complete(authorisation={
        "authorised_to_work": False, "requires_sponsorship": True})) == []


# -- answering the employer's own questions --------------------------------

def test_questions_the_profile_covers_are_answered():
    person = complete()
    assert person.answer_for("LinkedIn Profile*") == "https://linkedin.com/in/dev"
    assert person.answer_for("Will you now or in the future require sponsorship?") == "No"
    assert person.answer_for("What is your notice period?") == "2 weeks"
    assert person.answer_for("Are you willing to relocate?") == "No"


def test_a_question_the_profile_does_not_cover_is_left_blank():
    """An approximately-right answer on an application cannot be told apart
    from a considered one, which is what makes it worse than an empty field."""
    person = complete()

    assert person.answer_for("Why do you want to work at Acme?") is None
    assert person.answer_for("Describe a system you are proud of") is None
    assert person.answer_for("") is None


def test_an_unstated_declaration_is_not_invented():
    person = complete(authorisation={"authorised_to_work": None,
                                     "requires_sponsorship": None})
    assert person.answer_for("Are you legally authorized to work in the US?") is None
    assert person.answer_for("Do you hold an active security clearance?") is None


# -- how far the run may go ------------------------------------------------

def test_stopping_before_submit_is_the_default():
    """A setting that must be turned ON to submit is the right way round for
    something that cannot be taken back."""
    assert Applicant().stop_before_submit is True
    assert Applicant(apply={"stop_before_submit": False}).stop_before_submit is False


def test_a_daily_cap_always_has_a_sane_value():
    assert Applicant().max_per_day == 10
    assert Applicant(apply={"max_per_day": 50}).max_per_day == 50
    assert Applicant(apply={"max_per_day": "nonsense"}).max_per_day == 10
    assert Applicant(apply={"max_per_day": -5}).max_per_day == 0


# -- loading ---------------------------------------------------------------

def test_a_missing_profile_says_how_to_make_one(tmp_path):
    with pytest.raises(ProfileIncomplete) as raised:
        load(tmp_path / "nope.yaml")
    assert "applicant.yaml.example" in str(raised.value)


def test_a_real_file_round_trips(tmp_path):
    path = tmp_path / "applicant.yaml"
    path.write_text(
        "personal:\n  first_name: Dev\n  last_name: P\n  email: d@example.com\n"
        "  phone: '+1 555 0100'\n"
        "authorisation:\n  authorised_to_work: true\n  requires_sponsorship: false\n"
        "apply:\n  max_per_day: 30\n", encoding="utf-8")

    person = load(path)

    assert person.full_name == "Dev P"
    assert person.max_per_day == 30
    assert check_ready(person) == []
