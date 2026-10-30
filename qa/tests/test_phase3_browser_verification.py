"""Phase 3 verification on fictional local pages, with preserved traces and screenshots."""
import json
import os
from pathlib import Path

import pytest

from qa.browser_checks import browser_diagnostics, jobs_page_settled
from qa.tests.test_apply_page_presses import app_copy  # noqa: F401


@pytest.fixture
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as manager:
        browser = manager.webkit.launch()
        yield browser
        browser.close()


def test_jobs_stability_wait_and_review_fifteen_times(app_copy, browser, tmp_path):
    base, started = app_copy
    for attempt in range(15):
        width = 1440 if attempt % 2 == 0 else 390
        context = browser.new_context(viewport={"width": width, "height": 900 if width > 500 else 844},
                                      service_workers="block", reduced_motion="reduce")
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
        page = context.new_page()
        try:
            with browser_diagnostics(page, tmp_path, f"jobs-review-{attempt:02}-{width}") as folder:
                page.goto(base + "/", wait_until="networkidle")
                # A fictional delayed status card reproduces the movement without reading the real
                # board-search/careers files the old scratchpad servers used.
                page.evaluate("""() => {
                  const bar = document.getElementById('q').closest('.bar');
                  const status = document.createElement('div'); bar.before(status);
                  setTimeout(() => { status.style.height = '240px'; }, 100);
                  setTimeout(() => { status.style.height = '490px'; }, 250);
                }""")
                settled = jobs_page_settled(page)
                (folder / "stability.json").write_text(json.dumps(settled))
                assert settled["moved"] == 490 and settled["elapsed"] >= 900
                page.locator("#pick-all").check()
                page.wait_for_function("document.getElementById('pick-n').textContent === '3 selected'")
                page.locator("#pick-review").click()
                page.wait_for_function("document.getElementById('pickd').open")
                assert page.locator("#pickd").is_visible()
                page.screenshot(path=str(folder / "review.png"))
                page.keyboard.press("Escape")
                assert not page.locator("#pickd").is_visible()
                page.locator("#pick-clear").click()
                page.wait_for_function("document.getElementById('pick-n').textContent === '0 selected'")
        finally:
            context.close()
    assert started == []  # selecting/reviewing never starts an application or a writer


def test_stability_wait_has_a_deadline(browser, tmp_path):
    page = browser.new_page()
    try:
        with browser_diagnostics(page, tmp_path, "never-settles"):
            page.set_content('<div id="moving"></div><div class="bar"><input id="q"></div>')
            page.evaluate("() => setInterval(() => document.getElementById('moving').style.height = (Date.now() % 50) + 'px', 20)")
            with pytest.raises(Exception, match="Jobs page did not settle"):
                jobs_page_settled(page, still_ms=700, timeout_ms=300)
    finally:
        page.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_short_motivation_question_offers_a_reviewable_draft(app_copy, browser, tmp_path, width):
    base, started = app_copy
    context = browser.new_context(viewport={"width": width, "height": 900})
    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
    question = "What Excites You About This Role?"
    group = {"label": question, "required": True, "options": [],
             "jobs": [{"job_id": 701, "company": "Example"}]}
    context.route("**/apply/questions.json", lambda route: route.fulfill(json={
        "auto_submit": False, "shared": [], "not_eligible": [],
        "jobs": [{"job_id": 701, "company": "Example", "title": "Engineer", "required": 1,
                  "open": 1, "questions": [group]}]}))
    drafted = []

    def draft(route):
        drafted.append(route.request.post_data_json)
        route.fulfill(json={"ok": True, "answer": "I enjoy improving reliable systems.",
                            "gaps": [], "unsupported_numbers": [], "hedges": [], "from_earlier": []})
    context.route("**/apply/draft", draft)
    page = context.new_page()
    try:
        with browser_diagnostics(page, tmp_path, f"short-motivation-{width}"):
            page.goto(base + "/apply/questions", wait_until="networkidle")
            box = page.get_by_label(question, exact=False)
            assert box.evaluate("el => el.tagName") == "TEXTAREA"
            page.get_by_role("button", name="Draft with AI", exact=True).click()
            page.wait_for_function("document.querySelector('[data-g]').value.includes('reliable systems')")
            assert drafted == [{"job_id": 701, "label": question}]
            assert "read and edit it, then press Save answers" in page.locator("body").inner_text()
    finally:
        context.close()
    assert started == []


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_submission_notices_on_desktop_and_phone(app_copy, browser, tmp_path, width, theme):
    base, started = app_copy
    context = browser.new_context(viewport={"width": width, "height": 900 if width > 500 else 844},
                                  service_workers="block", reduced_motion="reduce")
    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
    context.add_init_script(f"localStorage.setItem('theme', '{theme}')")
    page = context.new_page()
    checks = {"/apply": "These runs stop for you to press Submit on every form.",
              "/apply/questions": "This setting applies only when you press",
              "/applicant": "Allow automatic submission for Eligible Greenhouse jobs through Apply to these",
              "/job/701": "This job-page run always waits for you, including on Greenhouse."}
    try:
        for path, words in checks.items():
            with browser_diagnostics(page, tmp_path, f"notice-{path.strip('/').replace('/', '-')}-{width}-{theme}") as folder:
                response = page.goto(base + path, wait_until="domcontentloaded")
                assert response.status == 200
                assert words in page.locator("body").text_content()
                # Exercise keyboard activation from a known focus position. WebKit's first
                # Tab after navigation can stay on BODY, depending on platform focus settings.
                toggle = page.locator("#theme-btn")
                before = toggle.get_attribute("aria-label")
                toggle.focus()
                page.keyboard.press("Enter")
                assert toggle.get_attribute("aria-label") != before
                page.keyboard.press("Enter")
                page.keyboard.press("Enter")
                assert toggle.get_attribute("aria-label") == before
                focused = page.evaluate("() => ({tag: document.activeElement.tagName, outline: getComputedStyle(document.activeElement).outlineStyle})")
                assert focused["tag"] == "BUTTON"
                (folder / "keyboard.json").write_text(json.dumps(focused))
                if path == "/job/701":
                    page.locator("#aa").evaluate("el => el.hidden = false")  # expose the notice, start nothing
                page.get_by_text(words, exact=False).first.scroll_into_view_if_needed()
                axe = os.environ.get("PHASE3_AXE")
                if axe:
                    page.add_script_tag(path=str(Path(axe)))
                    result = page.evaluate("async () => await axe.run(document, {runOnly: {type:'tag', values:['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa']}})")
                    (folder / "axe.json").write_text(json.dumps(result))
                    # Existing page-wide issues are reported for review; this check changes copy only.
    finally:
        context.close()
    assert started == []
