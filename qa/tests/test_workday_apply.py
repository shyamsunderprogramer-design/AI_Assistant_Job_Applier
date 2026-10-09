"""The Workday filler against a four-step form shaped like the live one, in a
real browser: dropdowns, a searchable prompt, a radio question only the person
can answer, the hidden `beecatcher`, a resume upload, the legal questions, and
Review. The test plays the person where the filler hands over."""

import pytest

from backend.apply import workday
from backend.apply.profile import Applicant

FORM = r"""<!doctype html><html><body>
<div data-automation-id="progressBar"><span data-automation-id="progressBarActiveStep" id="step"></span></div>
<main id="page"></main>
<input data-automation-id="beecatcher" style="display:none" id="bee">
<script>
const steps = {
 "My Information": `
  <div data-automation-id="formField-legalName--firstName"><label for="fn">First Name*</label><input id="fn" aria-required="true"></div>
  <div data-automation-id="formField-legalName--lastName"><label for="ln">Last Name*</label><input id="ln" aria-required="true"></div>
  <div data-automation-id="formField-preferredName"><label for="pn">Preferred Name</label><input id="pn"></div>
  <div data-automation-id="formField-email"><label for="em">Email Address*</label><input id="em" type="email"></div>
  <div data-automation-id="formField-country"><label>Country*</label><button id="country" aria-haspopup="listbox">Select One</button></div>
  <div data-automation-id="formField-addressLine1"><label for="a1">Address Line 1</label><input id="a1"></div>
  <div data-automation-id="formField-state"><label>State*</label><button aria-haspopup="listbox">Select One</button></div>
  <div data-automation-id="formField-phoneType"><label>Phone Device Type*</label><button aria-haspopup="listbox">Select One</button></div>
  <div data-automation-id="formField-phoneNumber"><label for="ph">Phone Number*</label><input id="ph" type="tel"></div>
  <div data-automation-id="formField-source"><label for="src">How Did You Hear About Us?*</label>
    <div data-automation-id="multiselectInputContainer"><input id="src"></div></div>
  <fieldset data-automation-id="formField-previousWorker"><legend>Have you previously worked for Acme?*</legend>
    <input type="radio" name="prev" id="py" value="true"><label for="py">Yes</label>
    <input type="radio" name="prev" id="pnn" value="false"><label for="pnn">No</label></fieldset>`,
 "My Experience": `
  <div data-automation-id="formField-resume"><label for="up">Resume/CV</label>
    <input type="file" id="up" data-automation-id="file-upload-input-ref" style="opacity:0;position:absolute;width:1px;height:1px"></div>`,
 "Application Questions": `
  <div data-automation-id="formField-q1"><label>Are you legally authorized to work in the United States?*</label><button aria-haspopup="listbox">Select One</button></div>
  <div data-automation-id="formField-q2"><label>Will you now or in the future require sponsorship?*</label><button aria-haspopup="listbox">Select One</button></div>`,
 "Review": `<p>Check everything.</p>`,
};
const options = {"Country*": ["United States Minor Outlying Islands", "United States of America", "Canada"],
  "State*": ["Tennessee", "Texas", "Utah"], "Phone Device Type*": ["Landline", "Mobile"]};
const order = Object.keys(steps); let at = 0;
function show() {
  document.getElementById('step').innerText = order[at];
  document.getElementById('page').innerHTML = steps[order[at]] +
    `<div data-automation-id="errors"></div>` +
    (order[at] === "Review" ? `<button id="submit" data-automation-id="pageFooterNextButton">Submit</button>`
                            : `<button data-automation-id="pageFooterNextButton">Save and Continue</button>`);
}
document.addEventListener('click', e => {
  const b = e.target.closest('button[aria-haspopup="listbox"]');
  if (b) {
    const label = b.parentElement.querySelector('label').innerText;
    const opts = options[label] || ["Yes", "No"];
    document.querySelectorAll('[role=listbox]').forEach(l => l.remove());
    const ul = document.createElement('ul'); ul.setAttribute('role', 'listbox');
    opts.forEach(o => { const li = document.createElement('li'); li.setAttribute('role', 'option');
      li.innerText = o; li.onclick = () => { b.innerText = o; ul.remove(); }; ul.appendChild(li); });
    document.body.appendChild(ul); return;
  }
  const next = e.target.closest('[data-automation-id="pageFooterNextButton"]');
  if (!next) return;
  if (order[at] === "Review") { document.body.innerHTML = "<h2>Application Submitted</h2>"; return; }
  const missing = [...document.querySelectorAll('#page label, #page legend')]
    .filter(l => l.innerText.endsWith('*')).filter(l => {
      const w = l.parentElement;
      const b = w.querySelector('button'), i = w.querySelector('input:not([type=radio])');
      if (w.tagName === 'FIELDSET') return !w.querySelector('input:checked');
      if (b) return b.innerText === 'Select One';
      if (w.querySelector('[data-automation-id=multiselectInputContainer]')) return !w.querySelector('[data-automation-id=selectedItem]');
      return i && !i.value;
    });
  if (missing.length) { document.querySelector('[data-automation-id=errors]').innerHTML =
      `<div data-automation-id="errorMessage">Required: ${missing.map(m => m.innerText).join(', ')}</div>`; return; }
  at++; show();
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Enter' || e.target.id !== 'src') return;
  const box = e.target.parentElement;
  ["Company Website", "LinkedIn"].forEach(o => { const d = document.createElement('div');
    d.setAttribute('data-automation-id', 'promptOption'); d.innerText = o;
    d.onclick = () => { box.innerHTML += `<div data-automation-id="selectedItem">${o}</div>`;
      document.querySelectorAll('[data-automation-id=promptOption]').forEach(x => x.remove()); };
    document.body.appendChild(d); });
});
document.addEventListener('change', e => {
  if (e.target.type === 'file') e.target.parentElement.insertAdjacentHTML('beforeend',
    `<div data-automation-id="file-upload-successful">${e.target.files[0].name}</div>`);
});
show();
</script></body></html>"""


def person():
    return Applicant(
        personal={"first_name": "Jordan", "last_name": "Example", "email": "jordan@example.com",
                  "phone": "+1 (555) 010-0199"},
        location={"country": "United States", "state": "TX", "street": "1 Example Way"},
        authorisation={"authorised_to_work": True, "requires_sponsorship": False},
        preferences={"referral_source": "Company Website"})


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    manager = sync_api.sync_playwright().start()
    try:
        browser = manager.chromium.launch()
    except Exception as exc:                       # no browser installed here
        manager.stop()
        pytest.skip(f"no Chromium: {exc}")
    page = browser.new_page()
    page.set_content(FORM)
    yield page
    browser.close()
    manager.stop()


def test_a_whole_application_with_the_person(page, tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    said = []

    def say(message):
        said.append(message)
        if "needs you" in message:                   # the person answers, then continues
            page.check("#pnn", force=True)
            page.click('[data-automation-id="pageFooterNextButton"]')
        if "Review page" in message:
            page.click("#submit")

    verdict, result = workday.apply(page, 7, None, person(), resume, evidence=tmp_path,
                                    wait_s=60, say=say)

    assert verdict == "submitted"
    assert result.filled["First Name"] == "Jordan"
    assert result.filled["Country"] == "United States"
    assert result.filled["State"] == "Texas"                    # TX, as the list says it
    assert result.filled["Phone Number"] == "5550100199"
    assert result.filled["Phone Device Type"] == "Mobile"
    assert result.filled["How Did You Hear About Us?"] == "Company Website"
    assert result.filled["Will you now or in the future require sponsorship?"] == "No"
    assert result.attachments == ["resume=resume.pdf"]
    assert "Have you previously worked for Acme?" in result.required_blank
    assert any("needs you" in s and "previously worked" in s for s in said)
    assert (tmp_path / "workday-my-information.png").exists()


def test_the_trap_field_and_preferred_name_stay_empty(page, tmp_path):
    workday.fill_step(page, person(), None, workday.FillResult(job_id=1, url=""))
    assert page.input_value("#bee") == ""
    assert page.input_value("#pn") == ""
    assert page.inner_text("#country") == "United States of America"   # not the Islands
    assert page.input_value("#ph") == "5550100199"


def test_the_filler_never_types_a_password(page):
    page.set_content('<div data-automation-id="signInContent"><input type="password" id="pw"></div>')
    assert workday.signing_in(page)
    workday.fill_step(page, person(), None, workday.FillResult(job_id=1, url=""))
    assert page.input_value("#pw") == ""


@pytest.mark.parametrize("label,expected", [
    ("First Name*", "Jordan"), ("Legal Name - Last Name", "Example"),
    ("Preferred First Name", None), ("Phone Extension", None), ("Password*", None),
    ("Country Phone Code*", None), ("State*", "Texas"), ("Gender", None),
])
def test_which_questions_get_which_answers(label, expected):
    assert workday.value_for(label, person()) == expected


def test_a_declaration_the_profile_does_not_state_is_left(page, tmp_path):
    someone = person()
    someone.authorisation = {}
    result = workday.FillResult(job_id=1, url="")
    page.evaluate("""() => { at = 2; show(); }""")
    waiting = workday.fill_step(page, someone, None, result)
    assert len(result.declarations) == 2 and len(waiting) == 2


def test_an_account_page_named_in_the_progress_bar_is_never_filled(page, tmp_path, monkeypatch):
    """Live, the progress bar says "Create Account/Sign In" before the form draws."""
    from backend.apply import accounts

    def none_saved():
        raise accounts.NoAccount()
    monkeypatch.setattr(accounts, "credentials", none_saved)   # never the real Keychain
    page.set_content('<div data-automation-id="progressBar"><span data-automation-id="progressBarActiveStep">'
                     'current step 1 of 7<br>Create Account/Sign In</span></div>'
                     '<div data-automation-id="formField-email"><label for="em">Email Address*</label>'
                     '<input id="em"></div>')
    assert workday.current_step(page) == "Create Account/Sign In"
    said = []
    verdict, result = workday.apply(page, 1, None, person(), None, evidence=tmp_path, wait_s=3,
                                    say=said.append)
    assert page.input_value("#em") == "" and result.filled == {}
    assert verdict == "timeout" and any("Sign in" in s for s in said)


def test_education_fields_come_from_the_profile_and_questions_do_not():
    from backend.apply.profile import Applicant
    from backend.apply.workday import degree_option, value_for
    a = Applicant(education=[{"school": "Example State University", "degree": "Master of Science",
                              "field": "Computer Science"}])
    assert value_for("School or University", a) == "Example State University"
    assert value_for("Degree", a) == "Master of Science"
    assert value_for("Field of Study", a) == "Computer Science"
    assert value_for("Did you graduate from high school or obtain a GED?", a) != "Example State University"
    assert degree_option(["Associate's Degree", "Bachelor's Degree", "Master's Degree"], "Master of Science") == 2
    assert degree_option(["MA - Master of Arts", "MS - Master of Science"], "Master of Science") == 1


def test_abbreviated_degrees_and_close_fields():
    from backend.apply.workday import _words_in, degree_option
    options = ["Select One", "GED", "High School", "AA", "AS", "BA", "BS", "MA", "MS", "MBA", "PhD"]
    assert options[degree_option(options, "Master of Science")] == "MS"
    assert degree_option(options, "Bachelor of Technology") is None        # no "BT": the person picks
    assert _words_in("Computer and Information Science", "Computer Science")
    assert not _words_in("Computer Engineering", "Computer Science")
