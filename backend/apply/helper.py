"""A form-filling browser extension (SpeedyApply, by default) in the apply browser.

Our filler knows Greenhouse. An autofill extension knows more portals --
Workday above all -- and when a form stops at "Waiting for you" the person
can press the extension's button to fill whatever we left blank. It only
ever helps a person: nothing here clicks it, reads what it fills, or trusts
its answers. Our own filling, the resume chosen for the job, and every check
still run first.

The extension is copied from the person's own Chrome, where they installed
it; nothing is downloaded. It keeps its profile (name, answers, resume) in
the apply browser's own profile folder, `data/apply-browser/`, set up once
with `main.py setup-helper` and kept between runs. That folder is theirs and
is never committed.

Playwright's bundled Chromium is used, not Google Chrome: Chrome stopped
honouring --load-extension in 2025.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from backend.config.loader import PROJECT_ROOT

log = logging.getLogger(__name__)

SPEEDYAPPLY_ID = "mbgjopdedgonlbpikjfibkccpmhjbnag"
BROWSER_DIR = PROJECT_ROOT / "data" / "apply-browser"
CHROME_DIRS = [
    Path.home() / "Library" / "Application Support" / "Google" / "Chrome",   # macOS
    Path.home() / ".config" / "google-chrome",                              # Linux
    Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data",  # Windows
]


def _version_key(path: Path) -> tuple:
    return tuple(int(p) if p.isdigit() else 0
                 for p in path.name.split("_")[0].split("."))


def find_installed(ext_id: str, chrome_dirs: list[Path] | None = None) -> Path | None:
    """The newest copy of this extension in any Chrome profile, or None."""
    found = []
    for root in chrome_dirs or CHROME_DIRS:
        found += [v for v in root.glob(f"*/Extensions/{ext_id}/*") if (v / "manifest.json").exists()]
    return max(found, key=_version_key) if found else None


def prepare(cfg, *, chrome_dirs: list[Path] | None = None,
            browser_dir: Path | None = None) -> Path | None:
    """Copy the extension beside the apply profile; its folder, or None.

    Copied rather than loaded in place: Chrome replaces that folder when it
    updates the extension, and refuses to load the `_metadata` folder the
    Web Store adds, which an unpacked extension may not carry.
    """
    if not cfg.get("apply.helper_extension.enabled", False):
        return None
    ext_id = str(cfg.get("apply.helper_extension.id", SPEEDYAPPLY_ID))
    source = find_installed(ext_id, chrome_dirs)
    if source is None:
        log.info("Helper extension %s is not installed in Chrome — applying without it", ext_id)
        return None
    target = (browser_dir or BROWSER_DIR) / "extensions" / ext_id
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("_metadata"))
    return target


def launch(manager, cfg, *, user_agent: str, viewport: dict,
           browser_dir: Path | None = None):
    """(browser, context) for applying.

    With a helper extension: one kept profile, so the extension's own setup
    survives, and no separate browser object. Without: a fresh browser each
    run, as before.
    """
    extension = prepare(cfg, browser_dir=browser_dir)
    if extension is None:
        browser = manager.chromium.launch(headless=False)
        return browser, browser.new_context(user_agent=user_agent, viewport=viewport)
    profile = (browser_dir or BROWSER_DIR) / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    context = manager.chromium.launch_persistent_context(
        str(profile), headless=False, user_agent=user_agent, viewport=viewport,
        args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"])
    log.info("Apply browser has the helper extension from %s", extension.name)
    return None, context
