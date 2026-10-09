"""The person's job-site account: one email and password, used to sign in where a site needs it.

Workday (and others like it) want an account per employer. The person types
the email and password once, on the Settings page; the email is kept in the
applicant profile, the password only in the macOS Keychain -- never in a
file, never in git, never in a chat. Use one password just for job sites.

On a Workday sign-in page the apply browser signs in with them. Where there
is no account yet it opens "Create Account" and fills the email and both
password boxes -- and stops: agreeing to the employer's terms, a CAPTCHA and
the emailed code are the person's to do. Then filling carries on.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SERVICE = "AI_Assistant_Job_Applier job sites"
EMAIL_KEY = "email"          # the account the password is stored under


class NoAccount(RuntimeError):
    """No job-site email and password have been saved."""


def _keyring():
    import keyring
    return keyring


def save(email: str, password: str) -> None:
    email, password = (email or "").strip(), password or ""
    if "@" not in email:
        raise ValueError("Enter the email address you use for job sites.")
    if len(password) < 8:
        raise ValueError("Use a password of at least 8 characters — most job sites require it.")
    kr = _keyring()
    kr.set_password(SERVICE, EMAIL_KEY, email)
    kr.set_password(SERVICE, email, password)


def email() -> str | None:
    try:
        return _keyring().get_password(SERVICE, EMAIL_KEY)
    except Exception:
        return None


def credentials() -> tuple[str, str]:
    address = email()
    password = _keyring().get_password(SERVICE, address) if address else None
    if not address or not password:
        raise NoAccount("Save your job-site email and password on the Settings page first.")
    return address, password


def forget() -> None:
    kr = _keyring()
    address = email()
    for user in filter(None, (address, EMAIL_KEY)):
        try:
            kr.delete_password(SERVICE, user)
        except Exception:
            pass


def status() -> dict:
    address = email()
    try:
        has_password = bool(address and _keyring().get_password(SERVICE, address))
    except Exception:
        has_password = False
    return {"email": address or "", "saved": has_password}


# -- Workday ------------------------------------------------------------------------

def _visible(page, selector: str) -> bool:
    try:
        el = page.query_selector(selector)
        return bool(el and el.is_visible())
    except Exception:
        return False


def _fill(page, automation_id: str, value: str) -> bool:
    el = page.query_selector(f'[data-automation-id="{automation_id}"]')
    if not el or not el.is_visible():
        return False
    el.fill(value)
    return True


def _click(page, el) -> bool:
    """Click a Workday button. Workday lays a transparent `click_filter` div over
    its buttons that takes the click (Abercrombie, Oct 2026), and the page can
    redraw mid-click; so try the button, then the overlay with the same name,
    then a script click -- and never let a failed click end the run."""
    try:
        name = el.get_attribute("aria-label") or el.inner_text()
    except Exception:
        name = ""
    attempts = [lambda: el.click(timeout=5000)]
    if name:
        attempts.append(lambda: page.click(f'[data-automation-id="click_filter"][aria-label="{name.strip()}"]',
                                           timeout=5000))
    attempts.append(lambda: el.evaluate("e => e.click()"))
    for attempt in attempts:
        try:
            attempt()
            return True
        except Exception as exc:
            log.debug("Workday click: %s", exc)
    return False


def workday_sign_in(page, state: dict, say=print) -> str:
    """Do the account step on a Workday sign-in page. Returns what happened.

    `state` is kept per tab: sign in is tried once; if Workday refuses it
    (no account there yet) the Create Account form is filled once, and left
    for the person to agree to the terms and press Create Account.
    """
    try:
        address, password = credentials()
    except NoAccount:
        return "no-account-saved"

    creating = _visible(page, '[data-automation-id="verifyPassword"]')
    if creating and not state.get("signed_in_tried") and not state.get("to_sign_in"):
        # Workday often opens on Create Account; the account may already exist, so sign in first.
        for link in ('[data-automation-id="signInLink"]', 'button:has-text("Sign In")'):
            el = page.query_selector(link)
            if el and el.is_visible() and _click(page, el):
                state["to_sign_in"] = True
                return "to-sign-in"
    if creating:
        if state.get("created"):
            return "waiting"
        _fill(page, "email", address)
        _fill(page, "password", password)
        _fill(page, "verifyPassword", password)
        state["created"] = True
        say("   New Workday account filled with your job-site email and password. Tick the "
            "employer's terms box and press Create Account (and enter any emailed code) — "
            "I'll carry on after that.")
        return "create-filled"

    if not state.get("signed_in_tried") and _visible(page, '[data-automation-id="password"]'):
        _fill(page, "email", address)
        _fill(page, "password", password)
        for button in ('[data-automation-id="signInSubmitButton"]', 'button:has-text("Sign In")'):
            el = page.query_selector(button)
            if el and el.is_visible() and _click(page, el):
                break
        state["signed_in_tried"] = True
        say("   Signing in to this employer's Workday with your job-site account…")
        return "signing-in"

    if state.get("signed_in_tried") and not state.get("create_opened"):
        # Still on the sign-in page after trying: no account with this employer yet.
        page.wait_for_timeout(2500)
        if not _visible(page, '[data-automation-id="password"]'):
            return "waiting"
        for link in ('[data-automation-id="createAccountLink"]', 'button:has-text("Create Account")',
                     'a:has-text("Create Account")'):
            el = page.query_selector(link)
            if el and el.is_visible() and _click(page, el):
                state["create_opened"] = True
                say("   No account with this employer yet — opening Create Account.")
                return "create-opened"
    return "waiting"
