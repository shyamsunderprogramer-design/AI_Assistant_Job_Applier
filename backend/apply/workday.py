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
  return out;
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
    if "country" in words and "phone" not in words:
        return str(applicant.location.get("country") or "").strip() or None
    school = applicant.education[0] if applicant.education else {}
    short = len(text) <= 40                 # a field's name, not a question ("did you finish high school?")
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
        if not _click(page, el):
            log.info("Workday %r: the dropdown would not open", label)
            return False
        page.wait_for_timeout(800)
        opts = _options(page, '[role="listbox"] [role="option"]')
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


def _choose_prompt(page, el, answer: str, label: str = "") -> bool:
    from backend.apply.accounts import _click
    try:
        _click(page, el)
        el.fill(answer)
        el.press("Enter")
        opts, texts = [], []
        for _ in range(8):                      # a school search can take a few seconds
            page.wait_for_timeout(750)
            opts = _options(page, '[data-automation-id="promptOption"], [role="option"]')
            texts = [o.inner_text() for o in opts]
            if texts:
                break
        hit = _pick(texts, answer)
        if hit is None:
            log.info("Workday %r: no option says %r — options: %s", label, answer,
                     "; ".join(t.strip() for t in texts)[:400] or "none shown")
            el.fill("")
            page.keyboard.press("Escape")
            return False
        _click(page, opts[hit])
        page.wait_for_timeout(400)
        return True
    except Exception as exc:
        log.info("Workday prompt %r = %r: %s", label, answer, str(exc).splitlines()[0][:160])
        return False


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


def fill_step(page, applicant: Applicant, resume: Path | None, result: FillResult) -> list[str]:
    """Fill what this step asks that the profile answers. Returns what is left for the person."""
    if signing_in(page):
        return []                    # an account page is never filled, whatever its fields
    fields = page.evaluate(_COLLECT_JS) or []
    waiting: list[str] = []
    radios: dict[str, list[dict]] = {}
    for f in fields:
        if f["kind"] == "radio":
            radios.setdefault(f["name"] or f["label"], []).append(f)
            continue
        el = page.query_selector(f'[data-ja="{f["i"]}"]')
        label = _bare(f["label"])
        if el is None:
            continue
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
            if f["kind"] == "dropdown":
                done = _choose_dropdown(page, el, answer, label)
            elif f["kind"] == "prompt":
                done = _choose_prompt(page, el, answer, label)
            else:
                el.fill(answer)
                done = True
        except Exception as exc:
            log.debug("Workday field %r: %s", label, exc)
        if done:
            result.filled[label[:60]] = answer
        elif f["required"]:
            result.required_blank.append(label[:60])
            waiting.append(label[:60])

    for group in radios.values():
        label = _bare(group[0]["label"])
        if any(r["value"] for r in group):
            continue
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
            continue
        if _check_radio(page, group[hit]["i"]):
            result.filled[label[:60]] = answer
        else:
            if group[0]["required"]:
                result.required_blank.append(label[:60])
            waiting.append(label[:60])
    return waiting


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
            if not reached_review:
                reached_review = True
                _shot(page, evidence, "review")
                say("   On the Review page — check it and press Submit. "
                    "Close the tab to skip this one.")
        else:
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
                waiting = fill_step(page, applicant, resume, result)
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
    except Exception as exc:
        log.debug("Workday screenshot: %s", exc)
