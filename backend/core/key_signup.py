"""Guided sign-up for the job-feed API keys: the person signs up, this does the rest.

For each provider whose key is missing (USAJOBS, Jooble, Adzuna, Findwork,
Careerjet), in a visible browser:

  1. Opens the provider's sign-up page and fills the fields that are not
     secret — name, email, "organisation", "what will you use it for" — from
     the applicant profile. It never fills a password, never ticks a terms
     box, never touches a CAPTCHA and never presses Submit: the account and
     the agreement are the person's, and so is the password, which is typed
     only into the provider's own page.
  2. Watches for the key: in the inbox (read-only), for providers that email
     it, and on the provider's own pages open in that browser, for providers
     that show it on a dashboard after sign-in.
  3. Tries each candidate with one real API call, and saves only a key that
     works, into .env through the same `write_env` the Settings page uses.

Closing a provider's tab skips it. A provider that already has a key is
skipped unless --force.
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from itertools import product

from backend.config.loader import PROJECT_ROOT

log = logging.getLogger(__name__)

ENV_PATH = PROJECT_ROOT / ".env"
MAX_TRIES_PER_PROVIDER = 8            # candidate keys tested, each one API call

PURPOSE = ("Personal job search. A small script on my own computer searches for "
           "jobs in my field once a day and shows me the results; nothing is "
           "republished or resold. About 30-40 requests a day.")


def say(line: str) -> None:
    print(line, flush=True)


# -- key validation: one real call each ------------------------------------

def _ok(resp) -> bool:
    return resp is not None and resp.status_code == 200


def check_usajobs(values: dict, http=None) -> bool:
    import requests
    http = http or requests
    email_addr = values.get("USAJOBS_EMAIL") or os.getenv("MAIL_ADDRESS", "")
    resp = http.get("https://data.usajobs.gov/api/search",
                    params={"Keyword": "devops", "ResultsPerPage": 1},
                    headers={"Host": "data.usajobs.gov", "User-Agent": email_addr,
                             "Authorization-Key": values["USAJOBS_API_KEY"]}, timeout=20)
    return _ok(resp)


def check_jooble(values: dict, http=None) -> bool:
    import requests
    http = http or requests
    resp = http.post(f"https://jooble.org/api/{values['JOOBLE_API_KEY']}",
                     json={"keywords": "devops", "location": "United States"}, timeout=20)
    return _ok(resp) and "jobs" in (resp.json() or {})


def check_adzuna(values: dict, http=None) -> bool:
    import requests
    http = http or requests
    resp = http.get("https://api.adzuna.com/v1/api/jobs/us/search/1",
                    params={"app_id": values["ADZUNA_APP_ID"], "app_key": values["ADZUNA_APP_KEY"],
                            "results_per_page": 1, "what": "devops"}, timeout=20)
    return _ok(resp)


def check_findwork(values: dict, http=None) -> bool:
    import requests
    http = http or requests
    resp = http.get("https://findwork.dev/api/jobs/", params={"search": "devops"},
                    headers={"Authorization": f"Token {values['FINDWORK_API_KEY']}"}, timeout=20)
    return _ok(resp)


def check_careerjet(values: dict, http=None) -> bool:
    import requests
    http = http or requests
    resp = http.get("http://public.api.careerjet.net/search",
                    params={"locale_code": "en_US", "keywords": "devops", "pagesize": 1,
                            "affid": values["CAREERJET_AFFID"], "user_ip": "127.0.0.1",
                            "user_agent": "Mozilla/5.0"},
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.careerjet.com/"},
                    timeout=20)
    return _ok(resp) and bool((resp.json() or {}).get("jobs"))


# -- providers ---------------------------------------------------------------

@dataclass
class Provider:
    name: str
    label: str
    signup_url: str
    env: tuple[str, ...]                          # keys this provider fills
    patterns: dict[str, str]                      # env key -> regex for candidates
    check: object                                 # (values) -> bool
    mail_from: str = ""                           # sender to search, when the key is emailed
    dashboard_url: str = ""                       # where the key is shown after sign-in
    steps: list[str] = field(default_factory=list)

    def missing(self) -> bool:
        return any(not (os.getenv(k) or "").strip() for k in self.env)


HEX = lambda n: rf"\b[0-9a-f]{{{n}}}\b"
UUID = r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"

PROVIDERS = [
    Provider("usajobs", "USAJOBS", "https://developer.usajobs.gov/APIRequest/Index",
             ("USAJOBS_API_KEY",), {"USAJOBS_API_KEY": r"\b[A-Za-z0-9+/]{40,48}={0,2}"},
             check_usajobs, mail_from="usajobs",
             steps=["Check the name and email filled in", "Complete any CAPTCHA and press Submit",
                    "The key arrives by email — this watches your inbox for it"]),
    Provider("jooble", "Jooble", "https://jooble.org/api/about",
             ("JOOBLE_API_KEY",), {"JOOBLE_API_KEY": UUID}, check_jooble, mail_from="jooble",
             steps=["Fill the API request form (name and email are filled in)",
                    "Press Submit — the key arrives by email, or on the page"]),
    Provider("adzuna", "Adzuna", "https://developer.adzuna.com/signup",
             ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"),
             {"ADZUNA_APP_ID": HEX(8), "ADZUNA_APP_KEY": HEX(32)}, check_adzuna,
             dashboard_url="https://developer.adzuna.com/admin/access_details",
             steps=["Choose a password (your password manager can make one) and sign up",
                    "Confirm the email Adzuna sends, then sign in",
                    "Open 'API Access Details' — the key is read from that page"]),
    Provider("findwork", "Findwork", "https://findwork.dev/login/",
             ("FINDWORK_API_KEY",), {"FINDWORK_API_KEY": HEX(40)}, check_findwork,
             dashboard_url="https://findwork.dev/developers/",
             steps=["Sign up / sign in", "Open the Developers page — the token is read from it"]),
    Provider("careerjet", "Careerjet", "https://www.careerjet.com/partners/register/as-publisher",
             ("CAREERJET_AFFID",), {"CAREERJET_AFFID": HEX(32)}, check_careerjet,
             steps=["Register as a publisher (password: yours to choose)",
                    "Open the page that shows your affiliate ID — it is read from there"]),
]


# -- filling the non-secret fields -------------------------------------------

def field_value(descriptor: str, person: dict) -> str | None:
    """What goes in a form field, judged from its name/label, or None to leave it.

    Only the non-secret: never a password, and never anything not listed here.
    """
    d = descriptor.lower()
    if any(w in d for w in ("password", "passwd", "captcha", "card", "ssn", "phone")):
        return None
    if "email" in d or "e-mail" in d:
        return person.get("email")
    if "first" in d and "name" in d:
        return person.get("first_name")
    if ("last" in d or "surname" in d or "family" in d) and "name" in d:
        return person.get("last_name")
    if any(w in d for w in ("company", "organization", "organisation", "business", "employer")):
        return "Independent (personal job search)"
    if any(w in d for w in ("website", "url", "homepage", "site")):
        return person.get("linkedin") or None
    if any(w in d for w in ("describe", "purpose", "reason", "project", "use case",
                            "how will you use", "intended", "description", "message")):
        return PURPOSE
    if "name" in d and not any(w in d for w in ("user", "login", "app", "application")):
        return person.get("full_name")
    return None


_FIELDS_JS = """() => [...document.querySelectorAll('input, textarea')].map((el, i) => {
  el.setAttribute('data-signup-idx', i);
  const lab = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
  const around = el.closest('label');
  return {idx: i, type: (el.type || el.tagName).toLowerCase(), value: el.value || '',
          visible: !!(el.offsetWidth || el.offsetHeight),
          text: [el.name, el.id, el.placeholder, el.getAttribute('aria-label'),
                 lab && lab.innerText, around && around.innerText].filter(Boolean).join(' ')};
})"""


def prefill(page, person: dict) -> list[str]:
    """Fill the empty non-secret fields. Returns what was filled, for the log."""
    filled = []
    try:
        fields = page.evaluate(_FIELDS_JS)
    except Exception:
        return filled
    for f in fields:
        if (not f["visible"] or f["value"] or
                f["type"] in ("password", "checkbox", "radio", "hidden", "submit", "button", "file")):
            continue
        value = field_value(f["text"], person)
        if value:
            try:
                page.fill(f"[data-signup-idx='{f['idx']}']", value)
                filled.append(f["text"][:40].strip() or f["type"])
            except Exception:
                pass
    return filled


# -- finding the key ------------------------------------------------------------

def candidates(text: str, provider: Provider) -> list[dict]:
    """Every combination of values that could be this provider's key(s)."""
    found = {}
    for env_key, pattern in provider.patterns.items():
        values = list(dict.fromkeys(re.findall(pattern, text or "", re.I)))
        if not values:
            return []
        found[env_key] = values[:6]
    keys = list(found)
    return [dict(zip(keys, combo)) for combo in product(*(found[k] for k in keys))]


def page_texts(context) -> str:
    text = []
    for page in list(getattr(context, "pages", [])):
        try:
            text.append(page.inner_text("body"))
        except Exception:
            pass
    return "\n".join(text)


def inbox_text(sender: str, since: datetime) -> str:
    """Bodies of recent mail from `sender`, read-only."""
    address, password = os.getenv("MAIL_ADDRESS"), os.getenv("MAIL_APP_PASSWORD")
    if not (address and password and sender):
        return ""
    bodies = []
    connection = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    try:
        connection.login(address, password)
        connection.select("INBOX", readonly=True)
        day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
        typ, data = connection.search(None, f'(FROM "{sender}" SINCE {day})')
        for uid in (data[0].split() if typ == "OK" else [])[-10:]:
            typ, payload = connection.fetch(uid, "(RFC822)")
            if typ != "OK" or not payload or not payload[0]:
                continue
            message = email.message_from_bytes(payload[0][1])
            try:
                if parsedate_to_datetime(message["Date"]) < since - timedelta(minutes=10):
                    continue                      # an old email: not this sign-up's
            except (TypeError, ValueError):
                pass
            for part in (message.walk() if message.is_multipart() else [message]):
                if part.get_content_type() in ("text/plain", "text/html"):
                    bodies.append((part.get_payload(decode=True) or b"").decode(errors="replace"))
    finally:
        try:
            connection.logout()
        except Exception:
            pass
    return "\n".join(bodies)


def find_working_key(text: str, provider: Provider, tried: set, check=None) -> dict | None:
    check = check or provider.check
    for combo in candidates(text, provider):
        signature = tuple(sorted(combo.items()))
        if signature in tried or len(tried) >= MAX_TRIES_PER_PROVIDER:
            continue
        tried.add(signature)
        try:
            if check(combo):
                return combo
        except Exception as exc:
            log.debug("%s candidate failed: %s", provider.name, exc)
    return None


def save(values: dict) -> list[str]:
    from backend.config.envfile import write_env
    changed = write_env(ENV_PATH, values)
    os.environ.update(values)
    return changed


# -- the guided run ------------------------------------------------------------

def run(*, only: list[str] | None = None, force: bool = False, wait_minutes: float = 20,
        poll_s: float = 5, mail_every_s: float = 30, context=None) -> dict[str, str]:
    from backend.apply.profile import ProfileIncomplete, load

    try:
        a = load(PROJECT_ROOT / "backend" / "config" / "applicant.yaml")
        person = {"email": a.email, "first_name": a.first_name, "last_name": a.last_name,
                  "full_name": a.full_name, "linkedin": a.links.get("linkedin", "")}
    except ProfileIncomplete:
        person = {}

    todo = [p for p in PROVIDERS if (not only or p.name in only) and (force or p.missing())]
    if not todo:
        say("Every provider already has a key. Use --force to redo one.")
        return {}

    manager = browser = None
    if context is None:
        from playwright.sync_api import sync_playwright
        manager = sync_playwright().start()
        browser = manager.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1280, "height": 1000})

    results: dict[str, str] = {}
    try:
        for provider in todo:
            results[provider.name] = guide(provider, context, person, wait_minutes=wait_minutes,
                                           poll_s=poll_s, mail_every_s=mail_every_s)
    finally:
        if manager is not None:
            for closer in (context, browser):
                try:
                    closer.close()
                except Exception:
                    pass
            manager.stop()

    say("\nSummary: " + " · ".join(f"{p.label}: {results[p.name]}" for p in todo))
    return results


def guide(provider: Provider, context, person: dict, *, wait_minutes: float,
          poll_s: float, mail_every_s: float, now=time.monotonic, sleep=time.sleep) -> str:
    started = datetime.now(timezone.utc)
    say(f"\n== {provider.label} ==")
    page = context.new_page()
    try:
        page.goto(provider.signup_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2500)
    except Exception as exc:
        say(f"   Could not open {provider.signup_url} ({str(exc)[:60]}) — skipped.")
        return "page did not open"
    filled = prefill(page, person)
    if filled:
        say(f"   Filled for you: {', '.join(filled)}")
    for n, step in enumerate(provider.steps, 1):
        say(f"   {n}. {step}")
    if provider.dashboard_url:
        say(f"   Key page: {provider.dashboard_url}")
    say("   Your password goes only into their page. Close the tab to skip this one.")

    tried: set = set()
    deadline = now() + wait_minutes * 60
    last_mail = 0.0
    # Some forms appear only after a "verify you are human" check or a click
    # (Jooble sits behind Cloudflare). Keep offering to fill until something
    # was filled once, then stop — a field the person cleared stays cleared.
    filled_once = bool(filled)
    while now() < deadline:
        if not filled_once and not page.is_closed():
            late = prefill(page, person)
            if late:
                filled_once = True
                say(f"   Filled for you: {', '.join(late)}")
        host = provider_host(provider)
        still_open = [p for p in context.pages if not p.is_closed() and host in (p.url or "")]
        if page.is_closed() and not still_open:
            say("   Skipped (tab closed).")
            return "skipped"
        text = page_texts(context)
        if provider.mail_from and now() - last_mail >= mail_every_s:
            last_mail = now()
            try:
                text += "\n" + inbox_text(provider.mail_from, started)
            except Exception as exc:
                log.debug("inbox read failed: %s", exc)
        found = find_working_key(text, provider, tried)
        if found:
            save(found)
            say(f"   ✓ Key found, tested with a real search, and saved ({', '.join(found)}).")
            try:
                page.close()
            except Exception:
                pass
            return "saved"
        sleep(poll_s)
    say(f"   No working key seen in {round(wait_minutes)} min — add it on the Settings page later.")
    return "timed out"


def provider_host(provider: Provider) -> str:
    return re.sub(r"^https?://(www\.)?", "", provider.signup_url).split("/")[0]
