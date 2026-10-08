"""Fill a Greenhouse application form, and stop where a person is required.

Built against the real markup of live forms (attentive, grvty, braze,
intersystems, alpaca, and in Sep 2026 aegworldwide and five9) rather than from
documentation, because Greenhouse forms are half standard and half per-employer:

  * The known fields have stable ids — `first_name`, `last_name`, `email`,
    `phone`, `resume`, `cover_letter`. Those can be filled by id.
  * The employer's own questions have generated ids (`question_7571117009`),
    different on every posting. Those can only be matched by their label text,
    which is why `Applicant.answer_for` takes a label rather than a field name.
  * Many questions are not text boxes but dropdowns (`role=combobox`): Country,
    Location, every Yes/No, the EEO questions. Typing into one filters its list
    but chooses nothing, and the form then fails validation on that field. A
    dropdown is answered by picking the option that matches, or not at all.
  * A required field says so twice: `aria-required` on the input and a `*` on
    its label.

**Every form sampled carried a reCAPTCHA.** That is the reason this module
fills and stops rather than submits. Solving or evading a CAPTCHA is not
something this project does (README §C9), so the last step belongs to the
person — which also means they see what is about to go out under their name.
The filling is the slow part; the clicking is not.

Three rules the filler will not bend:

**A rejected tailoring is never attached.** If the fabrication guard turned
down the resume for this posting, the tailored file does not exist, and the
caller attaches the base resume — which is the person's own words — instead.

**A declaration is never answered from a template.** Work authorisation,
sponsorship and clearance are left blank unless the profile states them
outright. The form filler flags them so the person can see what is waiting.

**A question this profile does not cover is left empty**, never approximated.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from backend.apply.profile import DECLARATION_PATTERNS, Applicant
from backend.config.loader import PROJECT_ROOT

log = logging.getLogger(__name__)

# Long enough for a slow form, short enough that a hung page does not stall a
# run of fifty.
PAGE_TIMEOUT_MS = 45_000
SETTLE_MS = 2_500

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

RUNS_DIR = PROJECT_ROOT / "backend" / "apply" / "runs"

# The fields Greenhouse names consistently across employers.
KNOWN_FIELDS = {
    "first_name": lambda a: a.first_name,
    "last_name": lambda a: a.last_name,
    "email": lambda a: a.email,
    "phone": lambda a: a.phone,
}

# Dropdowns Greenhouse names consistently. Answered by picking an option.
KNOWN_CHOICES = {
    "country": lambda a: str(a.location.get("country") or "").strip(),
    "candidate-location": lambda a: str(a.location.get("city") or "").strip(),
    "gender": lambda a: str(a.demographics.get("gender") or "").strip(),
    "hispanic_ethnicity": lambda a: str(a.demographics.get("hispanic_latino") or "").strip(),
    "race": lambda a: str(a.demographics.get("race_ethnicity") or "").strip(),
    "veteran_status": lambda a: str(a.demographics.get("veteran_status") or "").strip(),
    "disability_status": lambda a: str(a.demographics.get("disability_status") or "").strip(),
}

# The field ids the filler handles itself, never through the label pass.
HANDLED = set(KNOWN_FIELDS) | set(KNOWN_CHOICES) | {
    "resume", "cover_letter", "resume_text", "cover_letter_text", "iti-0__search-input"}

# How each EEO form phrases "I'd rather not say". The profile says "decline";
# the option never does, quite.
DECLINE_PHRASES = ("decline", "don't wish", "do not wish", "prefer not",
                   "not wish to", "choose not")

# What the page says once an application has gone through.
CONFIRM_MARKERS = ("thank you for applying", "thanks for applying",
                   "application has been received", "application was submitted",
                   "received your application", "application has been submitted")


def form_url(slug: str, external_id: str) -> str:
    """The Greenhouse form for one posting, wherever the employer hosts it.

    A posting's own link is often the employer's careers page, which frames
    the form in an iframe the filler cannot reach — and job-boards.greenhouse.io
    redirects there too when the employer has one. The embed URL is the form
    itself, with no page around it, for every Greenhouse employer.
    """
    return (f"https://job-boards.greenhouse.io/embed/job_app"
            f"?for={quote(slug)}&token={quote(str(external_id))}")


@dataclass
class FillResult:
    """What happened to one application, in enough detail to act on."""

    job_id: int
    url: str
    filled: dict[str, str] = field(default_factory=dict)
    left_blank: list[str] = field(default_factory=list)
    required_blank: list[str] = field(default_factory=list)
    declarations: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    captcha: bool = False
    submitted: bool = False
    screenshot: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def needs_person(self) -> bool:
        """Is there anything only the person can do before this goes out?"""
        return bool(self.captcha or self.declarations or self.required_blank)

    def summary(self) -> str:
        if self.error:
            return f"not filled: {self.error}"
        parts = [f"{len(self.filled)} fields", f"{len(self.attachments)} files"]
        if self.declarations:
            parts.append(f"{len(self.declarations)} declarations for you")
        if self.required_blank:
            parts.append(f"{len(self.required_blank)} required for you")
        elif self.left_blank:
            parts.append(f"{len(self.left_blank)} blank")
        parts.append("CAPTCHA — submit is yours" if self.captcha
                     else ("submitted" if self.submitted else "ready to submit"))
        return " · ".join(parts)


def _input_type(element) -> str:
    try:
        return (element.get_attribute("type") or "").lower()
    except Exception:
        return ""


def _is_declaration(label: str) -> bool:
    text = (label or "").lower()
    return any(pattern in text for pattern in DECLARATION_PATTERNS)


def _is_required(label: str, element=None) -> bool:
    if (label or "").rstrip().endswith("*"):
        return True
    try:
        return element is not None and element.get_attribute("aria-required") == "true"
    except Exception:
        return False


def _is_dropdown(element) -> bool:
    try:
        return element.get_attribute("role") == "combobox"
    except Exception:
        return False


def _evidence_dir(job_id: int) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / f"{job_id}-{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


# A right-to-work status, matched by what the option says, since every employer
# words it differently ("I am a U.S. citizen", "US Citizen or National").
STATUS_WORDS = {
    "u.s. citizen": (("citizen",), ("not a", "non-citizen", "non citizen", "noncitizen", "not citizen", "other than")),
    "permanent resident": (("permanent resident", "green card", "lawful permanent"), ("not a", "non-")),
    "visa": (("visa", "sponsor", "h-1b", "h1b"), ("not require", "do not", "don't", "will not", "no sponsorship")),
}


def status_option(texts: list[str], want: str) -> int | None:
    """The one option that says this status, or None when none or several do."""
    say, never = STATUS_WORDS[want]
    hits = [i for i, t in enumerate(texts) if any(s in t for s in say) and not any(n in t for n in never)]
    return hits[0] if len(hits) == 1 else None


def _choose_status(page, element, want: str, settle_ms: int) -> bool:
    try:
        element.click()
        page.wait_for_timeout(settle_ms)
        options = page.query_selector_all("[role=option]")
        index = status_option([(o.inner_text() or "").strip().lower() for o in options], want)
        if index is None:
            element.press("Escape")
            return False
        options[index].click()
        return True
    except Exception as exc:
        log.debug("Status dropdown %r could not be answered: %s", want, exc)
        return False


def _choose(page, element, answer: str, *, settle_ms: int = 700, label: str = "") -> bool:
    """Pick the dropdown option that says `answer`. False, and nothing chosen,
    when no option does.

    Exact text first, then an option starting with it ("Austin" ->
    "Austin, Texas, United States"). "decline" matches whatever this form calls
    declining. Anything looser than that is a guess, and a guessed answer on an
    application cannot be told apart from a considered one.
    """
    want = answer.strip().lower()
    if not want:
        return False
    if want in STATUS_WORDS:
        return _choose_status(page, element, want, settle_ms)
    declining = want == "decline"
    try:
        element.click()
        if not declining:
            element.fill(answer)          # filters the list; chooses nothing
        page.wait_for_timeout(settle_ms)
        options = page.query_selector_all("[role=option]")
        texts = [(opt, (opt.inner_text() or "").strip().lower()) for opt in options]

        if declining:
            pick = next((o for o, t in texts if any(p in t for p in DECLINE_PHRASES)), None)
        else:
            pick = (next((o for o, t in texts if t == want), None)
                    or next((o for o, t in texts if t.startswith(want)), None))
        if pick is None and label and not declining:
            # A general answer ("No") in this form's own words ("I have never worked at X"):
            # the full list, unfiltered, and only an option that clearly says the same thing.
            from backend.apply.general import pick_option
            element.fill("")
            page.wait_for_timeout(settle_ms)
            options = page.query_selector_all("[role=option]")
            index = pick_option(label, answer, [(o.inner_text() or "").strip() for o in options])
            pick = options[index] if index is not None else None
        if pick is None:
            element.press("Escape")
            return False
        pick.click()
        return True
    except Exception as exc:
        log.debug("Dropdown %r could not be answered: %s", answer, exc)
        return False


def fill(page, job_id: int, url: str, applicant: Applicant,
         resume_path: Path | None = None, letter_path: Path | None = None,
         *, submit: bool = False, evidence: Path | None = None) -> FillResult:
    """Fill one Greenhouse form. Does not submit unless explicitly told to.

    `evidence` is the folder the caller already keeps the resume in; the
    screenshot goes beside it, so one run is one folder.

    `page` is a Playwright page, passed in so a caller can reuse one browser
    across fifty applications and so this is testable with a fake.
    """
    result = FillResult(job_id=job_id, url=url)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(SETTLE_MS)
    except Exception as exc:
        result.error = f"could not open the form ({str(exc)[:80]})"
        return result

    # -- the fields Greenhouse names the same way everywhere ---------------
    for field_id, value_of in KNOWN_FIELDS.items():
        value = value_of(applicant)
        if not value:
            result.left_blank.append(field_id)
            continue
        try:
            element = page.query_selector(f"#{field_id}")
            if element is None:
                continue
            element.fill(value)
            result.filled[field_id] = value
        except Exception as exc:                        # one field, not the form
            log.debug("Greenhouse %s: %s could not be filled: %s", job_id, field_id, exc)

    for field_id, value_of in KNOWN_CHOICES.items():
        try:
            element = page.query_selector(f"#{field_id}")
        except Exception:
            element = None
        if element is None:
            continue
        value = value_of(applicant)
        settle = 1500 if field_id == "candidate-location" else 700   # searched live
        if value and _choose(page, element, value, settle_ms=settle):
            result.filled[field_id] = value
        elif _is_required("", element):
            result.required_blank.append(field_id)

    # -- the employer's own questions, matched by their label --------------
    try:
        questions = page.eval_on_selector_all(
            "label",
            """els => els.map(e => ({
                 text: (e.innerText || '').trim(),
                 id: e.getAttribute('for') || ''
               })).filter(q => q.id && q.text)""",
        )
    except Exception:
        questions = []

    for question in questions:
        label, target = question["text"], question["id"]
        if target in HANDLED:
            continue
        try:
            element = page.query_selector(f"#{target}")
        except Exception:
            element = None
        required = _is_required(label, element)

        if _is_declaration(label):
            # Answer only if the profile states it outright; never infer.
            if applicant.answer_for(label) is None:
                result.declarations.append(label[:90])
                continue

        answer = applicant.answer_for(label)
        if answer is None:
            result.left_blank.append(label[:60])
            if required:
                result.required_blank.append(label[:60])
            continue
        if element is None:
            continue
        try:
            if _is_dropdown(element):
                if not _choose(page, element, answer, label=label):
                    result.left_blank.append(label[:60])
                    if required:
                        result.required_blank.append(label[:60])
                    continue
            else:
                if " – " in answer and _input_type(element) == "number":
                    # A box that takes only a number: "$160,000 – $190,000" becomes its middle.
                    from backend.apply.salary import single_figure
                    answer = single_figure(answer) or answer
                element.fill(answer)
            result.filled[label[:60]] = answer
        except Exception as exc:
            log.debug("Greenhouse %s: %r could not be filled: %s", job_id, label, exc)

    # -- the documents -----------------------------------------------------
    for field_id, path in (("resume", resume_path), ("cover_letter", letter_path)):
        if path is None or not Path(path).exists():
            continue
        try:
            element = page.query_selector(f"input#{field_id}[type=file]")
            if element is not None:
                element.set_input_files(str(path))
                result.attachments.append(f"{field_id}={Path(path).name}")
                page.wait_for_timeout(800)
        except Exception as exc:
            log.warning("Greenhouse %s: could not attach %s: %s", job_id, field_id, exc)
    if resume_path is not None and not any(a.startswith("resume=") for a in result.attachments):
        result.required_blank.append("resume")      # never send one without it

    # -- is a person required? --------------------------------------------
    # Only a CAPTCHA a person must SOLVE stops the submit. Greenhouse's is
    # reCAPTCHA Enterprise in score mode (`render=<key>`, a corner badge, no
    # checkbox): it asks nothing of anyone and judges the browser when Submit
    # is pressed, exactly as it does for a person. Treating it as a stop is
    # why nothing was ever submitted. hCaptcha and checkbox reCAPTCHA remain
    # a stop, and so does any challenge that appears after the click.
    try:
        html = page.content().lower()
        present = "recaptcha" in html or "hcaptcha" in html
        result.captcha = present and not captcha_is_invisible(html)
    except Exception:
        result.captcha = False

    # -- keep the evidence -------------------------------------------------
    try:
        shot = (evidence or _evidence_dir(job_id)) / "form.png"
        page.screenshot(path=str(shot), full_page=True)
        result.screenshot = str(shot)
    except Exception as exc:
        log.debug("Greenhouse %s: no screenshot (%s)", job_id, exc)

    # -- the person presses Submit: show them where it is ----------------
    if not (submit and not result.needs_person):
        show_submit(page, scroll=not result.needs_person)

    # -- submit, only when everything says it is safe to ------------------
    if submit and not result.needs_person:
        try:
            button = page.query_selector(SUBMIT_SELECTOR)
            if button is not None:
                button.click()
                page.wait_for_timeout(4000)
                result.submitted = True
        except Exception as exc:
            result.error = f"submit failed ({str(exc)[:80]})"
    elif submit and result.captcha:
        log.info("Greenhouse %s: filled, but a CAPTCHA guards Submit — left for you",
                 job_id)
    elif submit and result.declarations:
        log.info("Greenhouse %s: filled, but %d declaration(s) need you: %s",
                 job_id, len(result.declarations), "; ".join(result.declarations[:2]))
    elif submit and result.required_blank:
        log.info("Greenhouse %s: filled, but %d required question(s) need you: %s",
                 job_id, len(result.required_blank), "; ".join(result.required_blank[:2]))

    return result


SUBMIT_SELECTOR = "button[type=submit], input[type=submit]"


def show_submit(page, scroll: bool = True) -> bool:
    """Outline the Submit button, and bring it into view when nothing above it needs the person.

    The form ran to about 6,600 pixels with the button at the very bottom, and a
    person finishing it by hand did not find it. Only the button's look changes.
    """
    try:
        return bool(page.evaluate(
            """([selector, scroll]) => {
                 const b = document.querySelector(selector);
                 if (!b) return false;
                 b.style.outline = '4px solid #f59e0b';
                 b.style.outlineOffset = '4px';
                 if (scroll) b.scrollIntoView({block: 'center', behavior: 'smooth'});
                 return true;
               }""", [SUBMIT_SELECTOR, scroll]))
    except Exception as exc:
        log.debug("Submit button not shown: %s", exc)
        return False


def captcha_is_invisible(html: str) -> bool:
    """Score-only reCAPTCHA: loaded with a site key to render, no checkbox."""
    html = (html or "").lower()
    if "hcaptcha" in html or "not a robot" in html:
        return False
    return bool(re.search(r"recaptcha/(enterprise|api)\.js\?render=(?!explicit)[\w-]+", html))


def challenge_shown(page) -> bool:
    """Did a CAPTCHA puzzle appear after Submit? Then it is the person's."""
    try:
        return bool(page.evaluate("""() => [...document.querySelectorAll('iframe')]
            .some(f => /recaptcha\\/.*bframe|hcaptcha/.test(f.src) && f.offsetHeight > 100)"""))
    except Exception:
        return False


# Read back every labelled answer on the form, as the person left it.
_SNAPSHOT_JS = """() => {
  const out = {};
  for (const label of document.querySelectorAll('label[for]')) {
    const el = document.getElementById(label.getAttribute('for'));
    const text = (label.innerText || '').trim();
    if (!el || !text || el.type === 'file') continue;
    let value = '';
    if (el.getAttribute('role') === 'combobox') {
      const box = el.closest('[class*="control"]') || el.parentElement;
      value = (box ? box.innerText : '').trim();
      if (/^select\\.\\.\\.$/i.test(value)) value = '';
    } else if (el.type === 'checkbox' || el.type === 'radio') {
      value = el.checked ? 'Yes' : '';
    } else {
      value = (el.value || '').trim();
    }
    if (value) out[text] = value;
  }
  return out;
}"""


def snapshot_answers(page) -> dict[str, str]:
    try:
        return page.evaluate(_SNAPSHOT_JS) or {}
    except Exception:
        return {}


def is_confirmed(page) -> bool:
    """Does the page say the application went through?"""
    try:
        if "confirmation" in (page.url or "").lower():
            return True
        text = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    return any(marker in text for marker in CONFIRM_MARKERS)


def watch_submission(page, timeout_s: float, *, poll_ms: int = 2000,
                     clock=time.monotonic) -> tuple[str, dict[str, str]]:
    """Wait for the person to finish this form, noting what they answered.

    Returns ("submitted" | "skipped" | "timeout", answers). The answers are the
    last reading taken before the confirmation page replaced the form — what
    the person actually sent.

    Closing the tab is how a person says "not this one" — the one gesture that
    needs no instructions and works however the form looks.
    """
    answers: dict[str, str] = {}
    deadline = clock() + timeout_s
    while clock() < deadline:
        try:
            if page.is_closed():
                return "skipped", answers
        except Exception:
            return "skipped", answers
        if is_confirmed(page):
            return "submitted", answers
        answers = snapshot_answers(page) or answers
        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            return "skipped", answers    # the tab went while we waited
    return "timeout", answers


def await_submission(page, timeout_s: float, **kw) -> str:
    """`watch_submission` without the answers."""
    return watch_submission(page, timeout_s, **kw)[0]


def browser_page(playwright, *, headless: bool = False):
    """One browser page, configured the same way for every application.

    Headless is OFF by default: a CAPTCHA and a Submit button are waiting for a
    person on nearly every Greenhouse form, and a window they can see and
    finish in is the whole workflow, not a debugging aid.
    """
    if headless:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 1600})
    else:
        # A window a person scrolls: the page follows the window (see helper.launch).
        from backend.apply.helper import WINDOW_HEIGHT
        browser = playwright.chromium.launch(headless=False, args=[f"--window-size=1280,{WINDOW_HEIGHT}"])
        context = browser.new_context(user_agent=USER_AGENT, no_viewport=True)
    return browser, context.new_page()
