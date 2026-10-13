"""JazzHR forms: the employer's questions read, answered from the profile, the resume's dates and the person's own answers."""

from types import SimpleNamespace

from backend.apply import jazzhr
from backend.apply.profile import Applicant

FORM = """
<form id="form_submit_new_resume">
<label for='resumator-firstname-value'>First Name<i class="asterisk">*</i></label><input id="resumator-firstname-value">
<label id='l1' for='resumator-questionnaire-q1' class='control-label'>Will you now or in the future require sponsorship for employment in the United States?<i class="asterisk">*</i></label>
<select id="resumator-questionnaire-q1" name="resumator-questionnaire[1]"><option value='resumator_no_selection'>-- No answer --</option><option value='Yes'>Yes</option><option value='No'>No</option></select>
<label for='resumator-questionnaire-q2'>Given this role's federal focus - are you a US Citizen?<i class="asterisk">*</i></label>
<select id="resumator-questionnaire-q2"><option value='resumator_no_selection'>-- No answer --</option><option value='Yes'>Yes</option><option value='No'>No</option></select>
<label for='resumator-questionnaire-q3'>How many years of experience in DevOps, site reliability engineering, or platform automation do you have?<i class="asterisk">*</i></label>
<select id="resumator-questionnaire-q3"><option value='resumator_no_selection'>-- No answer --</option><option value='1-4 Years'>1-4 Years</option><option value='5-7 Years'>5-7 Years</option><option value='8+ Years'>8+ Years</option></select>
<label for='resumator-questionnaire-q4'>Describe your direct experience supporting FedRAMP.<i class="asterisk">*</i></label>
<textarea id="resumator-questionnaire-q4"></textarea>
<label for='resumator-questionnaire-q5'>Are you currently residing in one of the following states or districts: Washington DC, Florida, Texas, or Washington State?<i class="asterisk">*</i></label>
<select id="resumator-questionnaire-q5"><option value='resumator_no_selection'>-- No answer --</option><option value='Yes'>Yes</option><option value='No'>No</option></select>
<div id="resumator-recaptcha">Human Check</div>
</form>"""


def person(**auth):
    return Applicant(personal={"first_name": "Jane", "last_name": "Doe", "email": "j@x.com", "phone": "555"},
                     location={"city": "Austin", "state": "TX", "postal_code": "78701"},
                     authorisation={"requires_sponsorship": False, "us_citizen": False, **auth})


JOBS = [SimpleNamespace(section="experience", title="Senior DevOps Engineer", start="2018-04"),
        SimpleNamespace(section="experience", title="Software Engineer", start="2012-05")]


def test_the_questions_are_read_with_their_choices():
    qs = jazzhr.questions_from_html(FORM)
    assert [q["kind"] for q in qs] == ["select", "select", "select", "textarea", "select"]
    assert qs[2]["options"] == ["1-4 Years", "5-7 Years", "8+ Years"] and all(q["required"] for q in qs)


def test_each_question_is_answered_from_what_the_person_stated():
    qs = jazzhr.questions_from_html(FORM)
    got = [jazzhr.answer(q, person(), JOBS) for q in qs]
    assert got[0] == "No" and got[1] == "No"          # no sponsorship; not a citizen, as stated
    assert got[2] == "8+ Years"                         # DevOps since 2018, from the resume's dates
    assert got[3] is None                               # the essay is the person's
    assert got[4] == "Yes"                              # Texas is on the list, "Washington State" is not a State field


def test_an_unstated_citizenship_is_left_blank_and_another_state_says_no():
    qs = jazzhr.questions_from_html(FORM)
    p = person(us_citizen=None)
    p.location["state"] = "Massachusetts"
    assert jazzhr.answer(qs[1], p, JOBS) is None and jazzhr.answer(qs[4], p, JOBS) == "No"


def test_the_persons_own_answer_for_this_job_fills_the_essay():
    from backend.apply.answers import normalise
    qs = jazzhr.questions_from_html(FORM)
    p = person()
    p.answers[normalise(qs[3]["label"])] = "I built FedRAMP evidence pipelines."
    assert jazzhr.answer(qs[3], p, JOBS) == "I built FedRAMP evidence pipelines."


def test_pick_matches_ranges_and_words():
    opts = ["Less than 1 year", "1-4 Years", "5-7 Years", "8+ Years"]
    assert jazzhr.pick(opts, "9") == "8+ Years" and jazzhr.pick(opts, "6") == "5-7 Years"
    assert jazzhr.pick(opts, "0") == "Less than 1 year" and jazzhr.pick(["Yes", "No"], "no") == "No"
    assert jazzhr.pick(["Yes", "No"], "Maybe") is None


def test_years_count_only_the_jobs_in_the_named_field():
    assert jazzhr.years_for("How many years of DevOps experience?", JOBS) >= 8
    assert jazzhr.years_for("How many years of professional experience?", JOBS) >= 14
    assert jazzhr.years_for("How many years of data science experience?", JOBS) is None


def test_a_submit_keeps_the_confirmation_page(tmp_path):
    class Page:
        def is_closed(self): return False
        def query_selector(self, sel): return None                      # the form is gone
        def inner_text(self, sel): return "Thank you for applying! We received your application."
        def screenshot(self, path, full_page): open(path, "wb").write(b"png")
        def wait_for_timeout(self, ms): pass
        def evaluate(self, js): return {}
    verdict, _ = jazzhr.watch_submission(Page(), 5, evidence=tmp_path)
    assert verdict == "submitted"
    assert (tmp_path / "confirmation.png").exists() and "Thank you" in (tmp_path / "confirmation.txt").read_text()


def test_the_form_still_there_is_not_a_submit():
    class Page:
        def query_selector(self, sel): return object()                 # the form is still on the page
        def inner_text(self, sel): return "Thank you for applying"     # in the posting's own text
    assert jazzhr.is_confirmed(Page()) is False
