# UI/UX and Frontend Playbook

## Scope

This playbook is curated for the AI_Assistant_Job_Applier UI redesign: a personal, local Flask/Jinja app
(one stylesheet `frontend/static/app.css`, inline vanilla JS, no build step) used by one person on a
13-inch Retina MacBook and sometimes a phone. It supports the planned "restrained liquid glass" direction.
It is not exhaustive. Each principle is paraphrased from a source that was opened on 2026-10-08; nothing
here replaces the source itself. Principles are numbered so reviews and commits can refer to them
(for example "P14").

Format of each entry: a short summary, how it applies to a named page of this app, and the source.

---

## 1. Page structure and navigation

### P1. One shared top navigation with few, stable items
Service navigation should tell people where they are and let them move between the main sections; keep the
number of items small and collapse them into a menu button on narrow screens.
- **How it applies here:** Put Jobs, Auto-apply, Application questions, Resume Tailoring, Assistant and
  Settings in one bar rendered from the base template; move Applicant profile and Health report under
  Settings or a "More" group so the bar fits a 390px phone after collapsing.
- **Source:** GOV.UK Design System, Service navigation, https://design-system.service.gov.uk/components/service-navigation/, accessed 2026-10-08

### P2. Mark the current page programmatically
Use `aria-current="page"` on the one navigation link that matches the current page so the visual
"selected" style is also announced by screen readers.
- **How it applies here:** On Job detail, the "Jobs" link carries `aria-current="page"`; the highlighted
  style should be keyed off that attribute (`[aria-current="page"]`) rather than a separate class.
- **Source:** MDN, aria-current, https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Reference/Attributes/aria-current, accessed 2026-10-08

### P3. Let keyboard users skip repeated blocks
A mechanism must exist to bypass content repeated on every page, typically a skip link plus landmarks
(`<header>`, `<nav>`, `<main>`).
- **How it applies here:** Add a "Skip to content" link as the first focusable element in the base
  template, targeting `<main id="main">`, so on the Jobs list a keyboard user lands directly on the filters.
- **Source:** W3C WAI, Understanding SC 2.4.1 Bypass Blocks, https://www.w3.org/WAI/WCAG22/Understanding/bypass-blocks.html, accessed 2026-10-08

### P4. A sticky bar must never fully hide the focused element
When a component receives keyboard focus it must not be entirely hidden by author content such as a sticky
header; scroll padding is the usual fix.
- **How it applies here:** The frosted top bar and the sticky table header on the Jobs list both overlay
  content. Set `scroll-padding-top` on `html` to the combined height of the bar (and header row) so tabbing
  through table rows and the eligibility form on Job detail keeps focus visible.
- **Source:** W3C WAI, Understanding SC 2.4.11 Focus Not Obscured (Minimum), https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum.html, accessed 2026-10-08

### P5. Build every page from one base template
Jinja template inheritance lets a base "skeleton" define blocks (head, title, content) that child templates
override, with `super()` to extend rather than replace a block.
- **How it applies here:** Create `base.html` holding the doctype, stylesheet link, top navigation, skip
  link and a status live region; each page (`jobs.html`, `job_detail.html`, `settings.html`, ...) only
  `{% extends %}` it and fills `{% block content %}` and an optional `{% block scripts %}`.
- **Source:** Flask documentation, Template Inheritance, https://flask.palletsprojects.com/en/stable/patterns/templateinheritance/, accessed 2026-10-08

---

## 2. Typography, color, spacing and design tokens

### P6. Layer tokens: reference, system, component
Mature systems separate raw values (reference tokens), role-based decisions (system tokens, e.g.
"primary", "surface") and per-component tokens, all exposed on the web as CSS custom properties.
- **How it applies here:** Keep raw palette values (`--teal-600`) separate from roles (`--color-signal`,
  `--surface-glass`, `--surface-table`, `--text-muted`) and only reference roles in component rules.
  Theming light/dark then means swapping role values on `:root`, not editing components.
- **Source:** Material Web (material-components/material-web), Theming docs, https://github.com/material-components/material-web/blob/main/docs/theming/README.md, accessed 2026-10-08

### P7. Use `light-dark()` with `color-scheme` for paired theme values
`light-dark()` returns one of two values depending on the active color scheme and requires
`color-scheme: light dark` to be declared; it is Baseline 2024.
- **How it applies here:** Role tokens such as `--surface-table: light-dark(#fff, #12161a)` keep the two
  existing themes in one declaration. Keep the existing explicit `[data-theme]` overrides for the manual
  toggle, since Safari/Chrome on the Mac support the function but older browsers will not.
- **Source:** MDN, light-dark(), https://developer.mozilla.org/en-US/docs/Web/CSS/color_value/light-dark, accessed 2026-10-08

### P8. Text contrast: 4.5:1 normal, 3:1 large, and always set a background
Body text needs 4.5:1 against its background and large text 3:1; images or busy backgrounds behind text are
a known failure, and setting a text color without a background color is also a failure.
- **How it applies here:** Every glass surface token must have a declared fallback background colour that
  passes 4.5:1 with `--text` and `--text-muted`. Check muted text in the Jobs stats strip and hint text on
  the Applicant profile form against the lightest point of the gradient showing through.
- **Source:** W3C WAI, Understanding SC 1.4.3 Contrast (Minimum), https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html, accessed 2026-10-08

### P9. Never rely on color alone
Color cannot be the only means of conveying information or state; add text, icons or patterns.
- **How it applies here:** Fit % and eligibility columns on the Jobs list must show a value or word
  ("Eligible", "Needs sponsorship") next to any coloured dot; Health report pass/fail needs text labels.
- **Source:** W3C WAI, Understanding SC 1.4.1 Use of Color, https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html, accessed 2026-10-08

### P10. Tabular figures for numbers that are compared
`font-variant-numeric: tabular-nums` makes all digits equal width so columns of numbers line up.
- **How it applies here:** Apply to fit %, dates and counts in the Jobs table, the Health report and the
  Resume Tailoring stage timings; IBM Plex Sans supports it, so Plex Mono need not be used just for alignment.
- **Source:** MDN, font-variant-numeric, https://developer.mozilla.org/en-US/docs/Web/CSS/font-variant-numeric, accessed 2026-10-08

### P11. One aesthetic, spent in one place
A distinctive interface commits to a clear direction rooted in the product, spends boldness on one
memorable element and keeps the rest quiet, and avoids generic template tells (uniform rounded card kits,
scattered motion).
- **How it applies here:** Let the gradient backdrop and frosted top bar be the signature; keep tables,
  forms and buttons plain and consistent, using the teal signal colour only for primary actions and focus.
- **Source:** Anthropic skills repository, frontend-design SKILL.md, https://github.com/anthropics/skills/blob/main/skills/frontend-design/SKILL.md, accessed 2026-10-08

---

## 3. Forms

### P12. Visible labels above fields; never placeholder-as-label
Every input needs a visible label; hints are short and sit under the label; placeholders vanish while typing
and are poorly supported by screen readers. Size inputs to the expected answer and use `autocomplete`.
- **How it applies here:** On the Applicant profile form, add `autocomplete="given-name"`, `email`, `tel`,
  `postal-code` etc., and give short fields (postcode, notice period) narrow widths. In Settings, API key
  fields keep a real label, not a "sk-..." placeholder.
- **Source:** GOV.UK Design System, Text input, https://design-system.service.gov.uk/components/text-input/, accessed 2026-10-08

### P13. Inline error messages that say how to fix it, and keep the input
Place the error under the label/hint, connected to the field visually, phrased plainly ("Enter a salary
as a number"), and keep what the person typed.
- **How it applies here:** Application questions is one long form; on a failed save, re-render with each
  answer preserved and a specific message next to the offending question instead of a generic banner.
- **Source:** GOV.UK Design System, Error message, https://design-system.service.gov.uk/components/error-message/, accessed 2026-10-08

### P14. An error summary at the top that links to each field
When validation fails, show a summary above the heading listing each error as a link to its field, move
focus to it, and use the same wording as the inline messages.
- **How it applies here:** Application questions and the Job detail eligibility form should render an error
  summary whose links jump to the question (`href="#q-123"`), with focus moved to the summary on load.
- **Source:** GOV.UK Design System, Error summary, https://design-system.service.gov.uk/components/error-summary/, accessed 2026-10-08

### P15. Group related choices in a fieldset with a legend; whole label is clickable
Groups of checkboxes or radios belong in a `<fieldset>` with a `<legend>` stating the question; labels sit
beside the control and enlarge the hit area.
- **How it applies here:** Work-authorisation and visa options on the eligibility form, and multi-choice
  application questions, each become a fieldset; the label wraps or is `for`-linked to the input.
- **Source:** GOV.UK Design System, Checkboxes, https://design-system.service.gov.uk/components/checkboxes/, accessed 2026-10-08

---

## 4. Data tables and dense data display

### P16. Freeze headers, aid row tracking, make sort/filter discoverable
Large tables should freeze header rows (and a key column when wide), use borders, zebra striping or hover
highlighting to keep the eye on a row, and expose filtering, sorting and column management cheaply.
- **How it applies here:** On the Jobs list use `position: sticky` on `thead th` (below the top bar),
  a row hover/focus-within highlight, and consider a column picker to hide eligibility columns on demand.
- **Source:** Nielsen Norman Group, Data Tables: Four Major User Tasks, https://www.nngroup.com/articles/data-tables/, accessed 2026-10-08

### P17. Sortable headers are buttons, with `aria-sort` on one column
Wrap each sortable header's text in a `<button>`; put `aria-sort="ascending|descending"` only on the
currently sorted `<th>` and move it when the sort changes.
- **How it applies here:** Replace clickable `<th>` handlers on the Jobs list with `<th aria-sort><button>`,
  and show the direction with an arrow glyph plus the attribute, not colour.
- **Source:** W3C WAI-ARIA APG, Sortable Table Example, https://www.w3.org/WAI/ARIA/apg/patterns/table/examples/sortable-table/, accessed 2026-10-08

### P18. Captions, `scope`, right-aligned numbers
Give tables a `<caption>`, mark headers with `scope="col"`/`scope="row"`, and right-align numeric columns
so values compare at a glance.
- **How it applies here:** Jobs list caption like "6,012 jobs, sorted by fit" (can be visually hidden);
  right-align fit % and salary; the Health report tables get the same treatment.
- **Source:** GOV.UK Design System, Table, https://design-system.service.gov.uk/components/table/, accessed 2026-10-08

### P19. Choose batch or live filtering deliberately
Batch filtering (an Apply button) suits users with several criteria or slow updates; interactive filtering
suits exploration, but should avoid jarring scroll jumps and can wait for a short pause before updating.
- **How it applies here:** With ~6,000 rows, Jobs list chips/selects can filter live client-side, but
  debounce text search, keep scroll position, and show the result count and a "Clear filters" action.
- **Source:** Nielsen Norman Group, Applying Filters, https://www.nngroup.com/articles/applying-filters/, accessed 2026-10-08

### P20. Keep the table opaque
Glass belongs to the control layer, not the content layer; data should sit on a solid surface.
- **How it applies here:** The Jobs table, Auto-apply queue and Health tables use an opaque
  `--surface-table`; only the top bar, filter bar and floating panels are frosted. See P34.
- **Source:** Apple Human Interface Guidelines, Materials (page data), https://developer.apple.com/tutorials/data/design/human-interface-guidelines/materials.json, accessed 2026-10-08

### P21. Skip rendering of off-screen rows
`content-visibility: auto` lets the browser skip layout and paint for off-screen content, with
`contain-intrinsic-size` reserving space to avoid scrollbar jumps; avoid APIs that force rendering of
skipped content.
- **How it applies here:** Apply to `tbody` chunks or row groups of the Jobs list (render rows in
  `<tbody>` groups of ~200) to cut initial render cost without introducing a virtual-scroll library.
- **Source:** web.dev, content-visibility, https://web.dev/articles/content-visibility, accessed 2026-10-08

---

## 5. Loading, empty, error and success states

### P22. Match the progress indicator to the wait
Spinners suit waits of roughly 2-10 seconds; beyond about 10 seconds show percent-done or step progress,
and lean towards progress bars when durations are unpredictable.
- **How it applies here:** Resume Tailoring's 6-stage run and tailoring on Job detail must show stage
  progress ("Stage 3 of 6: Rewriting bullets"), not a bare spinner; a quick filter needs no indicator.
- **Source:** Nielsen Norman Group, Progress Indicators Make a Slow System Less Insufferable, https://www.nngroup.com/articles/progress-indicators/, accessed 2026-10-08

### P23. Empty states explain status and offer the next action
An empty area should say why it is empty (nothing yet, filtered out, still loading), teach briefly, and
link to the task that fills it.
- **How it applies here:** Auto-apply with no prepared applications says so and links to the Jobs list;
  a Jobs list emptied by filters shows "No jobs match these filters" with a Clear filters button.
- **Source:** Nielsen Norman Group, Designing Empty States in Complex Applications, https://www.nngroup.com/articles/empty-state-interface-design/, accessed 2026-10-08

### P24. Success confirmation: a banner before the heading, used sparingly
Confirm a completed action with a success banner placed before the page heading; overuse teaches people to
ignore it.
- **How it applies here:** After Application questions are saved or a Settings sign-in succeeds,
  redirect with a flashed success banner at the top; do not show banners for routine saves of one field.
- **Source:** GOV.UK Design System, Notification banner, https://design-system.service.gov.uk/components/notification-banner/, accessed 2026-10-08

### P25. Status messages must be announced without stealing focus
Status updates must be programmatically determinable without receiving focus: `role="status"` for results
and success, `role="alert"` for errors, `role="log"` for sequential progress.
- **How it applies here:** Jobs list result counts use `role="status"`; Resume Tailoring stage updates use
  `role="log"`; a failed auto-submit on Application questions uses `role="alert"`.
- **Source:** W3C WAI, Understanding SC 4.1.3 Status Messages, https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html, accessed 2026-10-08

---

## 6. Accessibility (keyboard, focus, contrast, ARIA)

### P26. Live regions must exist before they change
Polite regions wait for idle; assertive ones interrupt and should be rare. The region must already be in the
DOM before its content updates; `status`, `log`, `alert` are implicit live regions, and pairing
`role="alert"` with `aria-live` can double-announce.
- **How it applies here:** Render empty `role="status"` and `role="log"` containers in the server HTML for
  the Assistant chat and Resume Tailoring; JS only fills them.
- **Source:** MDN, ARIA live regions, https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Guides/Live_regions, accessed 2026-10-08

### P27. Toggles: a native checkbox or `role="switch"` with `aria-checked`
A switch is a binary on/off control toggled with Space (optionally Enter); a native checkbox's `checked`
state can carry the value instead of `aria-checked`.
- **How it applies here:** The auto-submit toggle on Application questions should be
  `<input type="checkbox" role="switch">` with a visible label stating what "on" does.
- **Source:** W3C WAI-ARIA APG, Switch Pattern, https://www.w3.org/WAI/ARIA/apg/patterns/switch/, accessed 2026-10-08

### P28. Tabs: tablist/tab/tabpanel, arrow keys, one tab stop
Tabs use `tablist`, `tab` (with `aria-selected` and `aria-controls`) and `tabpanel`; arrow keys move between
tabs, Home/End jump to ends; activation can follow focus when panels are already loaded.
- **How it applies here:** The Resume Tailoring results tabs and any Job detail tabs (posting / prep /
  eligibility) use automatic activation, since content is server-rendered.
- **Source:** W3C WAI-ARIA APG, Tabs Pattern, https://www.w3.org/WAI/ARIA/apg/patterns/tabs/, accessed 2026-10-08

### P29. Visible, high-contrast focus with `:focus-visible`
Style focus with `:focus-visible` so keyboard users get a clear ring without mouse clicks showing one.
Focus indicators and component boundaries/states need 3:1 against adjacent colours; the AAA criterion
asks for an indicator at least as large as a 2px perimeter with 3:1 change.
- **How it applies here:** One token-driven ring (`outline: 2px solid var(--focus); outline-offset: 2px`)
  for every button, link, chip and input; verify 3:1 on both the frosted bar and the opaque table.
- **Sources:** MDN, :focus-visible, https://developer.mozilla.org/en-US/docs/Web/CSS/:focus-visible, accessed 2026-10-08;
  W3C WAI, Understanding SC 1.4.11 Non-text Contrast, https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html, accessed 2026-10-08;
  W3C WAI, Understanding SC 2.4.13 Focus Appearance, https://www.w3.org/WAI/WCAG22/Understanding/focus-appearance.html, accessed 2026-10-08

### P30. Targets at least 24x24 CSS px
Pointer targets must be at least 24x24 CSS px, or spaced so a 24px circle around each does not overlap a
neighbour; inline text links are exempt.
- **How it applies here:** Filter chips, sort buttons and row action icons on the Jobs list, and remove
  buttons in the Auto-apply queue, need a 24px minimum hit box (larger on phone).
- **Source:** W3C WAI, Understanding SC 2.5.8 Target Size (Minimum), https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html, accessed 2026-10-08

---

## 7. Responsive behaviour

### P31. Mobile-first fluid layout with a viewport meta tag
Declare `width=device-width`, build a single-column layout first with flexible Grid/Flexbox, and add
complexity at `min-width` breakpoints.
- **How it applies here:** Base styles target the 390px phone (single column, collapsed nav); a
  breakpoint around 1024px adds the Job detail two-column layout and the full filter bar for the laptop.
- **Source:** MDN, Responsive design, https://developer.mozilla.org/en-US/docs/Learn_web_development/Core/CSS_layout/Responsive_Design, accessed 2026-10-08

### P32. Reflow at 320px, except the two-dimensional data table
Content must work at 320 CSS px wide without horizontal scrolling, but data tables are exempt because they
need two dimensions; the rest of the page must still reflow.
- **How it applies here:** Forms, Settings, Assistant and the Auto-apply cards reflow fully; the Jobs table
  may scroll horizontally inside its own container, while the page around it does not.
- **Source:** W3C WAI, Understanding SC 1.4.10 Reflow, https://www.w3.org/WAI/WCAG22/Understanding/reflow.html, accessed 2026-10-08

### P33. On phones: signal horizontal scroll, lock the key column, let people pick columns
Mobile tables work better with sticky headers, a locked first column, a visible cue that more columns
exist, and a way to choose which columns to show.
- **How it applies here:** On the phone, the job title column is `position: sticky; left: 0`, the table
  wrapper shows a fade edge, and the column picker from P16 defaults to title, company, fit %.
- **Source:** Nielsen Norman Group, Mobile Tables, https://www.nngroup.com/articles/mobile-tables/, accessed 2026-10-08

---

## 8. Glass, blur, gradients and motion

### P34. Glass is for navigation and controls, used sparingly
Apple's Liquid Glass forms a functional layer for controls and navigation that content scrolls beneath; it
should not be used in the content layer, custom use should be limited to the most important elements, and
text-heavy glass uses the more opaque "regular" variant. It adapts to Reduce Transparency and Increase
Contrast.
- **How it applies here:** Frost the top bar, the Jobs filter bar and modal/popover surfaces; keep posting
  text on Job detail, chat messages and tables on solid surfaces.
- **Source:** Apple Human Interface Guidelines, Materials (page data), https://developer.apple.com/tutorials/data/design/human-interface-guidelines/materials.json, accessed 2026-10-08

### P35. Glass legibility: strong blur, checked contrast, user control
Glassmorphism stays usable when text meets contrast over every possible backdrop, the blur is strong
enough to flatten busy backgrounds, and people can reduce transparency.
- **How it applies here:** Keep the gradient low-contrast and low-frequency; use a fairly opaque tint
  (e.g. 70-80% surface alpha) plus blur on the top bar, and test contrast where table rows scroll under it.
- **Source:** Nielsen Norman Group, Glassmorphism: Definition and Best Practices, https://www.nngroup.com/articles/glassmorphism/, accessed 2026-10-08

### P36. `backdrop-filter` needs a translucent background and a fallback
`backdrop-filter` blurs what is behind an element only if the element's background is (partly)
transparent; it is Baseline 2024, and its effect stops at "backdrop roots" such as ancestors with
`opacity`, `filter`, `mask` or `clip-path`. Use `@supports not (...)` to provide a fallback.
- **How it applies here:** Default `.glass` to an opaque `--surface-glass-fallback`; inside
  `@supports (backdrop-filter: blur(1px))` switch to the translucent tint plus blur. Avoid `opacity` or
  `filter` on ancestors of the top bar, or the blur silently breaks.
- **Sources:** MDN, backdrop-filter, https://developer.mozilla.org/en-US/docs/Web/CSS/backdrop-filter, accessed 2026-10-08;
  MDN, @supports, https://developer.mozilla.org/en-US/docs/Web/CSS/@supports, accessed 2026-10-08

### P37. Honour Reduce Transparency
`prefers-reduced-transparency: reduce` matches the macOS/iOS "Reduce transparency" setting; it is not
Baseline, so treat it as an enhancement on top of an already-legible default.
- **How it applies here:** Under `@media (prefers-reduced-transparency: reduce)` set `.glass` to the
  opaque fallback, drop the blur and flatten the gradient backdrop on every page.
- **Source:** MDN, prefers-reduced-transparency, https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-reduced-transparency, accessed 2026-10-08

### P38. Honour Reduce Motion
`prefers-reduced-motion: reduce` reflects the OS setting; replace movement (slides, scaling, parallax)
with subtle fades or nothing.
- **How it applies here:** Remove any animated gradient drift, row slide-ins on Auto-apply and the
  Resume Tailoring progress stage transitions; keep instant state changes and a static progress bar.
- **Source:** MDN, prefers-reduced-motion, https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-reduced-motion, accessed 2026-10-08

### P39. Animate only transform and opacity; measure blur cost
`transform` and `opacity` animate on the compositor; other properties trigger layout or paint. Reserve
`will-change` for measured problems. Blur is a paint/compositing cost, so verify with DevTools paint
flashing, layer borders and frame stats.
- **How it applies here:** Keep blurred surfaces few and small (top bar, filter bar), never animate the
  blur radius, and never place a blurred layer over the whole scrolling Jobs table; check the frame-rate
  overlay while scrolling 6,000 rows on the laptop.
- **Sources:** web.dev, Animations guide, https://web.dev/articles/animations-guide, accessed 2026-10-08;
  Chrome for Developers, Rendering performance (DevTools), https://developer.chrome.com/docs/devtools/rendering/performance, accessed 2026-10-08

---

## 9. Frontend practices for a no-build Flask/Jinja app

### P40. Progressive enhancement through feature queries
Write the baseline CSS that works everywhere, then layer modern features inside `@supports` so missing
support degrades to something usable rather than broken.
- **How it applies here:** In `app.css`, order sections: tokens, base, layout, components, enhancements
  (`@supports` glass, `light-dark()`), then user-preference media queries last so they win.
- **Source:** MDN, @supports, https://developer.mozilla.org/en-US/docs/Web/CSS/@supports, accessed 2026-10-08

### P41. Shared markup lives in the base template; pages own only content
Template inheritance plus blocks keeps navigation, theme toggle, flash banners and live regions in one place,
so a fix lands once.
- **How it applies here:** Move repeated inline nav/flash markup out of each page into `base.html`; keep
  page-specific inline JS in `{% block scripts %}` so it is easy to find and test. (See P5.)
- **Source:** Flask documentation, Template Inheritance, https://flask.palletsprojects.com/en/stable/patterns/templateinheritance/, accessed 2026-10-08

---

## 10. Visual and functional testing

### P42. Screenshot baselines with Playwright, in a fixed environment
`toHaveScreenshot()` records a baseline on first run and diffs later runs; rendering varies by OS, browser and
hardware, so run in one consistent environment, tune `maxDiffPixels`/`threshold`, and use `stylePath` to
hide dynamic content.
- **How it applies here:** Capture each page at 1440x900 and 390x844, light and dark, plus emulated
  `reducedMotion: 'reduce'`; hide timestamps and fit-score churn on the Jobs list with a stylePath CSS file.
- **Source:** Playwright, Visual comparisons, https://playwright.dev/docs/test-snapshots, accessed 2026-10-08

### P43. Automated axe scans, scoped to WCAG 2.2 AA tags
`@axe-core/playwright`'s `AxeBuilder` scans a page, can `include`/`exclude` regions and filter by tags; it
catches common problems only, so manual checks remain necessary. axe-core reports no false positives by
design and finds roughly half of WCAG issues automatically, supporting WCAG 2.2.
- **How it applies here:** Add one test per page running AxeBuilder with
  `wcag2a, wcag2aa, wcag21a, wcag21aa, wcag22aa` tags, with test data seeded so the Jobs table,
  Auto-apply queue and error summaries all render.
- **Sources:** Playwright, Accessibility testing, https://playwright.dev/docs/accessibility-testing, accessed 2026-10-08;
  Deque, axe-core (GitHub), https://github.com/dequelabs/axe-core, accessed 2026-10-08

### P44. A manual keyboard pass on every page
Tab through each page checking that the focus indicator is always visible and moves logically, and that a
skip link is the first stop.
- **How it applies here:** Per release, keyboard-only: skip link, nav, Jobs filters, sort a column, open a
  job, toggle auto-submit on Application questions, switch Resume Tailoring tabs with arrow keys, and send
  a chat message on Assistant; confirm nothing is hidden under the sticky bar (P4).
- **Source:** W3C WAI, Easy Checks - A First Review of Web Accessibility, https://www.w3.org/WAI/test-evaluate/easy-checks/, accessed 2026-10-08

---

## Quick checklist for each redesigned page

- Extends `base.html`; one `<main>`; skip link works; current nav item has `aria-current="page"` (P1-P5).
- Uses role tokens only; contrast checked on glass and opaque surfaces in both themes (P6-P9, P35).
- Forms: visible labels, autocomplete, inline errors plus summary, fieldsets for groups (P12-P15).
- Tables: opaque, sticky header, sort buttons with `aria-sort`, tabular numbers, caption (P16-P21).
- Loading, empty, error and success states designed and announced (P22-P26).
- Focus ring visible everywhere; targets 24px or more; toggles and tabs follow APG (P27-P30).
- Works at 390px and 320px (except the table's own scroll) (P31-P33).
- Glass only on controls; fallback, reduced transparency and reduced motion handled; scroll perf checked (P34-P39).
- Screenshot, axe and keyboard checks pass (P42-P44).
