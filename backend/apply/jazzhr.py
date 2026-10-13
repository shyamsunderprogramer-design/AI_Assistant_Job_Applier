"""JazzHR application forms (<company>.applytojob.com/apply/<id>/...): filled, never submitted.

A JazzHR form is one page and names its fields the same way for every
employer: "resumator-firstname-value", "resumator-resume-value", and the
employer's own questions as "resumator-questionnaire-q<number>", each with a
label. So the filler reads the labels, answers what the profile and the
person's saved answers cover, and leaves the rest -- and the "Human Check"
reCAPTCHA and the Submit button -- to the person, like Workday.

Answers come from, in order:
  * the profile (name, email, phone, city, state, zip, sponsorship, citizenship,
    living in the states the question lists);
  * the person's own answers for this job, given on the Apply page (an essay
    drafted from the resume and approved there arrives this way);
  * for "How many years of experience in DevOps...": the resume's own dates for
    the jobs whose titles name that field, matched to the form's ranges
    ("8+ Years") -- never a number the resume does not support.

Fields are filled one at a time with a short pause between them, as a
person would, and every field is left visible for the person to check.
"""

from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path

from backend.apply.greenhouse import FillResult, _evidence_dir, snapshot_answers
from backend.apply.profile import Applicant

log = logging.getLogger(__name__)

PAGE_TIMEOUT_MS = 45_000
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# Fields JazzHR names the same way on every form.
FIELDS = {
    "resumator-firstname-value": lambda a: a.first_name,
    "resumator-lastname-value": lambda a: a.last_name,
    "resumator-email-value": lambda a: a.email,
    "resumator-phone-value": lambda a: a.phone,
    "resumator-address-value": lambda a: str(a.location.get("street") or a.location.get("address") or ""),
    "resumator-city-value": lambda a: str(a.location.get("city") or ""),
    "resumator-state-value": lambda a: str(a.location.get("state") or ""),
    "resumator-postal-value": lambda a: str(a.location.get("postal_code") or ""),
    "resumator-linkedin-value": lambda a: str(a.links.get("linkedin") or ""),
    "resumator-website-value": lambda a: str(a.links.get("portfolio") or a.links.get("website") or ""),
}
CONFIRM_TEXT = ("thank you for applying", "thanks for applying", "application has been submitted",
                "application was submitted", "received your application", "thank you for your application",
                "your application has been received", "successfully submitted")


def is_form_url(url: str) -> bool:
    return bool(re.match(r"https://[\w-]+\.applytojob\.com/apply/\w+", url or ""))


# -- reading the form ------------------------------------------------------------------------

def _clean(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).replace("&amp;", "&").replace("&#039;", "'").strip()


def questions_from_html(html: str) -> list[dict]:
    """The employer's own questions: {id, label, kind, options, required}."""
    out = []
    for m in re.finditer(r"<label[^>]*for=['\"](resumator-questionnaire-q\d+)['\"][^>]*>(.*?)</label>", html, re.S):
        qid, raw = m.group(1), m.group(2)
        label = _clean(raw).rstrip("*").strip()
        required = "asterisk" in raw or raw.strip().endswith("*")
        select = re.search(rf"<select[^>]*id=['\"]{qid}['\"][^>]*>(.*?)</select>", html, re.S)
        if select:
            kind = "select"
            options = [_clean(t) for v, t in re.findall(r"<option[^>]*value=['\"]([^'\"]*)['\"][^>]*>(.*?)</option>",
                                                       select.group(1), re.S) if v != "resumator_no_selection"]
        else:
            kind = "textarea" if re.search(rf"<textarea[^>]*id=['\"]{qid}['\"]", html) else "input"
            options = []
        out.append({"id": qid, "label": label, "kind": kind, "options": options, "required": required})
    return out


def fetch_questions(url: str) -> list[dict]:
    import requests
    try:
        html = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=20).text
    except requests.RequestException:
        return []
    return questions_from_html(html)


# -- answering -------------------------------------------------------------------------------

def pick(options: list[str], answer: str | None) -> str | None:
    """The option to choose for this answer, or None when none fits.

    An exact match first, then one that starts with it ("Yes" picks "Yes, I am"),
    then, for a number, the range that holds it ("9" picks "8+ Years").
    """
    if not answer or not options:
        return None
    want = answer.strip().lower()
    for o in options:
        if o.strip().lower() == want:
            return o
    for o in options:
        if o.strip().lower().startswith(want) or want.startswith(o.strip().lower()):
            return o
    number = re.fullmatch(r"\d+(?:\.\d+)?", want)
    if number:
        years = float(want)
        best = None
        for o in options:
            low = o.lower()
            r = re.search(r"(\d+)\s*(?:-|–|to)\s*(\d+)", low)
            if r and float(r.group(1)) <= years <= float(r.group(2)) + 0.99:
                return o
            plus = re.search(r"(\d+)\s*\+|(\d+)\s*(?:or more|and above|and up)", low)
            if plus and years >= float(plus.group(1) or plus.group(2)):
                best = o
            less = re.search(r"(?:less than|under|<)\s*(\d+)", low)
            if less and years < float(less.group(1)):
                best = best or o
        return best
    return None


_FIELD_WORDS = {
    "devops": ("devops", "dev ops"), "site reliability": ("site reliability", "sre"), "sre": ("sre", "site reliability"),
    "platform": ("platform",), "cloud": ("cloud",), "infrastructure": ("infrastructure",),
    "software": ("software", "developer", "programmer"), "data": ("data",), "security": ("security",),
    "automation": ("automation", "devops"), "systems": ("systems",), "release": ("release", "build"),
}


def years_for(label: str, entries) -> float | None:
    """Years in the field the question names, from the resume's jobs whose titles name it.

    "How many years of experience in DevOps, site reliability engineering, or
    platform automation?" counts from the first job titled DevOps, SRE or
    Platform. A question naming no field counts the whole career. None when the
    resume has no dated job of that kind: the person answers it.
    """
    from datetime import date
    text = (label or "").lower()
    wanted = [w for key, words in _FIELD_WORDS.items() if key in text for w in words]
    jobs = [e for e in entries if getattr(e, "section", "") == "experience" and getattr(e, "start", None)]
    if wanted:
        jobs = [e for e in jobs if any(w in (e.title or "").lower() for w in wanted)]
    if not jobs:
        return None
    first = min(e.start for e in jobs)
    today = date.today()
    return round(today.year - int(first[:4]) + (today.month - int(first[5:7])) / 12, 1)


_ENTRIES = None


def resume_entries(cfg=None):
    """The master resume's jobs, read once."""
    global _ENTRIES
    if _ENTRIES is None:
        try:
            from backend.config.loader import load_config
            from ml.resume.pipeline import base_resume_path
            from ml.tailoring.reader import read_document
            from ml.tailoring.structure import build
            _ENTRIES = build(read_document(base_resume_path(cfg or load_config()))).entries
        except Exception:
            _ENTRIES = []
    return _ENTRIES


def answer(question: dict, applicant: Applicant, entries=None) -> str | None:
    """The answer this form's question gets, or None to leave it for the person."""
    label = question["label"]
    given = applicant.answer_for(label)
    if given is None and re.search(r"\bhow many years\b|\byears of (professional )?experience\b", label, re.I):
        years = years_for(label, entries if entries is not None else resume_entries())
        given = None if years is None else str(int(years))
    if question["kind"] == "select":
        return pick(question["options"], given)
    return given


# -- filling ---------------------------------------------------------------------------------

def _pause(page, low=250, high=700):
    page.wait_for_timeout(random.randint(low, high))


def _type(page, element, value: str) -> None:
    """Typed like a person for short fields; pasted for long ones."""
    element.click()
    element.fill("")
    if len(value) <= 80:
        element.type(value, delay=random.randint(25, 55))
    else:
        element.fill(value)


def fill(page, job_id: int, url: str, applicant: Applicant, resume_path: Path | None = None,
         *, evidence: Path | None = None) -> FillResult:
    """Fill one JazzHR form. Never presses Submit: the Human Check and Submit are the person's."""
    result = FillResult(job_id=job_id, url=url)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(2500)
    except Exception as exc:
        result.error = f"could not open the form ({str(exc)[:80]})"
        return result
    try:
        html = page.content()
    except Exception:
        html = ""
    if "form_submit_new_resume" not in html and "resumator-firstname-value" not in html:
        result.error = "this page has no JazzHR application form (the posting may be closed)"
        return result

    for field_id, value_of in FIELDS.items():
        value = (value_of(applicant) or "").strip()
        element = page.query_selector(f"#{field_id}")
        if element is None:
            continue
        if not value:
            result.left_blank.append(field_id)
            continue
        try:
            element.scroll_into_view_if_needed()
            _type(page, element, value)
            result.filled[field_id] = value
            _pause(page)
        except Exception as exc:
            log.debug("JazzHR %s: %s not filled: %s", job_id, field_id, exc)

    if resume_path is not None and Path(resume_path).exists():
        try:
            element = page.query_selector("#resumator-resume-value")
            if element is not None:
                element.set_input_files(str(resume_path))
                result.attachments.append(f"resume={Path(resume_path).name}")
                _pause(page, 600, 1200)
        except Exception as exc:
            log.warning("JazzHR %s: the resume could not be attached: %s", job_id, exc)
    if not any(a.startswith("resume=") for a in result.attachments):
        result.required_blank.append("resume")

    entries = resume_entries()
    for q in questions_from_html(html):
        element = page.query_selector(f"#{q['id']}")
        if element is None:
            continue
        reply = answer(q, applicant, entries)
        if not reply:
            result.left_blank.append(q["label"][:60])
            if q["required"]:
                result.required_blank.append(q["label"][:60])
            continue
        try:
            element.scroll_into_view_if_needed()
            if q["kind"] == "select":
                element.select_option(label=reply)
            elif q["kind"] == "textarea":
                element.fill(reply)
            else:
                _type(page, element, reply)
            result.filled[q["label"][:60]] = reply
            _pause(page)
        except Exception as exc:
            log.debug("JazzHR %s: %r not filled: %s", job_id, q["label"], exc)
            result.left_blank.append(q["label"][:60])
            if q["required"]:
                result.required_blank.append(q["label"][:60])

    result.captcha = bool(re.search(r"recaptcha|hcaptcha", html, re.I))
    try:
        shot = (evidence or _evidence_dir(job_id)) / "form.png"
        page.screenshot(path=str(shot), full_page=True)
        result.screenshot = str(shot)
    except Exception:
        pass
    # Show the person where they come in: the Human Check, then Submit.
    try:
        target = page.query_selector("#resumator-recaptcha") or page.query_selector("button[type=submit], input[type=submit]")
        if target is not None:
            target.scroll_into_view_if_needed()
            page.evaluate("el => { el.style.outline = '3px solid #e8590c'; el.style.outlineOffset = '4px'; }", target)
    except Exception:
        pass
    return result


def is_confirmed(page) -> bool:
    try:
        if page.query_selector("#form_submit_new_resume") is not None:
            return False                  # the form is still there: not sent
        text = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    return any(marker in text for marker in CONFIRM_TEXT)


def watch_submission(page, timeout_s: float, *, poll_ms: int = 2000, clock=time.monotonic):
    """("submitted" | "skipped" | "timeout", answers): the person's Submit, or their closing the tab."""
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
            return "skipped", answers
    return "timeout", answers
