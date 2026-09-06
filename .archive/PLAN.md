# Build Plan — Job Applier Agent

Working document. Each item is ticked only when it is **built, run, and verified** —
not when the code is merely written.

Legend: `[x]` done & verified · `[ ]` not started · `[~]` in progress · `[!]` blocked

Constraints that govern every item live in `constraints.txt`. Read it first.

**Phase numbers are IDs, not a schedule.** They are stable so `constraints.txt`
and commit messages keep referring to the same thing. What to build next is the
**Execution order** below, which is deliberately *not* 0,1,2,3,4.

---

## Execution order (revised 2026-09-06)

| # | Phase | Why here | Blocked? |
|---|---|---|---|
| 1 | **P5 — Pipeline integrity** | Half-built already (dead columns in the DB). Without it the tracker rots: it recommends and tailors for filled roles. | No |
| 2 | **P6 — Breadth** | 10 boards / 105 jobs is a demo, not a job search. Biggest single lever on outcomes, and Ashby is nearly free coverage. | No |
| 3 | **P7 — Daily loop** | Cheap glue that turns 8 manual commands into one habit. Makes everything downstream actually get used. | No |
| 4 | **P3B — Live tailoring** | Fully built, never run. Unblocks the moment a resume + key exist. | Yes — user |
| 5 | **P8 — Relevance v2** | Only worth doing once the funnel is wide (P6); re-ranking 105 jobs is pointless, re-ranking 3,000 is not. | Yes — key |
| 6 | **P4 — Assisted apply** | Highest risk, lowest reliability, ToS-constrained. Deliberately last, and recast from "auto-submit" to "prefill and hand over". | Yes — profile + go/no-go |
| 7 | **P9 — Outcome feedback** | Needs real applications to exist before it has anything to learn from. | After P4 |

### What changed and why

The old plan was ordered **0 → 1 → 2 → 3 → 4**, which put the riskiest,
least reliable, most ToS-constrained work (auto-submission) at the finish line,
and left everything else waiting on two user-supplied files. Five problems with
that, all addressed above:

1. **The whole plan was blocked on the user** while genuinely unblocked,
   high-value work (freshness, breadth, the daily loop) wasn't scheduled at all.
2. **Breadth was ticked as "discovery works"** — but working discovery and a
   built inventory are different things. The pipeline runs against 10 companies.
3. **Postings never close.** The lifecycle columns exist and are dead. A job
   board drops a role the day it is filled; nothing here notices.
4. **Submission was framed as the goal.** It is the part most likely to burn an
   account, hit a CAPTCHA, and violate a ToS, for the smallest time saving.
   The time actually goes into screening questions and cover letters.
5. **Nothing closed the loop.** No application outcomes, so the filters and
   thresholds could never be tuned by evidence — only by guessing.

---

## Phase 0 — Foundation

**Status: COMPLETE.**

- [x] Project skeleton (`config/`, `db/`, `scraper/`, `excel/`, `resume/`, `submitter/`, `tests/`)
- [x] `requirements.txt` covering all phases
- [x] `.gitignore` (secrets, DB, logs, resumes, audit artifacts)
- [x] `.env.example` + `python-dotenv` loading; no hardcoded secrets
- [x] `config/config.yaml` as the single source of tunables
- [x] `config/loader.py` — dotted-path lookup, UA templating, logging setup
- [x] `constraints.txt` — scope, politeness, and safety decisions recorded
- [x] Virtualenv created, dependencies installed
- [x] `README.md` with setup + usage
- [x] `db/migrate.py` — additive-only column migration, run on every startup
- [ ] Put the project under git — **it is not a repo today**, so every change
      since Aug 31 is unrecoverable and `PLAN.md.bak` is the only history

## Phase 1 — Job Discovery

**Status: COMPLETE.** Verified live: 10 companies, 3,110 postings seen, 105 matched, 0 duplicates on re-run.

- [x] SQLAlchemy models: `Company`, `Job`, `ScrapeLog`
- [x] Unique constraint `(source, company_slug, external_id)` for dedupe
- [x] `content_hash` so an edited posting updates in place instead of duplicating
- [x] Session/engine setup resolving SQLite paths from any cwd
- [x] `PoliteClient`: robots.txt fetch + cache + honor, per-host delay w/ jitter
- [x] Bounded retries with exponential backoff; `Retry-After` respected on 429
- [x] Honest, contactable User-Agent (no browser impersonation)
- [x] `PortalScraper` ABC + `RawJob` normalised shape
- [x] Greenhouse scraper against the public board API
- [x] Lever scraper against the public postings API
- [x] HTML → readable plain text for JD bodies
- [x] Requirements-section extraction heuristic (returns None rather than guessing)
- [x] Keyword filters: title include/exclude, location, JD-body required
- [x] Company discovery: name → candidate slugs → live ATS probe
- [x] Inventory CSV import (`name,slug,source`)
- [x] Runner: per-company failure isolation, logged to `scrape_log`, run continues
- [x] Auto-deactivate a company after N consecutive failures
- [x] CLI: `init-db`, `scrape`, `discover`, `import-csv`, `stats`, `failures`
- [x] 20 offline unit tests (filters, parsers, hashing, slug derivation)
- [x] Live verification run against real Greenhouse + Lever boards
- [ ] Narrow `filters.title_keywords` to the user's actual target roles — **needs user input**

## Phase 2 — Excel Tracking Sheet

**Status: COMPLETE.** 105 jobs exported live; re-export is a clean no-op.

- [x] `excel/tracker.py` module
- [x] Read newly-discovered jobs from the DB (drive off `exported_to_excel`)
- [x] Column set: Company · Job Title · Application Link · JD · Location · Posting Date ·
      Required Skills · Date Found · Application Status · ATS Match Score
- [x] Create workbook if absent; append to it if present
- [x] Append-only semantics — never duplicate or clobber an existing row
- [x] Preserve user edits to Status when re-exporting (the sheet is user-writable)
- [x] Update in place when a posting changed (content_hash moved) rather than re-appending
- [x] Application Link written as a real clickable hyperlink
- [x] JD truncated to a cell-safe length, with full text still in the DB
- [x] Freeze the header row
- [x] Sensible column widths + autofilter; hidden `Job Key` join column
- [x] Conditional formatting on the Status column (7 states, not duplicated per export)
- [x] Data-validation dropdown for Status values
- [x] Mark rows `exported_to_excel` so the next run only appends new ones
- [x] CLI: `python main.py export` (`--all` to re-export everything)
- [x] Unit tests: append idempotency, status preservation, formatting applied (14 tests)
- [x] Verified live against the real DB (105 jobs)

## Phase 3A — ATS Scoring

**Status: COMPLETE.** Verified live on 105 real postings and calibrated.

- [x] `resume/parser.py` — extract structured text from .docx, .pdf, .txt/.md
- [x] Auto-detect the base resume in `resume/`, ignoring Word lock files
- [x] `resume/scorer.py` — weighted keyword overlap between resume and JD
- [x] Scorer works standalone (no API key, no cost) and is unit-tested
- [x] Score against the *requirements* section, not company blurb/benefits
- [x] Skills weighted above generic JD prose; company-name tokens excluded
- [x] Threshold calibrated against 105 real postings (max 47%, p90 31%, median 20%)
- [x] Write match score back to the DB and into the Excel sheet
- [x] Threshold flag: below `resume.min_score` → "Manual Review", and the flag
      clears symmetrically when a job later clears the bar
- [x] CLI: `score`, `reparse`
- [x] Scoring verified live: 105 jobs ranked, backend roles top, frontend bottom

## Phase 3B — Resume Tailoring

**Status: CODE COMPLETE, NEVER RUN.** Written and unit-tested; needs the user's
resume + API key for a live run.

- [x] `resume/tailor.py` — Claude API call (`claude-opus-5`, adaptive thinking, streaming)
- [x] Anti-fabrication guard: prompt rule + post-hoc check for invented skills,
      metrics, dates, and employers
- [x] Guard shares one skill vocabulary with the scorer so they can't drift
- [x] Reject any tailored output that fails the fabrication check
- [x] `resume/writer.py` — ATS-safe .docx (no tables, columns, text boxes, graphics)
- [x] Employers, dates, and education copied through verbatim — never rewritten
- [x] Review note (`*_review.txt`) listing rewrites, omissions, and honest gaps
- [x] Deterministic output filenames tying a resume version to a job
- [x] Skip re-tailoring a job that already has an output file (API cost control)
- [x] CLI: `tailor [--job-id N] [--limit N]`
- [x] 42 offline unit tests for scorer, guard, parser, writer, pipeline
- [!] Live tailoring run — **blocked on user**: base resume in `resume/` + `ANTHROPIC_API_KEY`

**New — cost control (none exists today; every tailor call is an Opus call):**

- [ ] `resume.max_spend_per_run_usd` — hard stop before exceeding it, not after
- [ ] Log per-call input/output tokens and running cost to the DB
- [ ] `--estimate` — print projected cost for a selection and exit without calling
- [ ] Model tiering: draft on a cheaper model, escalate to Opus only for the shortlist
- [ ] Cache the JD→requirements analysis so re-tailoring the same job is not re-paid for

**Done when:** a real tailored .docx exists for a real posting, the guard has been
seen to pass *and* to reject, and a run's cost is known before it starts.

## Phase 5 — Pipeline Integrity (freshness & closure) — **DO FIRST**

**Status: HALF-BUILT AND INERT.** `Job.last_seen_at`, `Job.is_open`, and
`Job.closed_at` exist in the model and were migrated into the live DB on
2026-09-06 — and **no code reads or writes any of them** (`grep` confirms:
only `models.py` mentions them). Every job in the DB has been `is_open=True`
since the day it was found, including any that were filled a week later.

Why this is first: a stale row is worse than a missing one. It gets ranked, it
gets an Opus call spent on it, and it wastes the one thing the whole project is
supposed to save — the user's time.

- [ ] Runner advances `last_seen_at` for every job returned by a **successful**
      company fetch
- [ ] After a successful fetch, jobs for that company that were *not* returned
      get `is_open=False` + `closed_at`
- [ ] A failed or partial fetch closes **nothing** — an outage must never look
      like a mass closure (this is the bug that makes naive versions of this
      feature dangerous)
- [ ] Closure is per-company and only for companies scraped in that run
- [ ] Re-opening: a posting that reappears clears `closed_at` and goes open again
- [ ] `scorer` and `tailor` skip closed jobs by default (`--include-closed` to override)
- [ ] Excel: a `Closed` status flows through, without clobbering a user-set status
      (same precedence rule Phase 2 already established)
- [ ] `stats` reports open vs closed, and median posting age
- [ ] Staleness guard: warn when a company has not been successfully scraped in N days
- [ ] Unit tests: closure on success, **no** closure on failure, reopen, user-status precedence

**Done when:** a scrape run closes a posting that really vanished from a board,
a simulated board outage closes nothing, and `stats` shows the open/closed split.

## Phase 6 — Breadth (get off 10 companies) — **DO SECOND**

**Status: NOT STARTED.** Phase 1 proved discovery *works*; it was never *used*.
The DB holds 10 companies. Stripe alone had 580 postings and matched 6 — that
ratio is what a real inventory has to overcome by volume.

- [ ] **Ashby scraper** (`scraper/ashby.py`) — public posting API, same JSON shape
      as Greenhouse/Lever. Roughly one module for a large slice of the startup
      market the current two miss. Highest coverage-per-line available.
- [ ] Wire Ashby into `discovery.probe_sources` and `portals`
- [ ] Source real seed name lists (YC company list, tech-scaleup lists) — per
      `constraints.txt` §3, Fortune-1000 names are a poor seed, startups are rich
- [ ] Discovery run at scale: resumable, checkpointed, safe to Ctrl-C and restart
- [ ] Cache negative probes so re-running does not re-hammer boards for known misses
- [ ] Record probe outcome + date per candidate slug (a board can appear later)
- [ ] `discover --dry-run` to see candidate slugs before spending requests
- [ ] Respect politeness at scale — breadth comes from more hosts, never from a
      shorter delay against one (`constraints.txt` §5)
- [ ] `max_companies_per_run` becomes meaningful; rotate so every company gets
      scraped over a few days rather than all of them every run
- [ ] Cross-company duplicate detection (the same role reposted under two slugs)

**Done when:** ≥300 verified boards in `companies`, a full scrape completes
inside a reasonable window under the existing rate limits, and the tracker is
fed by all three ATSs.

**Target to agree with the user:** how many boards is "enough"? 300 is a
starting number, not a researched one.

## Phase 7 — Daily Loop — **DO THIRD**

**Status: NOT STARTED.** Today the workflow is 5+ commands run by hand, which
means in practice it is run once and then not again.

- [ ] `python main.py daily` — scrape → close stale → score → export, one command
- [ ] Idempotent and safe to run twice in a day
- [ ] A digest of what changed: new matches, newly closed, top new scores
- [ ] `--since` so "what's new since yesterday" is answerable
- [ ] Digest to a file (and optionally email) rather than only stdout
- [ ] Exit codes that mean something, so it can be put behind `cron`/`launchd`
- [ ] A run that partially fails still reports what succeeded
- [ ] `main.py` is ~330 lines and growing a command per phase — split the CLI
      into `cli/` before it becomes the bottleneck

**Done when:** one command, run daily, produces a digest the user actually reads.

## Phase 8 — Relevance v2

**Status: NOT STARTED.** The current scorer is unweighted keyword overlap, and
its own calibration is the argument for this phase: on 105 postings the *best*
match scored 47% and the median 20%. That is a weak signal to rank on, and it
cannot distinguish a must-have from a nice-to-have, or a senior role from a new-grad one.

- [ ] Structured JD facts: seniority, years of experience, visa/citizenship
      requirements, comp range, remote/hybrid/onsite — extracted once, stored
- [ ] Hard filters from those facts (a role demanding 10+ YOE should not rank at all)
- [ ] Must-have vs nice-to-have weighting within the requirements section
- [ ] LLM re-rank of only the top N (bounded, cheap) producing a fit verdict and
      a named gap list — not another opaque number
- [ ] Explainability: `score --why <job-id>` shows which terms drove the score
- [ ] Keep the free keyword scorer as the wide first pass; the LLM only re-ranks
- [ ] Recalibrate `resume.min_score` against the new distribution and document it
      in `config.yaml` the way the current number is

**Done when:** the top 20 by score are ones the user agrees are the top 20, and
disagreements are explainable.

## Phase 4 — Assisted Apply (recast from "Submission")

**Gate: user go/no-go on scope before any code is written here.**

**Recast.** The old framing was "automate the submission". Everything in the
assessment below argues against that, and `constraints.txt` §9 already commits
to human-in-the-loop. So the deliverable is changed: **assemble the application,
prefill the form, and hand the browser to the user.** The final click is always
theirs. This keeps every benefit (no retyping, no re-uploading, no re-answering
the same screening question) and drops the ToS, CAPTCHA, and account-flag risk
that made the original framing the worst-ROI phase in the plan.

### Reliability assessment (done — read before deciding where to keep manual review)

| Portal | Automating the form | Verdict |
|---|---|---|
| **Greenhouse** | Most predictable. Stable, mostly static form markup, consistent field names, plain file input for the resume. Embedded iframes on some company-hosted pages are the main annoyance. | **Confident** — worth automating field-fill + upload. |
| **Lever** | Also predictable and simpler than Greenhouse. Fewer custom questions in practice. | **Confident** — same treatment as Greenhouse. |
| **Workday** | Hostile to automation: per-tenant subdomains, heavy JS, shadow DOM, mandatory account creation, email verification, multi-step wizards that lose state. Selectors differ per tenant, so "supporting Workday" means supporting each company separately. | **Not recommended.** Track manually. |
| **Custom / in-house** | Unbounded variety. No shared structure to code against. | **Not automatable generically.** Flag for manual application. |

Cutting across all four: **CAPTCHAs, bot-detection, and account flags are the real
failure mode, not selectors** — and most portals' ToS prohibit automated submission
outright (`constraints.txt` §9). That is why CAPTCHA is a hard stop rather than
something to work around.

- [x] Written reliability assessment: Greenhouse vs Lever vs Workday vs custom
- [ ] `config/profile.yaml` — name, contact, work history, EEO answers, links
- [ ] **Application packet** per job: tailored resume + cover letter + prefilled
      answers + link, in one folder, usable even with zero browser automation
- [ ] Cover letter generation under the same anti-fabrication guard as the resume
- [ ] **Screening-answer bank**: answers keyed by question similarity, reused
      across applications, always user-approved before first use
- [ ] `submitter/base.py` — shared fill/verify lifecycle
- [ ] Playwright session, **headed always** — the user watches, the user clicks
- [ ] Greenhouse form filler (standard fields + resume upload), stops before Submit
- [ ] Lever form filler (standard fields + resume upload), stops before Submit
- [ ] Custom/screening question detection — surfaced, never auto-answered
- [ ] CAPTCHA detection = hard stop, never an evasion attempt
- [ ] Daily application cap enforced from `limits.max_applications_per_day`
- [ ] Audit log: timestamp, screenshot, exact resume version, field values
- [ ] `Application` table + status write-back to DB and Excel
- [ ] Verified on a real posting, hand-off point confirmed before any submit

**Explicitly out of scope:** clicking Submit unattended, solving CAPTCHAs,
Workday, custom portals.

## Phase 9 — Outcome Feedback

**Status: NOT STARTED.** Nothing currently learns from what happened. Without
this, the filters, the threshold, and the target company list are tuned by
intuition forever.

- [ ] `Application` lifecycle: applied → response → screen → onsite → offer/reject
- [ ] Status changes readable back from the Excel sheet the user already edits
- [ ] Funnel stats: response rate by company, by title keyword, by score band
- [ ] Does score actually predict a response? Report the correlation honestly,
      including if the answer is "no" — that result would justify Phase 8
- [ ] Ghost detection: no response after N days → flag, stop counting as live
- [ ] Feed the evidence back: which title keywords and companies to drop
- [ ] `main.py report` — the one view of the whole search

**Done when:** the user can answer "is this working, and where is it leaking?"
with data rather than a feeling.

---

## Blocked on user

1. **Base resume file** (`.docx` or `.pdf`) into `resume/` — unblocks the live
   Phase 3B tailoring run. Everything else in 3B is built and tested.
   *(Confirmed absent as of 2026-09-06.)*
2. **`ANTHROPIC_API_KEY`** in `.env` — same. *(No `.env` file exists yet.)*
3. **Target role keywords** — current filters are a broad SWE default, which is
   why Stripe matched 6 of 580 postings. Narrowing them improves signal.
4. **Profile data** (contact, work history, EEO answers) — needed for Phase 4.
5. **Go/no-go on the Phase 4 recast** — prefill-and-hand-over instead of
   auto-submit. This is a scope *reduction*; confirm it is the one you want.
6. **Breadth target** — is 300 boards the right number for Phase 6?

**None of items 1–6 block Phases 5, 6, or 7.** That is the point of the reorder:
there is roughly a week of high-value, fully unblocked work available right now.
