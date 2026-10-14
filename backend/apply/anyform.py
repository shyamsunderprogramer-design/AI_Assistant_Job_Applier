"""Any posting, any form: follow a posting to the employer's own form and fill it for the person.

The app has fillers of its own for Greenhouse, Workday and JazzHR forms. Every other posting comes
here: one listed on a board that only lists jobs (Himalayas, Jobicy, Remotive, an email alert), or
an employer hiring on Teamtailor, Lever, Ashby, Workable, BambooHR, Breezy, Recruitee and the rest.

  1. The posting opens in the apply browser. A "checking your browser" page is waited out; when it
     wants a box ticked, the person ticks it. The app never does.
  2. Its Apply link is followed to the employer's form, through the job board's own page when there
     is one (Himalayas -> careers.bixal.com -> "Apply Here"). A Greenhouse, Workday or JazzHR form
     reached this way goes to that board's own filler.
  3. Every field the profile covers is filled, found by the words that label it: name, email,
     phone, address, links, and the screening questions the profile and the person's saved answers
     answer. The resume is attached, and the cover letter goes where the form asks for one.
  4. Submit is never pressed here, and no consent box is ticked. Submit is outlined and the app
     waits while the person checks, finishes and submits, or closes the tab to skip. A later page
     of the form (Next, Next), or a form that appears once the person has signed in, is filled
     when it shows.
  5. A thank-you page counts as applied. When a site says something the app does not recognise,
     the job page asks the person whether it went (`keep_unconfirmed`).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from backend.apply.greenhouse import CONFIRM_MARKERS, DECLINE_PHRASES, FillResult, _evidence_dir
from backend.apply.profile import US_STATES, Applicant
from backend.config.loader import PROJECT_ROOT

log = logging.getLogger(__name__)

PAGE_TIMEOUT_MS = 45_000
UNCONFIRMED = PROJECT_ROOT / "data" / "apply" / "unconfirmed.json"

# The boards with a filler of their own, by where their forms live.
BOARDS = (
    ("greenhouse", re.compile(r"^https://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/", re.I)),
    ("workday", re.compile(r"^https://[\w.-]+\.(?:myworkdayjobs|myworkdaysite)\.com/", re.I)),
    ("jazzhr", re.compile(r"^https://[\w-]+\.applytojob\.com/apply/", re.I)),
)

# Posting pages whose form is one step away at a known address.
APPLY_PATHS = (
    (re.compile(r"^(https://jobs\.(?:eu\.)?lever\.co/[^/?#]+/[0-9a-f-]{36})/?(?:[?#].*)?$", re.I), r"\1/apply"),
    (re.compile(r"^(https://jobs\.ashbyhq\.com/[^/?#]+/[0-9a-f-]{36})/?(?:[?#].*)?$", re.I), r"\1/application"),
    (re.compile(r"^(https://apply\.workable\.com/[^/?#]+/j/[0-9a-z]+)/?(?:[?#].*)?$", re.I), r"\1/apply/"),
    (re.compile(r"^(https://[\w-]+\.breezy\.hr/p/[\w-]+)/?(?:[?#].*)?$", re.I), r"\1/apply"),
)

# A page that checks the browser before showing anything (Cloudflare and the like).
CHECK_TITLES = ("just a moment", "attention required", "checking your browser", "verifying you are human",
                "verify you are human", "security check")
CHECK_FRAMES = r"challenges\.cloudflare\.com|captcha-delivery\.com|perimeterx|px-cloud\.net"

# What a page says once an application has gone through, on top of Greenhouse's own words.
DONE_WORDS = tuple(dict.fromkeys(CONFIRM_MARKERS + (
    "thank you for your application", "thanks for your application", "your application has been received",
    "successfully submitted", "application submitted", "we've received your application",
    "we have received your application", "your application has been sent", "you have successfully applied",
    "successfully applied", "application complete")))
DONE_URL = re.compile(r"/(?:thank|thanks|thank-you|thankyou|confirmation|confirmed|success|submitted|complete)\b", re.I)


@dataclass
class Reached:
    """Where following a posting ended: the page, what kind of form it holds, and why not, if not."""

    page: object
    kind: str | None          # greenhouse | workday | jazzhr | form | signin | unknown
    url: str
    error: str | None = None


def board_of(url: str) -> str | None:
    for name, pattern in BOARDS:
        if pattern.match(url or ""):
            return name
    return None


def greenhouse_form(url: str) -> str | None:
    """The form for a Greenhouse link: its embed address, from the board's name and the job number."""
    from backend.apply.greenhouse import form_url
    parsed = urlparse(url or "")
    query = parse_qs(parsed.query)
    if query.get("for") and query.get("token"):
        return form_url(query["for"][0], query["token"][0])
    m = re.match(r"^/([\w-]+)/jobs/(\d+)", parsed.path)
    return form_url(m.group(1), m.group(2)) if m else None


def apply_path(url: str) -> str | None:
    for pattern, form in APPLY_PATHS:
        if pattern.match(url or ""):
            return pattern.sub(form, url.split("#")[0].split("?")[0].rstrip("/"))
    return None


# -- reading a page ---------------------------------------------------------------------------

_SHAPE_JS = """() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                     return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const els = [...document.querySelectorAll('input, select, textarea')]
    .filter(e => !['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(e.type));
  const shown = els.filter(vis);
  return {files: els.filter(e => e.type === 'file').length,
          email: shown.filter(e => e.type === 'email' || /e-?mail/i.test((e.name || '') + ' ' + (e.id || '')
                                    + ' ' + (e.getAttribute('autocomplete') || ''))).length,
          text: shown.filter(e => e.type !== 'checkbox' && e.type !== 'radio').length,
          password: shown.filter(e => e.type === 'password').length};
}"""


def shape(page) -> dict:
    """How many fields of each kind the page shows."""
    try:
        return page.evaluate(_SHAPE_JS) or {}
    except Exception:
        return {}


def is_form(info: dict) -> bool:
    """An application form, not a search box or a newsletter sign-up."""
    if info.get("password"):
        return False
    return (info.get("files", 0) > 0 and info.get("text", 0) >= 2) or \
           (info.get("email", 0) > 0 and info.get("text", 0) >= 3)


def being_checked(page) -> bool:
    """Is the site checking the browser before it shows the page?"""
    try:
        title = (page.title() or "").lower()
    except Exception:
        return False
    if any(t in title for t in CHECK_TITLES):
        return True
    try:
        framed = page.evaluate("""(re) => [...document.querySelectorAll('iframe')]
            .some(f => new RegExp(re, 'i').test(f.src || '') && f.offsetHeight > 30)""", CHECK_FRAMES)
    except Exception:
        return False
    # A check box inside an application form is that form's own CAPTCHA, which is the person's at Submit.
    return bool(framed) and not is_form(shape(page))


def wait_out_check(page, say=print, timeout_s: float = 180, poll_ms: int = 2000,
                   clock=time.monotonic) -> bool:
    """Wait while the site checks the browser. True once the page is through."""
    if not being_checked(page):
        return True
    say("   The site is checking the browser first. If it shows a box to tick, tick it in the window.")
    deadline = clock() + timeout_s
    while clock() < deadline:
        try:
            if page.is_closed():
                return False
            page.wait_for_timeout(poll_ms)
        except Exception:
            return False
        if not being_checked(page):
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
            return True
    return False


_APPLY_JS = """() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                     return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const els = [...document.querySelectorAll('a[href], button, [role=button], input[type=submit], input[type=button]')].filter(vis);
  const found = [];
  els.forEach((e, i) => {
    const text = ((e.innerText || e.value || '') + ' ' + (e.getAttribute('aria-label') || '')).replace(/\\s+/g, ' ').trim();
    if (!/\\bapply\\b/i.test(text) || text.length > 60) return;
    if (/\\bapplied\\b|apply (filters?|changes|selection)|how to apply|easy apply|apply (with|using|via) (linkedin|indeed|seek|google)/i.test(text)) return;
    let score = 0;
    if (/apply (now|here|online|today|for (this|the) (job|position|role)|to (this )?(job|position|role)|on (the )?(company|employer)|at|for)/i.test(text)) score += 3;
    if (/^apply$/i.test(text)) score += 2;
    const href = e.tagName === 'A' ? e.href : '';
    if (href && /^https?:/.test(href) && !href.startsWith(location.href.split('#')[0] + '#')) score += 1;
    e.setAttribute('data-ja-apply', String(i));
    found.push({i, text: text.slice(0, 60), href, score});
  });
  found.sort((a, b) => b.score - a.score);
  return found.slice(0, 6);
}"""


def apply_controls(page) -> list[dict]:
    """The page's Apply links and buttons, the likeliest first."""
    try:
        return page.evaluate(_APPLY_JS) or []
    except Exception:
        return []


def _board_frame(page) -> str | None:
    """A Greenhouse, Workday or JazzHR form framed inside an employer's own careers page."""
    try:
        sources = page.evaluate("() => [...document.querySelectorAll('iframe')].map(f => f.src || '')")
    except Exception:
        return None
    return next((s for s in sources if board_of(s)), None)


def _follow(page, choice: dict):
    """Go where an Apply control leads: a link is opened in place, a button is pressed."""
    context = page.context
    before = list(context.pages)
    href = choice.get("href") or ""
    if href.startswith(("http://", "https://")) and "#" not in href.replace(page.url.split("#")[0], "", 1)[:1]:
        page.goto(href, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    else:
        page.locator(f'[data-ja-apply="{choice["i"]}"]').first.click(timeout=8000)
    page.wait_for_timeout(2500)
    opened = [p for p in context.pages if p not in before]
    if opened:
        target = opened[-1]
        try:
            target.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        except Exception:
            pass
        return target
    return page


def reach(page, url: str, *, say=print, check_s: float = 180, hops: int = 5) -> Reached:
    """Open a posting and get to its application form."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    except Exception as exc:
        return Reached(page, None, url, f"could not open the posting ({str(exc).splitlines()[0][:80]})")
    tried: set[str] = set()
    for _ in range(hops):
        try:
            page.wait_for_timeout(1500)
        except Exception:
            return Reached(page, None, url, "the window was closed")
        if not wait_out_check(page, say, check_s):
            return Reached(page, None, page.url, "the site's browser check did not clear")
        here = page.url
        kind = board_of(here)
        if kind:
            return Reached(page, kind, here)
        framed = _board_frame(page)
        if framed:
            return Reached(page, board_of(framed), framed)
        info = shape(page)
        if is_form(info):
            return Reached(page, "form", here)
        step = apply_path(here)
        if step and step not in tried:
            tried.add(step)
            page.goto(step, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            continue
        choices = [c for c in apply_controls(page) if (c["href"] or f'#{here}#{c["text"]}') not in tried]
        if not choices:
            return Reached(page, "signin" if info.get("password") else "unknown", here)
        choice = choices[0]
        tried.add(choice["href"] or f'#{here}#{choice["text"]}')
        say(f"   Following “{choice['text']}”…")
        try:
            page = _follow(page, choice)
        except Exception as exc:
            log.debug("Apply control %r did not work: %s", choice, exc)
    info = shape(page)
    kind = board_of(page.url) or ("form" if is_form(info) else "signin" if info.get("password") else "unknown")
    return Reached(page, kind, page.url)


# -- the fields --------------------------------------------------------------------------------

# Every field a person would fill, with the words that say what it is, each tagged (data-ja) so
# it can be found again. Radios and checkboxes come as one entry per group, with the question
# their group asks and each option's own words.
_FIELDS_JS = r"""(mark) => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                     return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  // A node's words as the person sees them: not a control's own text (a select's every option),
  // and not text the page keeps hidden ("Please wait for the video answers..." on Teamtailor).
  const visibleText = (node, skipLabels) => {
    if (!node) return '';
    const parts = [];
    const walk = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    for (let t = walk.nextNode(); t; t = walk.nextNode()) {
      const host = t.parentElement;
      if (!host || host.closest('select, option, textarea, button, script, style')) continue;
      const lab = skipLabels ? host.closest('label') : null;
      if (lab && node.contains(lab)) continue;
      if (!host.getClientRects().length) continue;
      parts.push(t.textContent);
    }
    return parts.join(' ');
  };
  const strip = node => visibleText(node, false);
  const clean = s => (s || '').replace(/[*✱]/g, ' ').replace(/\(required\)|\brequired\b/gi, ' ').replace(/\s+/g, ' ').trim();
  const says = node => !!node && /[*✱]|\brequired\b/i.test(node.textContent || '');
  const byId = id => id ? document.getElementById(id) : null;
  const forLabel = el => el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
  const describe = el => {
    for (const node of [forLabel(el), el.closest('label')]) {
      if (node && clean(strip(node))) return {text: clean(strip(node)), req: says(node)};
    }
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    if (ids.length) {
      const t = clean(ids.map(i => strip(byId(i))).join(' '));
      if (t) return {text: t, req: ids.some(i => says(byId(i)))};
    }
    if (el.getAttribute('aria-label')) return {text: clean(el.getAttribute('aria-label')), req: false};
    const fs = el.closest('fieldset'), lg = fs && fs.querySelector('legend');
    if (lg && clean(strip(lg))) return {text: clean(strip(lg)), req: says(lg)};
    let c = el.parentElement;
    for (let i = 0; i < 4 && c && c !== document.body; i++, c = c.parentElement) {
      if (c.querySelectorAll('input:not([type=hidden]), select, textarea').length > 1) break;
      const t = clean(strip(c));
      if (t) return {text: t.slice(0, 300), req: says(c)};
    }
    return {text: '', req: false};
  };
  const question = el => {
    const fs = el.closest('fieldset'), lg = fs && fs.querySelector('legend');
    if (lg && clean(strip(lg))) return {text: clean(strip(lg)), req: says(lg)};
    const group = el.closest('[role=radiogroup], [role=group]');
    if (group) {
      const ids = (group.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
      const t = clean(ids.map(i => strip(byId(i))).join(' ') || group.getAttribute('aria-label') || '');
      if (t) return {text: t, req: ids.some(i => says(byId(i)))};
    }
    const sel = el.name ? 'input[name="' + CSS.escape(el.name) + '"]' : null;
    const all = sel ? document.querySelectorAll(sel).length : 1;
    let c = el.parentElement;
    for (let i = 0; i < 6 && c && c !== document.body; i++, c = c.parentElement) {
      if (sel && c.querySelectorAll(sel).length < all) continue;
      const t = clean(visibleText(c, true));
      if (t) return {text: t.slice(0, 300), req: says(c)};
    }
    return {text: '', req: false};
  };
  const optionText = el => {
    const l = forLabel(el) || el.closest('label');
    return clean(l ? strip(l) : (el.getAttribute('aria-label') || el.value || ''));
  };
  const out = [], groups = {};
  let n = 0;
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'password', 'search'].includes(type)) continue;
    if (el.disabled || el.readOnly) continue;
    const key = mark + (n++);
    if (type === 'radio' || type === 'checkbox') {
      const lab = forLabel(el) || el.closest('label');
      if (!vis(el) && !(lab && vis(lab))) continue;
      el.setAttribute('data-ja', key);
      const g = el.name || key;
      if (!groups[g]) {
        const q = question(el);
        groups[g] = {key, kind: type, name: el.name || '', id: el.id || '', auto: '', label: q.text,
                     placeholder: '', value: '', required: q.req, options: [], keys: [], checked: [], role: ''};
      }
      groups[g].options.push(optionText(el));
      groups[g].keys.push(key);
      groups[g].checked.push(el.checked);
      groups[g].required = groups[g].required || el.required || el.getAttribute('aria-required') === 'true'
                           || (type === 'checkbox' && says(lab));
      continue;
    }
    if (type !== 'file' && !vis(el)) continue;
    el.setAttribute('data-ja', key);
    const d = describe(el);
    const select = el.tagName === 'SELECT';
    out.push({key, kind: select ? 'select' : (el.tagName === 'TEXTAREA' ? 'textarea' : type),
              name: el.name || '', id: el.id || '', auto: (el.getAttribute('autocomplete') || '').toLowerCase(),
              label: d.text, placeholder: el.placeholder || '',
              value: select ? (el.value ? (el.options[el.selectedIndex] || {}).text || '' : '') : (el.value || ''),
              required: d.req || el.required || el.getAttribute('aria-required') === 'true',
              options: select ? [...el.options].filter(o => o.value).map(o => o.text.trim()) : [],
              keys: [], checked: [], role: el.getAttribute('role') || ''});
  }
  return out.concat(Object.values(groups));
}"""

_scan = 0


def fields(page) -> list[dict]:
    """The form's fields as the person sees them."""
    global _scan
    _scan += 1
    try:
        found = page.evaluate(_FIELDS_JS, f"s{_scan}-") or []
    except Exception as exc:
        log.debug("Fields not read: %s", exc)
        return []
    for field in found:
        # A lone checkbox ("By submitting this application, I authorize...") says what it is itself.
        if field["kind"] == "checkbox" and len(field["options"]) == 1 and field["options"][0]:
            field["label"] = field["options"][0].lstrip(". ")
    return found


def _humanise(raw: str) -> str:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw or "")
    return re.sub(r"[\[\]_\-.:]+", " ", spaced).strip()


def hint(field: dict) -> str:
    """Everything that says what a field is, in plain lower-case words."""
    parts = [field.get("label") or "", field.get("placeholder") or "",
             _humanise(field.get("name") or ""), _humanise(field.get("id") or "")]
    return " ".join(p for p in parts if p).lower()


def signature(field: dict) -> str:
    """The same field, however often the page is read: its question, name and kind."""
    return "|".join([field.get("kind") or "", (field.get("label") or "")[:120], field.get("name") or "",
                     field.get("id") or ""])


def _loc(applicant: Applicant, part: str) -> str:
    return str(applicant.location.get(part) or "").strip()


def _city_state(applicant: Applicant) -> str:
    return ", ".join(p for p in (_loc(applicant, "city"), _loc(applicant, "state")) if p)


# The browser's own names for what a field holds, when the form gives one.
AUTOCOMPLETE = {
    "given-name": lambda a: a.first_name,
    "family-name": lambda a: a.last_name,
    "name": lambda a: a.full_name,
    "email": lambda a: a.email,
    "tel": lambda a: a.phone,
    "tel-national": lambda a: a.phone,
    "street-address": lambda a: _loc(a, "street"),
    "address-line1": lambda a: _loc(a, "street"),
    "address-level2": lambda a: _loc(a, "city"),
    "address-level1": lambda a: _loc(a, "state"),
    "postal-code": lambda a: _loc(a, "postal_code"),
    "country": lambda a: _loc(a, "country"),
    "country-name": lambda a: _loc(a, "country"),
    "organization": lambda a: str(a.employment.get("current_employer") or ""),
    "organization-title": lambda a: str(a.employment.get("current_title") or ""),
}

YEARS = re.compile(r"\bhow many years\b|\byears of (?:professional )?experience\b", re.I)


def value_for(field: dict, applicant: Applicant, *, letter_text: str | None = None,
              entries=None) -> str | None:
    """What one field gets, or None to leave it for the person."""
    from backend.apply import jazzhr, workday
    label = (field.get("label") or "").strip()
    said = hint(field)
    words = set(re.findall(r"[a-z]+", said))
    if not said or "password" in words:
        return None
    for token in reversed((field.get("auto") or "").split()):
        if token in AUTOCOMPLETE:
            return str(AUTOCOMPLETE[token](applicant) or "").strip() or None
    if "cover" in words and "letter" in words:
        return (letter_text or "").strip() or None if field.get("kind") == "textarea" else None
    if "email" in words:
        return applicant.email or None
    if (re.search(r"\b(full|legal|your|candidate|applicant)\s+name\b", said) or label.lower().rstrip(": ") == "name"
            or (field.get("name") or "").lower() == "name") \
            and not words & {"company", "employer", "school", "reference", "manager", "referrer", "preferred"}:
        return applicant.full_name or None
    if words & {"phone", "mobile", "telephone", "cell"} and not words & {"type", "code", "extension"}:
        return applicant.phone or None
    if "preferred" in words and "name" in words and "pronoun" not in said:
        return applicant.first_name or None         # the name the person goes by, unless they say otherwise
    if len(label or said) <= 60 and "location" in words and not words & {"willing", "relocate", "open", "prefer"}:
        return _city_state(applicant) or None
    answer = workday.value_for(label or said, applicant)
    if answer is None and YEARS.search(label or ""):
        years = jazzhr.years_for(label, entries if entries is not None else jazzhr.resume_entries())
        answer = None if years is None else str(int(years))
    return answer


def choose(options: list[str], answer: str | None) -> int | None:
    """Which option says the answer: exact, then begins with it, a number's range, or "decline"."""
    from backend.apply.jazzhr import pick
    if not answer or not options:
        return None
    want = answer.strip()
    if want.lower() == "decline":
        return next((i for i, o in enumerate(options) if any(p in o.lower() for p in DECLINE_PHRASES)), None)
    got = pick(options, want)
    if got is None and want.upper() in US_STATES:
        got = pick(options, US_STATES[want.upper()].title())
    return options.index(got) if got is not None else None


# -- filling -----------------------------------------------------------------------------------

def _body(page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:
        return ""


def _attach(page, field: dict, path: Path, what: str, result: FillResult) -> bool:
    """Put a file in an upload field, and check the page took it."""
    locator = page.locator(f'[data-ja="{field["key"]}"]')
    before = _body(page)
    try:
        locator.set_input_files(str(path))
        page.wait_for_timeout(1500)
    except Exception as exc:
        log.debug("Upload %s not taken: %s", what, exc)
        return False
    try:
        held = bool(locator.evaluate("el => !!(el.files && el.files.length)", timeout=1000))
    except Exception:
        held = False                   # a drop zone swaps its input once it has the file
    shown = path.name in _body(page) and path.name not in before
    if held or shown:
        result.attachments.append(f"{what}={path.name}")
        return True
    return False


def _upload_kind(field: dict, result: FillResult) -> str | None:
    said = hint(field)
    if re.search(r"\bcover\b|\bletter\b", said):
        return "cover_letter"
    if re.search(r"resume|résumé|\bcv\b|curriculum", said):
        return "resume"
    if not any(a.startswith("resume=") for a in result.attachments) and \
            not re.search(r"additional|other|supporting|portfolio|transcript|photo|image", said):
        return "resume"
    return None


def fill(page, job_id: int, url: str, applicant: Applicant, resume_path: Path | None = None,
         letter_path: Path | None = None, *, letter_text: str | None = None, evidence: Path | None = None,
         seen: set[str] | None = None, entries=None, screenshot: bool = True) -> FillResult:
    """Fill what the profile covers on the form now showing. Never presses Submit.

    `seen` holds the fields already dealt with, so a later page of the form is filled without
    touching anything the person has since changed.
    """
    result = FillResult(job_id=job_id, url=url)
    seen = seen if seen is not None else set()
    for field in fields(page):
        sig = signature(field)
        if sig in seen:
            continue
        seen.add(sig)
        kind = field["kind"]
        name = (field.get("label") or field.get("name") or "a field")[:60]
        if kind == "file":
            what = _upload_kind(field, result)
            path = {"resume": resume_path, "cover_letter": letter_path}.get(what or "")
            if path is not None and Path(path).exists() and not any(a.startswith(f"{what}=") for a in result.attachments):
                _attach(page, field, Path(path), what, result)
            continue
        if kind == "checkbox":
            # Consent ("I agree...", "By submitting I authorize...") and pick-several questions are the person's.
            if field["required"] and not any(field["checked"]):
                result.declarations.append(name)
            continue
        if kind == "radio" and any(field["checked"]):
            continue
        if kind not in ("radio",) and field.get("value"):
            continue                    # already filled, by the site or the person
        if field.get("role") == "combobox":
            answer = None               # a typed-and-searched list: the person picks
        else:
            answer = value_for(field, applicant, letter_text=letter_text, entries=entries)
        if answer is None:
            result.left_blank.append(name)
            if field["required"]:
                result.required_blank.append(name)
            continue
        try:
            if kind in ("select", "radio"):
                index = choose(field["options"], answer)
                if index is None:
                    raise ValueError(f"no option says {answer!r}")
                if kind == "select":
                    page.locator(f'[data-ja="{field["key"]}"]').select_option(label=field["options"][index])
                else:
                    target = page.locator(f'[data-ja="{field["keys"][index]}"]')
                    try:
                        target.check(timeout=3000)
                    except Exception:
                        target.check(timeout=3000, force=True)
                answer = field["options"][index]
            else:
                if kind == "number":
                    from backend.apply.salary import single_figure
                    answer = (single_figure(answer) if " – " in answer else None) or re.sub(r"[^\d.]", "", answer) or answer
                page.locator(f'[data-ja="{field["key"]}"]').fill(answer, timeout=5000)
            result.filled[(field.get("label") or field.get("name") or name)[:300]] = answer
        except Exception as exc:
            log.debug("Field %r not filled: %s", name, exc)
            result.left_blank.append(name)
            if field["required"]:
                result.required_blank.append(name)

    try:
        html = page.content().lower()
    except Exception:
        html = ""
    from backend.apply.greenhouse import captcha_is_invisible
    present = "recaptcha" in html or "hcaptcha" in html or "turnstile" in html
    result.captcha = present and not captcha_is_invisible(html)
    if screenshot:
        try:
            shot = (evidence or _evidence_dir(job_id)) / "form.png"
            page.screenshot(path=str(shot), full_page=True)
            result.screenshot = str(shot)
        except Exception as exc:
            log.debug("No screenshot: %s", exc)
    return result


def show_submit(page, scroll: bool = True) -> bool:
    """Outline the form's Submit, and bring it into view when nothing above it needs the person."""
    try:
        return bool(page.evaluate("""(scroll) => {
          const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
          const filled = document.querySelector('[data-ja]');
          const home = filled ? filled.closest('form') : null;
          const pool = [...(home || document).querySelectorAll('button, input[type=submit], [role=button]')].filter(vis);
          const say = e => ((e.innerText || e.value || '') + ' ' + (e.getAttribute('aria-label') || '')).trim();
          const b = pool.find(e => e.type === 'submit' && /submit|send|apply|finish|complete/i.test(say(e)))
                 || pool.find(e => /^(submit|send|apply|finish)\\b/i.test(say(e)))
                 || pool.find(e => e.type === 'submit');
          if (!b) return false;
          b.style.outline = '4px solid #f59e0b';
          b.style.outlineOffset = '4px';
          if (scroll) b.scrollIntoView({block: 'center', behavior: 'smooth'});
          return true;
        }""", scroll))
    except Exception:
        return False


# -- waiting for the person -----------------------------------------------------------------------

def _said_done(page) -> set[str]:
    text = _body(page).lower()
    return {w for w in DONE_WORDS if w in text}


def snapshot(page) -> dict[str, str]:
    """Every labelled answer on the form, as the person has left it."""
    out = {}
    for field in fields(page):
        label = (field.get("label") or "").strip()
        if not label or field["kind"] in ("file", "password"):
            continue
        if field["kind"] in ("radio", "checkbox"):
            picked = [o for o, on in zip(field["options"], field["checked"]) if on]
            if picked:
                out[label] = ", ".join(picked)
        elif field.get("value"):
            out[label] = field["value"]
    return out


def watch(page, applicant: Applicant, result: FillResult, timeout_s: float, *, resume_path=None,
          letter_path=None, letter_text=None, seen=None, evidence: Path | None = None, say=print,
          poll_ms: int = 2000, clock=time.monotonic):
    """("submitted" | "skipped" | "timeout", answers, page): the person's Submit, or the tab closed.

    While waiting, a new page of the form, or a form that appears once the person has signed in,
    is filled the first time it shows. On a Submit the confirmation is kept beside the resume.
    """
    seen = seen if seen is not None else set()
    answers: dict[str, str] = {}
    try:
        context = page.context
        known = set(context.pages)
    except Exception:
        context, known = None, set()
    baseline, start = _said_done(page), page.url
    deadline = clock() + timeout_s
    while clock() < deadline:
        try:
            if page.is_closed():
                return "skipped", answers, page
        except Exception:
            return "skipped", answers, page
        if context is not None:
            opened = [p for p in context.pages if p not in known and not p.is_closed()]
            if opened:                       # the form went on in a tab of its own
                known.update(opened)
                page = opened[-1]
                baseline, start = _said_done(page), page.url
        moved = page.url != start and DONE_URL.search(urlparse(page.url).path or "")
        if (_said_done(page) - baseline) or moved:
            if evidence is not None:
                try:
                    page.screenshot(path=str(evidence / "confirmation.png"), full_page=True)
                    (evidence / "confirmation.txt").write_text(_body(page), encoding="utf-8")
                except Exception:
                    pass
            try:
                page.wait_for_timeout(6000)  # the thank-you page stays up long enough to be read
            except Exception:
                pass
            return "submitted", answers, page
        if is_form(shape(page)):
            more = fill(page, result.job_id, page.url, applicant, resume_path, letter_path,
                        letter_text=letter_text, evidence=evidence, seen=seen, screenshot=False)
            if more.filled or more.attachments:
                result.filled.update(more.filled)
                result.attachments.extend(a for a in more.attachments if a not in result.attachments)
                say(f"   Filled {len(more.filled)} more field(s) on this page"
                    + (f" and attached {', '.join(a.split('=')[0] for a in more.attachments)}" if more.attachments else "")
                    + ".")
                show_submit(page, scroll=False)
            answers = snapshot(page) or answers
        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            return "skipped", answers, page
    return "timeout", answers, page


# -- "did it go?" ----------------------------------------------------------------------------------

def _read_unconfirmed() -> dict:
    try:
        return json.loads(UNCONFIRMED.read_text())
    except (OSError, ValueError):
        return {}


def keep_unconfirmed(job_id: int, entry: dict) -> None:
    """What went into a form the app could not see sent, for the person to say whether it went."""
    data = _read_unconfirmed()
    data[str(job_id)] = {**entry, "kept_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    UNCONFIRMED.parent.mkdir(parents=True, exist_ok=True)
    tmp = UNCONFIRMED.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(UNCONFIRMED)


def unconfirmed(job_id: int) -> dict | None:
    return _read_unconfirmed().get(str(job_id))


def forget_unconfirmed(job_id: int) -> None:
    data = _read_unconfirmed()
    if data.pop(str(job_id), None) is not None:
        tmp = UNCONFIRMED.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(UNCONFIRMED)
