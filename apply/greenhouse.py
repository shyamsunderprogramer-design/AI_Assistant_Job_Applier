"""Fill a Greenhouse application form, and stop where a person is required.

Built against the real markup of five live forms (attentive, grvty, braze,
intersystems, alpaca) rather than from documentation, because Greenhouse forms
are half standard and half per-employer:

  * The known fields have stable ids — `first_name`, `last_name`, `email`,
    `phone`, `resume`, `cover_letter`. Those can be filled by id.
  * The employer's own questions have generated ids (`question_7571117009`),
    different on every posting. Those can only be matched by their label text,
    which is why `Applicant.answer_for` takes a label rather than a field name.

**Every one of those five forms carried a reCAPTCHA.** That is the reason this
module fills and stops rather than submits. Solving or evading a CAPTCHA is
not something this project does (README §C9), so the last step belongs to the
person — which also means they see what is about to go out under their name.
The filling is the slow part; the clicking is not.

Three rules the filler will not bend:

**A rejected tailoring is never attached.** If the fabrication guard turned
down the resume for this posting, there is nothing safe to upload, and the
application is skipped with the reason. This is the whole point of the guard.

**A declaration is never answered from a template.** Work authorisation,
sponsorship and clearance are left blank unless the profile states them
outright. The form filler flags them so the person can see what is waiting.

**A question this profile does not cover is left empty**, never approximated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from apply.profile import DECLARATION_PATTERNS, Applicant

log = logging.getLogger(__name__)

# Long enough for a slow form, short enough that a hung page does not stall a
# run of fifty.
PAGE_TIMEOUT_MS = 45_000
SETTLE_MS = 2_500

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# The fields Greenhouse names consistently across employers.
KNOWN_FIELDS = {
    "first_name": lambda a: a.first_name,
    "last_name": lambda a: a.last_name,
    "email": lambda a: a.email,
    "phone": lambda a: a.phone,
}


@dataclass
class FillResult:
    """What happened to one application, in enough detail to act on."""

    job_id: int
    url: str
    filled: dict[str, str] = field(default_factory=dict)
    left_blank: list[str] = field(default_factory=list)
    declarations: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    captcha: bool = False
    submitted: bool = False
    screenshot: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def summary(self) -> str:
        if self.error:
            return f"not filled: {self.error}"
        parts = [f"{len(self.filled)} fields", f"{len(self.attachments)} files"]
        if self.declarations:
            parts.append(f"{len(self.declarations)} declarations for you")
        if self.left_blank:
            parts.append(f"{len(self.left_blank)} blank")
        parts.append("CAPTCHA — submit is yours" if self.captcha
                     else ("submitted" if self.submitted else "ready to submit"))
        return " · ".join(parts)


def _is_declaration(label: str) -> bool:
    text = (label or "").lower()
    return any(pattern in text for pattern in DECLARATION_PATTERNS)


def _evidence_dir(job_id: int) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = Path("apply/runs") / f"{job_id}-{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def fill(page, job_id: int, url: str, applicant: Applicant,
         resume_path: Path | None = None, letter_path: Path | None = None,
         *, submit: bool = False) -> FillResult:
    """Fill one Greenhouse form. Does not submit unless explicitly told to.

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
        if target in KNOWN_FIELDS or target in ("resume", "cover_letter"):
            continue

        if _is_declaration(label):
            # Answer only if the profile states it outright; never infer.
            stated = applicant.answer_for(label)
            if stated is None:
                result.declarations.append(label[:90])
                continue

        answer = applicant.answer_for(label)
        if answer is None:
            result.left_blank.append(label[:60])
            continue
        try:
            element = page.query_selector(f"#{target}")
            if element is not None:
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

    # -- is a person required? --------------------------------------------
    try:
        html = page.content().lower()
        result.captcha = "g-recaptcha" in html or "recaptcha" in html or "hcaptcha" in html
    except Exception:
        result.captcha = False

    # -- keep the evidence -------------------------------------------------
    try:
        shot = _evidence_dir(job_id) / "form.png"
        page.screenshot(path=str(shot), full_page=True)
        result.screenshot = str(shot)
    except Exception as exc:
        log.debug("Greenhouse %s: no screenshot (%s)", job_id, exc)

    # -- submit, only when everything says it is safe to ------------------
    if submit and not result.captcha and not result.declarations:
        try:
            button = page.query_selector("button[type=submit], input[type=submit]")
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

    return result


def browser_page(playwright, *, headless: bool = False):
    """One browser page, configured the same way for every application.

    Headless is OFF by default: a CAPTCHA and a Submit button are waiting for a
    person on nearly every Greenhouse form, and a window they can see and
    finish in is the whole workflow, not a debugging aid.
    """
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(user_agent=USER_AGENT,
                                  viewport={"width": 1280, "height": 1600})
    return browser, context.new_page()
