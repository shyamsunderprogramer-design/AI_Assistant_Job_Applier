"""Fill a Workday application, step by step, alongside the person.

Built against the live flow (abercrombie.wd108, Sep 2026) and Workday's
stable `data-automation-id` attributes, which are the same on every tenant:

  job page      `adventureButton` ("Apply")
  popup         `applyManually` · `autofillWithResume` · `useMyLastApplication`
  sign-in       `signInContent`, `email`, `password`, `createAccountSubmitButton`
                -- and `beecatcher`, a hidden field only a bot fills
  the wizard    `progressBar` / `progressBarActiveStep`; each field wrapped in
                `formField-*`; dropdowns are `button[aria-haspopup=listbox]`,
                searchable lists are `multiselectInputContainer` prompts;
                `pageFooterNextButton` (older: `bottom-navigation-next-button`)

What it will not do, and why:

**Sign in or create an account.** Every Workday employer wants an account.
Creating one is the person agreeing to that employer's terms under their own
name and password, so it is theirs; the filler waits on the sign-in page,
never reads or types into it, and carries on once they are through. The
apply browser keeps its profile (backend/apply/helper.py), so a tenant signed
into once usually stays signed in.

**Fill what a person cannot see.** Hidden fields are skipped, which is also
what keeps `beecatcher` empty.

**Tick a box.** Checkboxes on these forms are consents, terms and the
disability self-identification -- statements, not details.

**Press Submit.** It stops on the Review step: this filler is new, a Workday
application cannot be withdrawn by the applicant, and Review is where the
person sees everything at once. It watches for the confirmation and records
the application when they submit.

Answers come from the same place as Greenhouse's (`Applicant.answer_for`),
with the rule that a question the profile does not cover is left blank and
named, never approximated.
"""

from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path

from backend.apply.greenhouse import (DECLINE_PHRASES, PAGE_TIMEOUT_MS, FillResult,
                                      _evidence_dir, _is_declaration)
from backend.apply.profile import Applicant

log = logging.getLogger(__name__)

STEP_LIMIT = 12             # a wizard with more steps than this is going in circles
POLL_MS = 2000

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "PR": "Puerto Rico", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

CONFIRM_MARKERS = ("application submitted", "successfully submitted", "thank you for applying",
                   "thanks for applying", "we have received your application",
                   "your application has been submitted", "you have successfully applied")

NEXT_BUTTONS = ('[data-automation-id="pageFooterNextButton"]',
                '[data-automation-id="bottom-navigation-next-button"]')

# Every visible field on the step, tagged `data-ja="<n>"` so Python can act on
# it, with the question it answers. The question is read from the field's
# Workday wrapper first (its label or legend), because a dropdown button's
# own aria-labelledby includes its current value ("Select One").
_COLLECT_JS = r"""() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const seen = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const question = el => {
    const wrap = el.closest('[data-automation-id^="formField-"], fieldset');
    if (wrap) {
      const l = wrap.querySelector('legend, label');
      if (l && clean(l.innerText)) return clean(l.innerText);
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && clean(l.innerText)) return clean(l.innerText);
    }
    return clean(el.getAttribute('aria-label'));
  };
  const required = (el, label) => /\*\s*$/.test(label) || el.getAttribute('aria-required') === 'true'
      || !!(el.closest('[data-automation-id^="formField-"]') || el).querySelector?.('[aria-required="true"]');
  const out = []; let n = 0;
  const tag = el => { el.setAttribute('data-ja', String(n)); return n++; };

  for (const el of document.querySelectorAll('input, textarea, button[aria-haspopup="listbox"]')) {
    const auto = el.getAttribute('data-automation-id') || '';
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (auto === 'beecatcher' || type === 'password' || type === 'hidden' || el.disabled || el.readOnly) continue;
    if (type === 'file') { out.push({i: tag(el), kind: 'file', label: question(el) || 'Resume', value: '', required: false}); continue; }
    if (!seen(el)) continue;
    const label = question(el);
    if (el.tagName === 'BUTTON') {
      let value = clean(el.innerText);
      if (/^select one$/i.test(value)) value = '';
      out.push({i: tag(el), kind: 'dropdown', label, value, required: required(el, label)});
    } else if (type === 'radio') {
      const opt = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
      out.push({i: tag(el), kind: 'radio', label, option: clean(opt ? opt.innerText : el.value),
                name: el.name, value: el.checked ? 'checked' : '', required: required(el, label)});
    } else if (type === 'checkbox') {
      out.push({i: tag(el), kind: 'checkbox', label, value: el.checked ? 'checked' : '', required: required(el, label)});
    } else if (el.closest('[data-automation-id="multiselectInputContainer"]')) {
      const wrap = el.closest('[data-automation-id^="formField-"]') || el.parentElement;
      const chosen = wrap.querySelector('[data-automation-id="selectedItem"]');
      out.push({i: tag(el), kind: 'prompt', label, value: chosen ? clean(chosen.innerText) : '',
                required: required(el, label)});
    } else {
      out.push({i: tag(el), kind: 'text', label, value: clean(el.value), required: required(el, label)});
    }
  }
  return out.map(o => ({...o, eid: (document.querySelector(`[data-ja="${o.i}"]`) || {}).id || ''}));
}"""

_ERRORS_JS = r"""() => [...document.querySelectorAll(
    '[data-automation-id="errorMessage"], [data-automation-id="errorBanner"], [role="alert"]')]
  .filter(e => e.offsetHeight > 0).map(e => (e.innerText || '').replace(/\s+/g, ' ').trim())
  .filter(Boolean)"""


# -- answers ------------------------------------------------------------------

def _bare(label: str) -> str:
    return re.sub(r"\s*\*\s*$", "", label or "").strip()


def national_number(phone: str, country: str = "") -> str:
    """The number without its country code: Workday asks for the code separately."""
    digits = re.sub(r"\D", "", phone or "")
    us = not country or "united states" in country.lower() or country.strip().upper() in ("US", "USA")
    if us and len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def value_for(label: str, applicant: Applicant) -> str | None:
    """The answer to one Workday question, or None to leave it for the person."""
    text = _bare(label).lower()
    words = set(re.findall(r"[a-z]+", text))
    if not text or "password" in text:
        return None
    if "preferred" in words or "middle" in words or "extension" in words:
        return None
    if "first name" in text or "given name" in text:
        return applicant.first_name or None
    if "last name" in text or "family name" in text or "surname" in words:
        return applicant.last_name or None
    if "email" in words:
        return applicant.email or None
    if "phone device type" in text or text == "device type":
        return "Mobile"
    if "phone code" in text:
        return None                       # set from the country, not typed
    if "phone" in words:
        return national_number(applicant.phone, str(applicant.location.get("country") or "")) or None
    if "address line 1" in text or text in ("address", "street", "street address"):
        return str(applicant.location.get("street") or "").strip() or None
    if "address line" in text:
        return None
    short = len(text) <= 40                 # a field's name, not a question ("did you finish high school?")
    # "Country" is a field; "Are you legally authorized to work in the country
    # where the job is located?" is a question, and was answered "United States".
    if short and "country" in words and "phone" not in words:
        return str(applicant.location.get("country") or "").strip() or None
    school = applicant.education[0] if applicant.education else {}
    if short and "high" not in words and words & {"school", "university", "college"}:
        return str(school.get("school") or "").strip() or None
    if short and "degree" in words and not words & {"level", "highest", "have", "do", "required"}:
        return str(school.get("degree") or "").strip() or None
    if short and ("field of study" in text or "major" in words):
        return str(school.get("field") or "").strip() or None
    demo = applicant.demographics
    if "gender" in words or "sex" in words:
        return str(demo.get("gender") or "").strip() or None
    if "veteran" in words:
        return str(demo.get("veteran_status") or "").strip() or None
    if "disability" in words:
        return str(demo.get("disability_status") or "").strip() or None
    if "hispanic" in words or "latino" in words:
        return str(demo.get("hispanic_latino") or "").strip() or None
    if "ethnicity" in words or "race" in words:
        return str(demo.get("race_ethnicity") or "").strip() or None

    answer = applicant.answer_for(_bare(label))
    if answer and ("state" in words or "province" in words):
        return US_STATES.get(answer.strip().upper(), answer)
    return answer


def _pick(texts: list[str], answer: str) -> int | None:
    """Which option says `answer`: exact, then starts-with, then "decline"."""
    want = answer.strip().lower()
    low = [t.strip().lower() for t in texts]
    if want == "decline":
        return next((i for i, t in enumerate(low) if any(p in t for p in DECLINE_PHRASES)), None)
    exact = next((i for i, t in enumerate(low) if t == want), None)
    if exact is not None:
        return exact
    # The shortest option that starts with it: "United States" is "United
    # States of America", not "United States Minor Outlying Islands".
    starts = [i for i, t in enumerate(low) if t.startswith(want)]
    return min(starts, key=lambda i: len(low[i])) if starts else None


def degree_option(texts: list[str], degree: str) -> int | None:
    """ "Master of Science" -> "Master's Degree" / "MS - Master of Science"; None when unsure."""
    level = next((lvl for lvl, pat in (("master", r"\bmaster|\bm\.?s\b|\bm\.?tech"),
                                       ("bachelor", r"\bbachelor|\bb\.?s\b|\bb\.?tech|\bb\.?e\b"),
                                       ("doctor", r"\bdoctor|\bph\.?d"), ("associate", r"\bassociate"))
                  if re.search(pat, degree.lower())), None)
    if level is None:
        return None
    hits = [i for i, t in enumerate(texts) if level in t.lower()]
    if not hits:
        # Abbreviated lists (Abercrombie: "BS", "MS", "MBA"): the degree's initials.
        initials = "".join(w[0] for w in re.findall(r"[a-z]+", degree.lower()) if w not in ("of", "in", "the"))
        hits = [i for i, t in enumerate(texts) if re.sub(r"[^a-z]", "", t.lower()) == initials]
    if not hits and level == "bachelor" and re.search(r"\btech|\bengineering|\bb\.?e\b", degree.lower()):
        # No B.Tech or B.E. on the list: the person's choice is BS, its usual US equivalent.
        hits = [i for i, t in enumerate(texts)
                if re.sub(r"[^a-z]", "", t.lower()) == "bs" or "bachelor of science" in t.lower()]
    if len(hits) > 1:                      # "Master of Science" over "Master of Arts"
        tail = [i for i in hits if any(w in texts[i].lower() for w in re.findall(r"[a-z]{4,}", degree.lower())
                                       if w not in ("master", "bachelor", "doctor"))]
        hits = tail or hits
    return hits[0] if len(hits) == 1 else None


# -- acting on one field ---------------------------------------------------------

def _options(page, selector: str) -> list:
    return [o for o in page.query_selector_all(selector) if o.is_visible()]


def _choose_dropdown(page, el, answer: str, label: str = "") -> bool:
    from backend.apply.accounts import _click
    try:
        # A School list left open read as this dropdown's options: Degree was
        # offered only "School Not Found". Close any open list first, then read
        # the one this button controls.
        # Workday leaves the School list on the page after a pick, and it was
        # read as Degree's options ("School Not Found"). Only the options that
        # appear when this dropdown opens are its own.
        if _options(page, '[role="listbox"]'):
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        stale = {o.inner_text().strip() for o in _options(page, '[role="option"]')}
        if not _click(page, el):
            log.info("Workday %r: the dropdown would not open", label)
            return False
        page.wait_for_timeout(800)
        owned = el.get_attribute("aria-controls")
        opts = _options(page, f'[id="{owned}"] [role="option"]') if owned else []
        real = '[role="listbox"] [role="option"]:not([data-automation-id="selectedItem"])'
        if not opts:
            opts = [o for o in _options(page, real) if o.inner_text().strip() not in stale]
        if not opts:
            # The click landed but no list opened (Degree, Abercrombie): a
            # person would press Enter on the focused field.
            el.focus()
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
            opts = [o for o in _options(page, real) if o.inner_text().strip() not in stale]
        if not opts:
            log.info("Workday %r: the list did not open", label)
            return False
        texts = [o.inner_text() for o in opts]
        hit = _pick(texts, answer)
        if hit is None and _bare(label).lower() == "degree":
            hit = degree_option(texts, answer)
        if hit is None and label:
            from backend.apply import general     # "Company website" -> "Abercrombie Careers Website"
            hit = general.pick_option(label, answer, texts)
        if hit is None:
            log.info("Workday %r: no option says %r — options: %s", label, answer, "; ".join(t.strip() for t in texts)[:400])
        if hit is None:
            page.keyboard.press("Escape")
            return False
        _click(page, opts[hit])
        page.wait_for_timeout(400)
        return True
    except Exception as exc:
        log.info("Workday dropdown %r = %r: %s", label, answer, str(exc).splitlines()[0][:160])
        return False


NOT_FOUND = {"school": "School Not Found", "field of study": "Degree Not Found"}


def _words_in(option: str, answer: str) -> bool:
    """Every significant word of the answer is in the option: "Computer Science" in
    "Computer and Information Science"."""
    want = [w for w in re.findall(r"[a-z]+", answer.lower()) if w not in ("of", "and", "the", "in")]
    have = set(re.findall(r"[a-z]+", option.lower()))
    return bool(want) and all(w in have for w in want)


def _search_prompt(page, el, query: str) -> tuple[list, list[str]]:
    from backend.apply.accounts import _click
    _click(page, el)
    el.fill(query)
    el.press("Enter")
    opts, texts = [], []
    for _ in range(8):                          # a school search can take a few seconds
        page.wait_for_timeout(750)
        opts = _options(page, '[data-automation-id="promptOption"], [role="option"]')
        texts = [o.inner_text().strip() for o in opts]
        if texts:
            break
    real = [(o, t) for o, t in zip(opts, texts) if t and not t.lower().startswith("no items")]
    return [o for o, _ in real], [t for _, t in real]


def _choose_prompt(page, el, answer: str, label: str = "") -> bool:
    """Search a Workday list for the answer: the whole answer, then its first word;
    an option that says it exactly, or holds every word of it; else the form's own
    "School Not Found" entry, which Workday asks for when a school is not listed."""
    from backend.apply.accounts import _click
    try:
        queries = [answer] + [w for w in answer.split()[:1] if len(w) > 3 and w != answer]
        fallback = next((v for k, v in NOT_FOUND.items() if k in _bare(label).lower()), None)
        hit, opts, texts = None, [], []
        for query in queries + ([fallback] if fallback else []):
            opts, texts = _search_prompt(page, el, query)
            hit = _pick(texts, query if query == fallback else answer)
            if hit is None and query != fallback:
                close = [i for i, t in enumerate(texts) if _words_in(t, answer)]
                hit = close[0] if close else None
            if hit is not None:
                break
            el.fill("")
        if hit is None:
            log.info("Workday %r: no option says %r — options: %s", label, answer,
                     "; ".join(t.strip() for t in texts)[:400] or "none shown")
            el.fill("")
            page.keyboard.press("Escape")
            return False
        _click(page, opts[hit])
        page.wait_for_timeout(400)
        page.keyboard.press("Escape")            # a prompt list stays open after a pick
        return True
    except Exception as exc:
        log.info("Workday prompt %r = %r: %s", label, answer, str(exc).splitlines()[0][:160])
        return False


# -- work experience ---------------------------------------------------------------

_CAPS = {"devops": "DevOps", "devsecops": "DevSecOps", "sre": "SRE", "qa": "QA", "it": "IT",
         "ai": "AI", "ml": "ML", "ui": "UI", "ux": "UX", "hr": "HR", "vp": "VP", "cto": "CTO"}


def _titled(text: str) -> str:
    """"SENIOR DEVOPS ENGINEER" -> "Senior DevOps Engineer"; mixed case is left alone."""
    if text != text.upper():
        return text
    return " ".join(_CAPS.get(w.lower(), w.capitalize()) for w in text.split())


def work_history(applicant: Applicant, resume: Path | None) -> list[dict]:
    """The jobs for Workday's Work Experience, newest first.

    The profile's own `work` list when it has one -- a resume heading says
    "CITY WTR. DEPT., NORTH, IND", and a form wants the employer's real name --
    else the jobs as the resume states them.
    """
    if applicant.work:
        return applicant.work
    if resume is None or not Path(resume).exists():
        return []
    try:
        from ml.tailoring.reader import read_document
        from ml.tailoring.structure import build
        model = build(read_document(Path(resume)))
    except Exception as exc:
        log.info("Workday work experience: could not read the resume (%s)", exc)
        return []
    jobs = []
    for e in model.entries:
        if e.section != "experience" or not e.start:
            continue
        company, _, place = e.employer.rpartition(", ")
        # The attached (tailored) resume's own lines for this job. From a PDF
        # they read as "text", not "bullet": Role Description came out empty.
        facts = [f.text.strip().lstrip("•-* ").strip() for f in (model.fact(i) for i in e.facts)
                 if f and f.kind in ("bullet", "text")]
        jobs.append({"title": _titled(e.title), "company": company or e.employer,
                     "location": place if company else "", "from": e.start,
                     "to": None if e.present else e.end, "current": e.present,
                     "description": _description(facts)})
    return jobs


def _description(facts: list[str], limit: int = 1900) -> str:
    """One bullet per line, whole bullets only, within Workday's box."""
    out = ""
    for fact in facts:
        line = f"• {fact}\n"
        if len(out) + len(line) > limit:
            break
        out += line
    return out.rstrip()


def _month_year(value) -> tuple[str, str] | None:
    """"2025-02", "02/2025" or 2025 -> ("02", "2025")."""
    text = str(value or "").strip()
    m = re.match(r"^(\d{4})(?:-(\d{1,2}))?$", text) or None
    if m:
        return (m.group(2) or "01").zfill(2), m.group(1)
    m = re.match(r"^(\d{1,2})/(\d{4})$", text)
    return (m.group(1).zfill(2), m.group(2)) if m else None


_ADD_JS = r"""([aria, heading, mark]) => {
  // A section's own Add button: by its label ("Add Work Experience"), else the
  // Add / Add Another under the section's heading.
  let b = [...document.querySelectorAll('button')].find(x =>
      new RegExp(aria, 'i').test(x.getAttribute('aria-label') || ''));
  if (!b) {
    const h = [...document.querySelectorAll('h2,h3,h4,[role="heading"],legend')]
        .find(x => new RegExp('^\\s*' + heading + '\\s*$', 'i').test(x.textContent));
    let box = h;
    while (box && !b) {
      box = box.parentElement;
      if (!box) break;
      b = [...box.querySelectorAll('button')].find(x => /^\s*add( another)?\s*$/i.test(x.textContent));
    }
  }
  if (!b) return false;
  document.querySelectorAll(`[data-ja-add="${mark}"]`).forEach(x => x.removeAttribute('data-ja-add'));
  b.setAttribute('data-ja-add', mark);
  return true;
}"""


def _find_add(page, section: str) -> bool:
    heading = {"work": "work experience", "school": "education"}[section]
    return bool(page.evaluate(_ADD_JS, [f"add {heading}", heading, section]))



def _type_date(page, prefix: str, which: str, value) -> bool:
    parts = _month_year(value)
    month = page.query_selector(f'[id="{prefix}{which}-dateSectionMonth-input"]')
    year = page.query_selector(f'[id="{prefix}{which}-dateSectionYear-input"]')
    if not parts or month is None:
        return False
    # The month and year boxes sit under Workday's own display layer, which
    # takes the click (30 s timeouts at Abercrombie): focus them and type.
    _human(page, month)
    month.focus()
    page.keyboard.type(parts[0], delay=60)
    if year is not None:
        year.focus()
    page.keyboard.type(parts[1], delay=60)
    return True


def fill_work_experience(page, jobs: list[dict], result: FillResult) -> int:
    """Add each job under Work Experience when the step has the section and no jobs yet.

    Returns how many were added. A job a field would not take is left for the
    person, named in result.required_blank.
    """
    from backend.apply.accounts import _click
    if not jobs or page.query_selector('input[id$="--jobTitle"], input[name="jobTitle"]'):
        return 0                                  # no section here, or already filled
    if not _find_add(page, "work"):
        return 0
    added = 0
    for n, job in enumerate(jobs, 1):
        if n > 1 and not _find_add(page, "work"):
            break
        button = page.query_selector('[data-ja-add="work"]')
        if button is None or not _click(page, button):
            break
        try:
            page.wait_for_function(
                f"document.querySelectorAll('input[id$=\"--jobTitle\"]').length >= {n}",
                timeout=8000)
        except Exception:
            log.info("Workday work experience: no new entry appeared after Add")
            break
        title = page.query_selector_all('input[id$="--jobTitle"]')[-1]
        prefix = (title.get_attribute("id") or "")[:-len("jobTitle")]
        missed = []
        for key, field in (("title", "jobTitle"), ("company", "companyName"),
                           ("location", "location"), ("description", "roleDescription")):
            value = str(job.get(key) or "").strip()
            el = page.query_selector(f'[id="{prefix}{field}"]')
            if not value or el is None:
                if key in ("title", "company") and el is not None:
                    missed.append(key)
                continue
            try:
                _human(page, el)
                _type(el, value)
            except Exception:
                missed.append(key)
        current = bool(job.get("current")) or not job.get("to")
        if current:
            box = page.query_selector(f'[id="{prefix}currentlyWorkHere"]')
            if box is not None and not box.is_checked():
                _click(page, box)
                page.wait_for_timeout(300)
        try:
            if not _type_date(page, prefix, "startDate", job.get("from")):
                missed.append("from")
            if not current and not _type_date(page, prefix, "endDate", job.get("to")):
                missed.append("to")
        except Exception as exc:
            log.info("Workday work experience dates: %s", str(exc).splitlines()[0][:160])
            missed.append("dates")
        if missed:
            result.required_blank.append(f"Work Experience {n} ({', '.join(missed)})")
        result.filled[f"Work Experience {n}"] = f"{job.get('title')} at {job.get('company')}"
        added += 1
    log.info("Workday work experience: added %d of %d jobs", added, len(jobs))
    return added


def _type_year(page, el, year) -> bool:
    text = str(year or "").strip()[:4]
    if el is None or not re.fullmatch(r"\d{4}", text):
        return False
    _human(page, el)
    el.focus()
    page.keyboard.type(text, delay=60)
    return True


def fill_education(page, schools: list[dict], result: FillResult) -> int:
    """Each school in its own Education block, highest first: school, degree,
    field of study, years. Adds a block per school the form does not show yet.

    Filled entry by entry because every block asks the same questions: filled
    by label, the second block got the first school's answers.
    """
    from backend.apply.accounts import _click

    def blocks():
        return [el.get_attribute("id")[:-len("school")]
                for el in page.query_selector_all('input[id^="education-"][id$="--school"]')]

    if not schools or not (blocks() or _find_add(page, "school")):
        return 0
    while len(blocks()) < len(schools):
        had = len(blocks())
        button = page.query_selector('[data-ja-add="school"]') if _find_add(page, "school") else None
        if button is None or not _click(page, button):
            break
        try:
            page.wait_for_function(
                f"document.querySelectorAll('input[id^=\"education-\"][id$=\"--school\"]').length > {had}",
                timeout=8000)
        except Exception:
            log.info("Workday education: no new block appeared after Add")
            break
    done = 0
    for n, (prefix, school) in enumerate(zip(blocks(), schools), 1):
        missed = []
        box = page.query_selector(f'[id="{prefix}school"]')
        wrap = page.query_selector(f'[data-fkit-id="{prefix}school"] [data-automation-id="selectedItem"]')
        if box is not None and wrap is None and school.get("school"):
            _human(page, box)
            if not _choose_prompt(page, box, str(school["school"]), "School or University"):
                missed.append("school")
        degree = page.query_selector(f'[id="{prefix}degree"]')
        if degree is not None and school.get("degree") and \
                (degree.inner_text().strip().lower() in ("", "select one")):
            _human(page, degree)
            if not _choose_dropdown(page, degree, str(school["degree"]), "Degree"):
                missed.append("degree")
        field_box = page.query_selector(f'[id="{prefix}fieldOfStudy"]')
        chosen = page.query_selector(f'[data-fkit-id="{prefix}fieldOfStudy"] [data-automation-id="selectedItem"]')
        if field_box is not None and chosen is None and school.get("field"):
            _human(page, field_box)
            if not _choose_prompt(page, field_box, str(school["field"]), "Field of Study"):
                missed.append("field of study")
        for key, which in (("from", "firstYearAttended"), ("to", "lastYearAttended")):
            el = page.query_selector(f'[id="{prefix}{which}-dateSectionYear-input"]')
            if el is not None and not (el.input_value() or "").strip():
                if not _type_year(page, el, school.get(key)):
                    missed.append(key)
        if missed:
            result.required_blank.append(f"Education {n} ({', '.join(missed)})")
        result.filled[f"Education {n}"] = f"{school.get('degree')}, {school.get('school')}"
        done += 1
    log.info("Workday education: filled %d of %d", done, len(schools))
    return done


def _upload(page, el, path: Path) -> bool:
    """Attach the resume unless this step already shows one."""
    already = page.query_selector('[data-automation-id="file-upload-successful"], '
                                  '[data-automation-id="delete-file"]')
    if already is not None:
        return True
    try:
        el.set_input_files(str(path))
        page.wait_for_selector('[data-automation-id="file-upload-successful"], '
                               '[data-automation-id="delete-file"]', timeout=20_000)
        return True
    except Exception as exc:
        log.debug("Workday upload: %s", exc)
        return False


def _check_radio(page, tag: str) -> bool:
    """Pick one radio option and confirm it took. A forced `check` on Workday's
    radios can pass without the page seeing it (Abercrombie, Oct 2026), so click
    like a person -- the input, then its label -- and read the state back."""
    el = page.query_selector(f'[data-ja="{tag}"]')
    if el is None:
        return False
    for click in (lambda: el.click(),
                  lambda: el.evaluate("e => (document.querySelector(`label[for=\"${CSS.escape(e.id)}\"]`) || e).click()"),
                  lambda: el.check(force=True)):
        try:
            click()
            page.wait_for_timeout(300)
            if el.is_checked():
                return True
        except Exception as exc:
            log.debug("Workday radio: %s", exc)
    return False


def _human(page, el) -> None:
    """Bring a field into view and pause, as a person would before touching it.

    Filled in one pass at machine speed, the page jumped up and down as each
    field scrolled itself into view, radios last of all, back at the top.
    """
    try:
        el.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(random.randint(250, 550))


def _type(el, text: str) -> None:
    """Short answers typed key by key; a long description pasted, as people do."""
    if len(text) <= 80:
        el.fill("")
        el.type(text, delay=random.randint(25, 45))
    else:
        el.fill(text)


def note_given(page, result: FillResult) -> None:
    """Keep the answers the person typed or picked on this step, by question.

    Only theirs: what this tool filled is in result.filled, and the job and
    school entries are the profile's, not answers. answers.remember decides
    later which are short and general enough to reuse.
    """
    try:
        fields = page.evaluate(_COLLECT_JS) or []
    except Exception:
        return
    for f in fields:
        label = _bare(f.get("label") or "")
        if (not label or not f.get("value") or f["kind"] in ("file", "checkbox")
                or f.get("eid", "").startswith(("workExperience-", "education-"))
                or label[:60] in result.filled):
            continue
        result.given[label] = f["option"] if f["kind"] == "radio" else f["value"]


def fill_step(page, applicant: Applicant, resume: Path | None, result: FillResult) -> list[str]:
    """Fill what this step asks that the profile answers, top to bottom, one field
    at a time. Returns what is left for the person."""
    if signing_in(page):
        return []                    # an account page is never filled, whatever its fields
    fields = page.evaluate(_COLLECT_JS) or []
    waiting: list[str] = []
    radios: dict[str, list[dict]] = {}
    for f in fields:
        if f["kind"] == "radio":
            radios.setdefault(f["name"] or f["label"], []).append(f)
    for f in fields:
        if f["kind"] == "radio":
            group = radios.pop(f["name"] or f["label"], None)
            if group:                                 # the whole question, where it first appears
                _fill_radio(page, group, applicant, result, waiting)
            continue
        el = page.query_selector(f'[data-ja="{f["i"]}"]')
        label = _bare(f["label"])
        if el is None:
            continue
        if f.get("eid", "").startswith("workExperience-") or \
                (applicant.education and f.get("eid", "").startswith("education-")):
            continue                              # fill_work_experience / fill_education
        if f["kind"] == "file":
            other = any(w in label.lower() for w in ("cover", "transcript", "other", "additional"))
            already = any(a.startswith("resume=") for a in result.attachments)
            if resume is not None and Path(resume).exists() and not other and not already:
                if _upload(page, el, Path(resume)):
                    result.attachments.append(f"resume={Path(resume).name}")
                else:
                    waiting.append("resume upload")
            continue
        if f["value"]:
            continue                                  # the person's, or Workday's own
        if f["kind"] == "checkbox":
            if f["required"]:
                waiting.append(label[:60])
            continue
        answer = value_for(label, applicant)
        if answer is None:
            if _is_declaration(label):
                result.declarations.append(label[:90])
                waiting.append(label[:60])
            elif f["required"]:
                result.required_blank.append(label[:60])
                waiting.append(label[:60])
            else:
                result.left_blank.append(label[:60])
            continue
        done = False
        try:
            _human(page, el)
            if f["kind"] == "dropdown":
                done = _choose_dropdown(page, el, answer, label)
            elif f["kind"] == "prompt":
                done = _choose_prompt(page, el, answer, label)
            else:
                _type(el, answer)
                done = True
        except Exception as exc:
            log.debug("Workday field %r: %s", label, exc)
        if done:
            result.filled[label[:60]] = answer
        elif f["required"]:
            result.required_blank.append(label[:60])
            waiting.append(label[:60])
    return waiting


def _fill_radio(page, group: list[dict], applicant: Applicant, result: FillResult,
                waiting: list[str]) -> None:
    label = _bare(group[0]["label"])
    if any(r["value"] for r in group):
        return
    answer = value_for(label, applicant)
    hit = None if answer is None else _pick([r["option"] for r in group], answer)
    if hit is None:
        if _is_declaration(label):
            result.declarations.append(label[:90])
            waiting.append(label[:60])
        elif group[0]["required"]:
            result.required_blank.append(label[:60])
            waiting.append(label[:60])
        else:
            result.left_blank.append(label[:60])
        return
    el = page.query_selector(f'[data-ja="{group[hit]["i"]}"]')
    if el is not None:
        _human(page, el)
    if _check_radio(page, group[hit]["i"]):
        result.filled[label[:60]] = answer
    else:
        if group[0]["required"]:
            result.required_blank.append(label[:60])
        waiting.append(label[:60])


# -- the wizard --------------------------------------------------------------------

def _text(page, selector: str) -> str:
    try:
        el = page.query_selector(selector)
        return (el.inner_text() or "").strip() if el else ""
    except Exception:
        return ""


def current_step(page) -> str:
    """The step's name: its text is "current step 1 of 7" over the name."""
    lines = [l.strip() for l in _text(page, '[data-automation-id="progressBarActiveStep"]').splitlines()]
    lines = [l for l in lines if l and not re.match(r"current step \d+ of \d+$", l, re.I)]
    return lines[-1] if lines else ""


def step_ready(page) -> bool:
    """The step has drawn its form: a Save and Continue (or Submit) button and no loading placeholder."""
    try:
        if page.query_selector('[data-automation-id="loadingSpinner"], [aria-busy="true"]'):
            return False
    except Exception:
        pass
    return _next_button(page) is not None or on_review(page)


def _next_button(page):
    for selector in NEXT_BUTTONS:
        el = page.query_selector(selector)
        if el is not None and el.is_visible():
            return el
    for el in page.query_selector_all("button"):
        try:
            if el.is_visible() and re.fullmatch(r"(save and continue|next|continue)",
                                                (el.inner_text() or "").strip(), re.I):
                return el
        except Exception:
            continue
    return None


def on_review(page) -> bool:
    if "review" in current_step(page).lower():
        return True
    button = _next_button(page)
    label = (button.inner_text() if button else "") or ""
    return label.strip().lower() == "submit"


def signing_in(page) -> bool:
    """Is this the account page? By its step name as well as its form: the
    progress bar arrives before the sign-in form does, and a sign-in page
    taken for a step would get the person's email typed into it."""
    step = current_step(page).lower()
    if "sign in" in step or "create account" in step or "sign-in" in step:
        return True
    return page.query_selector('[data-automation-id="signInContent"], '
                               '[data-automation-id="signInFormo"], '
                               '[data-automation-id="createAccountSubmitButton"], '
                               'input[type="password"]') is not None


def is_confirmed(page, reached_review: bool) -> bool:
    try:
        text = (page.inner_text("body") or "").lower()
        url = (page.url or "").lower()
    except Exception:
        return False
    if any(marker in text for marker in CONFIRM_MARKERS):
        return True
    return reached_review and "/userhome" in url


def open_application(page, url: str) -> str | None:
    """Job page -> Apply -> Apply Manually. None when there, else what went wrong."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_selector('[data-automation-id="adventureButton"], '
                               '[data-automation-id="progressBar"]', timeout=PAGE_TIMEOUT_MS)
    except Exception as exc:
        return f"could not open the posting ({str(exc)[:80]})"
    apply = page.query_selector('[data-automation-id="adventureButton"]')
    if apply is not None:
        text = (apply.inner_text() or "").strip().lower()
        if "applied" in text:
            return "already applied on Workday"
        apply.click()
        try:
            page.wait_for_selector('[data-automation-id="applyManually"], '
                                   '[data-automation-id="progressBar"]', timeout=15_000)
        except Exception:
            return "the Apply button opened nothing"
        manual = page.query_selector('[data-automation-id="applyManually"]')
        if manual is not None:
            manual.click()
    try:
        page.wait_for_selector('[data-automation-id="progressBar"]', timeout=PAGE_TIMEOUT_MS)
    except Exception:
        return "the application did not start"
    return None


def apply(page, job_id: int, url: str | None, applicant: Applicant, resume: Path | None,
          *, evidence: Path | None = None, wait_s: float = 900, say=print,
          clock=time.monotonic) -> tuple[str, FillResult]:
    """Run one Workday application with the person. ("submitted"|"skipped"|"timeout"|"not-filled", result).

    One loop fills and waits: each step is filled once, the first time it is
    shown -- whether we moved there or the person did -- and the person can
    take over and hand back at any point just by moving through the form.
    `url` None means the page is already on the application (tests).
    """
    result = FillResult(job_id=job_id, url=url or "")
    evidence = evidence or _evidence_dir(job_id)
    if url:
        problem = open_application(page, url)
        if problem:
            result.error = problem
            return "not-filled", result

    done_steps: set[str] = set()
    told: set[str] = set()
    account_state: dict = {}
    reached_review = False
    deadline = clock() + wait_s
    while clock() < deadline:
        try:
            if page.is_closed():
                return "skipped", result
        except Exception:
            return "skipped", result
        if is_confirmed(page, reached_review):
            return "submitted", result

        if signing_in(page):
            # The saved job-site account signs in, or fills a new account for
            # the person to agree to and create (backend/apply/accounts.py).
            from backend.apply.accounts import workday_sign_in
            try:
                done = workday_sign_in(page, account_state, say)
            except Exception as exc:          # the page redrew under us: look again next round
                log.debug("Workday sign-in: %s", exc)
                done = "waiting"
            if done == "no-account-saved" and "signin" not in told:
                say("   Sign in to this employer's Workday (or create your account) in the "
                    "browser — I'll carry on from there. Save a job-site email and password "
                    "in Settings and I'll do this for you next time.")
                told.add("signin")
        elif on_review(page):
            note_given(page, result)
            if not reached_review:
                reached_review = True
                _shot(page, evidence, "review")
                say("   On the Review page — check it and press Submit. "
                    "Close the tab to skip this one.")
        else:
            note_given(page, result)                 # before the person moves on
            step = current_step(page) or "form"
            if step not in done_steps and len(done_steps) < STEP_LIMIT:
                page.wait_for_timeout(1500)          # let the step finish drawing
                if signing_in(page) or current_step(page) != step:
                    continue
                if not step_ready(page):
                    # Still the loading placeholder (Abercrombie, Oct 2026): filling now
                    # would mark the step done with nothing in it. Look again next round.
                    page.wait_for_timeout(POLL_MS)
                    continue
                done_steps.add(step)
                before = len(result.required_blank)
                fill_work_experience(page, work_history(applicant, resume), result)
                fill_education(page, applicant.education, result)
                left = result.required_blank[before:]       # a job or school field it could not set
                waiting = left + fill_step(page, applicant, resume, result)
                _shot(page, evidence, step)
                if waiting:
                    say(f"   {step}: needs you — {', '.join(dict.fromkeys(waiting))[:200]}. "
                        f"Press Save and Continue when done; I'll fill the next step.")
                else:
                    stuck = _advance(page)
                    if stuck:
                        say(f"   {step}: Workday wants more — {stuck[:200]}. "
                            f"Fix it and press Save and Continue.")
                    else:
                        say(f"   {step}: filled ✓")
                        continue                  # straight on to the next step
        try:
            page.wait_for_timeout(POLL_MS)
        except Exception:
            return "skipped", result
    return "timeout", result


def _advance(page) -> str:
    """Press Save and Continue. '' when the step changed, else what Workday said."""
    before = current_step(page)
    button = _next_button(page)
    if button is None:
        return "no Save and Continue button"
    try:
        button.click()
    except Exception as exc:
        return f"could not press Save and Continue ({str(exc)[:60]})"
    for _ in range(20):
        page.wait_for_timeout(500)
        if current_step(page) != before or on_review(page):
            return ""
    try:
        errors = page.evaluate(_ERRORS_JS) or []
    except Exception:
        errors = []
    return "; ".join(dict.fromkeys(errors)) or "the step did not move on"


def _shot(page, evidence: Path, step: str) -> None:
    try:
        name = re.sub(r"[^a-z0-9]+", "-", step.lower()).strip("-") or "step"
        page.screenshot(path=str(evidence / f"workday-{name}.png"), full_page=True)
        # The page as drawn, for working out a tenant's fields after the fact.
        (evidence / f"workday-{name}.html").write_text(page.content())
    except Exception as exc:
        log.debug("Workday screenshot: %s", exc)
