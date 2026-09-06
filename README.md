# Job Applier Agent

Personal job-search automation for one person. Scrapes real ATS job boards, tracks
every posting in SQLite, mirrors them into an editable Excel sheet, scores each one
against your resume, and tailors that resume per job — without inventing a single
thing you haven't done.

**This file is the whole project's documentation: setup, decisions, and plan.**
It replaces the old `README.md` + `constraints.txt` + `PLAN.md` split (originals in
`.archive/`). Code comments cite the constraint sections as **§C1–§C10** below;
those numbers are stable, don't renumber them.

### Status at a glance

| Part | State | Evidence |
|---|---|---|
| **P0** Foundation | ✅ Done | venv, config, logging, additive migrations |
| **P1** Job discovery | ✅ Done, verified live | 10 companies · 3,110 postings seen · 105 matched · 0 dupes on re-run |
| **P2** Excel tracker | ✅ Done, verified live | 105 exported · re-export is a no-op · your Status edits survive |
| **P3A** ATS scoring | ✅ Done, calibrated | 105 real postings: max 47%, p90 31%, median 20% |
| **P3B** Resume tailoring | ⚠️ Built, **never run** | Blocked: no resume file, no API key |
| **P5** Pipeline integrity | ✅ Done, verified live | Closed 3 vanished postings on a real run; a simulated outage closes nothing |
| **P6** Breadth | ❌ Not started — **next** | Discovery works but was never used at scale — still 10 boards |
| **P7** Daily loop | ❌ Not started | 5+ manual commands, so it gets run once |
| **P8** Relevance v2 | ❌ Not started | Needs P6 first |
| **P4** Assisted apply | ❌ Not started | Recast from auto-submit; needs your go/no-go |
| **P9** Outcome feedback | ❌ Not started | Needs real applications first |

**96 tests, all passing, all offline.** Under git as of 2026-09-06 (§C11).

### Contents

1. [Quick start](#1-quick-start) · 2. [Commands](#2-commands) · 3. [Configuration](#3-configuration)
· 4. [Layout](#4-layout) · 5. [Constraints & decisions](#5-constraints--decisions-c1c11)
· 6. [Build plan](#6-build-plan) · 7. [What I need from you](#7-what-i-need-from-you)

---

## 1. Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # Phase 1 and scoring need no API key

.venv/bin/python main.py init-db     # create tables, load seed companies
.venv/bin/python main.py scrape      # scrape every active company
.venv/bin/python main.py export      # append new jobs to the Excel tracker
.venv/bin/python main.py score       # rank them against your resume — free, no API
```

To go further than scoring:

1. Drop your base resume (`.docx` or `.pdf`) into `resume/` — auto-detected, gitignored.
2. Put `ANTHROPIC_API_KEY` in `.env` — needed only by `tailor`, never by `score`.

Two things worth understanding before you read a number out of this tool:

> **The score is a ranking, not a grade.** Calibrated on 105 real postings, a
> strong-fit resume scored 47% at best and 20% at the median. It measures coverage
> of a JD's salient terms, and no real resume approaches 100%. Tune
> `resume.min_score` rather than reading the percentage as a mark out of 100.

> **Nothing invents experience.** Claude may only rephrase, reorder, and
> re-emphasise what your resume already says. Every tailored output is checked
> afterwards for invented skills, metrics, dates, and employers; anything that
> fails is rejected and flagged for manual review rather than written out (§C8).

## 2. Commands

```bash
# Discovery
main.py init-db                          # create tables, sync seed companies
main.py scrape                           # scrape all active companies
main.py stats                            # counts + most recent finds
main.py failures                         # which boards failed, and why

# Building the company inventory
main.py discover --names my_companies.txt        # name -> slug -> live ATS probe
main.py discover --names fortune1000.csv --limit 200
main.py import-csv --file ats_companies.csv      # pre-built name,slug,source

# Tracking sheet
main.py export                           # append new jobs
main.py export --all                     # re-export everything

# Scoring and tailoring
main.py score                            # free, no API key
main.py score --rescore --top 20
main.py score --include-closed           # closed postings are skipped by default
main.py tailor --limit 3                 # needs ANTHROPIC_API_KEY
main.py reparse                          # re-derive requirements after a heuristic change
```

### Scaling the company list

There is no public directory of every company on Greenhouse/Lever/Ashby — each
runs its own board at a slug, so the name→slug map has to be **built, not fetched**
(§C3). `discover` derives candidate slugs from each name, asks each ATS whether that
board exists, and stores the hits; later `scrape` runs pick them up automatically.

Fortune-1000 names have a low hit rate: big enterprises mostly use Workday, Taleo,
and iCIMS. **Startup and tech-scaleup lists are a much richer seed.**

## 3. Configuration

Everything tunable lives in `config/config.yaml`. No behaviour is hardcoded.

| Section | What it controls |
|---|---|
| `database` | SQLite path |
| `excel` | tracker workbook path |
| `resume` | base resume path (null = auto-detect), output dir, `min_score`, overwrite |
| `http` | robots.txt enforcement, per-host delay, jitter, retries, backoff, UA |
| `portals` | which ATSs are enabled |
| `companies` | seed list; whether to also scrape discovered companies |
| `filters` | title keywords + exclusions, location include/exclude, JD keyword requirements |
| `limits` | companies per run, applications/day (P4), auto-deactivation, posting-closure safety (`close_on_empty_board`), staleness warning (`stale_company_warn_days`) |
| `discovery` | which ATSs to probe, corporate suffixes to strip from names |
| `logging` | level and log file |

**The role filters are still a broad software-engineering default.** That is why
Stripe matched 6 postings out of 580. Narrowing `filters.title_keywords` to your
actual targets is the single cheapest quality win available and it needs no code.

Location matching is word-boundary, so `"us"` matches `US-Remote` but not `Aarhus`.
Exclusions win over inclusions — that is what separates `Remote - USA` from
`Remote - India` when both match `remote`.

## 4. Layout

```
config/     config.yaml + loader (YAML + .env, logging setup)
db/         SQLAlchemy models (Company, Job, ScrapeLog), session, additive migrations
scraper/    http_client (robots + rate limiting), base, greenhouse, lever,
            filters, discovery, runner, lifecycle (closure + staleness)
excel/      tracker.py — DB -> editable workbook, append-only
resume/     parser, scorer, tailor, guard, writer, pipeline (+ your base resume)
submitter/  submission + audit log — EMPTY, Phase 4 not started
tests/      96 offline unit tests
data/       SQLite database + job_tracker.xlsx (gitignored)
logs/       run logs (gitignored)
.archive/   the pre-merge README / PLAN / constraints, kept because there is no git
```

### Testing

```bash
.venv/bin/python -m pytest tests/ -q
```

All parser tests run offline against captured payload shapes — no network.

---

## 5. Constraints & decisions (§C1–§C11)

These are the hard rules and the reasoning behind them. **Read this before changing
architecture or adding a portal.** Section numbers are cited from code comments and
`config.yaml`; they are stable.

### §C1 — Project nature
Personal automation for one person's job search. Not a commercial product, not a
service run on behalf of others. Built incrementally; each phase is reviewed and
tested before the next begins. Do not build ahead.

### §C2 — Scope: portals
**In scope:** Greenhouse (`boards-api.greenhouse.io`), Lever (`api.lever.co/v0/postings`),
and Ashby (planned, P6) — all public JSON APIs.

**Out of scope:** Workday (per-tenant subdomains, heavy JS, shadow DOM, session
auth — high effort, fragile; track manually), plus iCIMS, Taleo/Oracle,
SuccessFactors, BambooHR, and custom in-house sites.

**Rule:** one scraper module per **ATS type**, never per company. A company is a
tenant of an ATS, not a portal. Adding a company is a config/DB row, never code.

### §C3 — Scope: the company list
Goal is the widest net, not a hand-curated shortlist.

There is no public directory of "all companies on Greenhouse or Lever", so the
name→slug map is built two ways: **slug probing** (derive candidates from a name,
probe each ATS, keep the 200s) and **CSV ingest** of a community-maintained
inventory. Discovered companies are stored with their ATS type and slug, so
discovery runs once and scraping reuses the result.

Fortune 500/1000 lists are a **poor seed** — those companies overwhelmingly use
Workday/Taleo/iCIMS. Use such a list only as raw *name* input to the prober, never
as the target list.

### §C4 — Tech decisions (and deviations from the original spec)
Python 3.11+, SQLite via SQLAlchemy, pandas/openpyxl, pytest, python-dotenv,
`config.yaml` — all as originally specified.

**Deviation: no Playwright in Phase 1.** The original spec called for it; discovery
uses plain `requests` against the Greenhouse and Lever JSON APIs instead. Both
publish documented, stable endpoints returning every posting with its full
description in a single request. A headless browser would be far slower, far more
fragile, and much heavier on their servers, for zero gain. Playwright stays in
`requirements.txt` and is reserved for P4 form filling and any genuinely JS-heavy
portal. That is a **per-portal** decision, not a global one.

Secrets live in `.env` only. Never hardcoded, never committed.

### §C5 — Politeness and rate limiting (non-negotiable)
- `robots.txt` is fetched, cached, and honored per host before any request. A
  disallowed path is skipped and logged, never fetched anyway.
- Minimum delay between requests to the same host, plus jitter (default 1.5s +
  0.75s). **Never zero this to go faster.**
- Bounded retries with exponential backoff. On 429 or 5xx, back off; `Retry-After`
  is respected. Do not hammer.
- A descriptive, honest, contactable User-Agent. Do not impersonate a browser to
  evade detection.
- Concurrency capped per host. **Scale breadth by adding companies, never by
  raising request rate against one host.**

### §C6 — Failure handling
A portal or company that fails must not crash the run: log it to `scrape_log` with
the error and continue. Per-portal and per-company failure counts are queryable, so
broken targets surface instead of silently returning zero jobs.

### §C7 — Deduplication
A job's identity is `(source, company_slug, external_id)`, enforced by a unique
constraint. A stable `content_hash` is also stored so an edited posting is detected
and updated in place instead of duplicating.

**The DB is the source of truth** for "have I seen this?" and later "have I applied
to this?" — never re-derive that from the Excel sheet. The sheet is a view you can
edit, not a database.

### §C8 — Resume tailoring: the hard rule
- Claude may only **rephrase, reorder, and re-emphasise** content already in the
  base resume. It must **never** invent employers, titles, dates, credentials,
  metrics, or skills the user does not have.
- Enforced twice: a prompt rule, and a post-hoc fabrication check that rejects the
  output. The guard shares one skill vocabulary with the scorer so the two cannot drift.
- Output must be ATS-safe `.docx`: no tables, columns, text boxes, headers/footers,
  or graphics. Employers, dates, and education are copied verbatim.
- A match score below `resume.min_score` flags for manual review rather than
  auto-proceeding — and the flag clears symmetrically if the job later clears the bar.

### §C9 — Applying: human in the loop
Most career portals' Terms of Service prohibit automated submission. **This project
does not click Submit.** P4 is recast as *prefill and hand over* (see the plan below).

- Every portal defaults to `require_manual_confirm: true`; `--dry-run` fills the
  form and stops before the final Submit.
- Open-ended and custom screening questions are **never** auto-submitted. Either you
  answer them, or Claude drafts an answer you explicitly approve.
- **CAPTCHAs are a stop condition**, not something to solve or evade.
- Every submission and dry run is logged with timestamp, screenshot, and the exact
  resume version used.
- A per-day application cap is enforced from `config.yaml`.

### §C10 — Cost control
Tailoring is a per-job Opus call and there is currently **no budget ceiling** — the
only brake is `resume.overwrite: false` skipping jobs that already have output. A
hard spend cap, a cost estimate before a run, and model tiering are part of P3B and
are not optional once the inventory grows past a few hundred jobs.

### §C11 — Version control
Under git since 2026-09-06, pushed to
`github.com/shyamsunderprogramer-design/AI_Assitant_Job_Applier` (**public**).

`.gitignore` keeps `.env`, `data/` (the SQLite DB and the tracker workbook),
`logs/`, and `resume/` output out of the repo — so no API key, no scraped job
data, and no resume is ever committed. **Check that before adding a file**: the
repo is public, and the DB in particular holds the full text of every posting.

`data/` being gitignored also means the database is *not* backed up by git. A
destructive DB change is unrecoverable; copy `data/jobs.db` before one.

---

## 6. Build plan

Each item is ticked only when it is **built, run, and verified** — not when the code
is merely written.

Legend: `[x]` done & verified · `[ ]` not started · `[~]` in progress · `[!]` blocked

**Phase numbers are IDs, not a schedule.** They are stable so code comments and
commit messages keep referring to the same thing. What to build next is the
execution order below, which is deliberately *not* 0,1,2,3,4.

### Execution order (revised 2026-09-06)

| # | Phase | Why here | Blocked? |
|---|---|---|---|
| ~~0~~ | ~~**git init** (§C11)~~ | ✅ Done 2026-09-06 — repo initialised and pushed. | — |
| ~~1~~ | ~~**P5 — Pipeline integrity**~~ | ✅ Done 2026-09-06 — closure live, 3 dead postings retired on the first run. | — |
| 2 | **P6 — Breadth** ← **next** | 10 boards / 105 jobs is a demo, not a job search. Biggest lever on outcomes, and Ashby is nearly free coverage. | No |
| 3 | **P7 — Daily loop** | Cheap glue that turns 5+ commands into one habit. Makes everything downstream actually get used. | No |
| 4 | **P3B — Live tailoring** | Fully built, never run. Unblocks the moment a resume + key exist. | Yes — you |
| 5 | **P8 — Relevance v2** | Only worth it once the funnel is wide. Re-ranking 105 jobs is pointless; re-ranking 3,000 is not. | Yes — key |
| 6 | **P4 — Assisted apply** | Highest risk, lowest reliability, ToS-constrained. Last on purpose, and recast from "auto-submit" to "prefill and hand over". | Yes — profile + go/no-go |
| 7 | **P9 — Outcome feedback** | Needs real applications before it has anything to learn from. | After P4 |

### Why the order changed

The original plan ran 0 → 1 → 2 → 3 → 4, which put the riskiest, least reliable,
most ToS-constrained work — auto-submission — at the finish line, and left
everything else waiting on two files only you can provide. Six problems with that:

1. **The whole plan was blocked on you**, while genuinely unblocked, high-value work
   (freshness, breadth, the daily loop) wasn't scheduled at all.
2. **Breadth was ticked as "discovery works"** — but working discovery and a *built
   inventory* are different things. The pipeline still runs against 10 companies.
3. **Postings never close.** The lifecycle columns exist and are dead code.
4. **Submission was framed as the goal.** It is the part most likely to burn an
   account, hit a CAPTCHA, and violate a ToS, for the smallest time saving. The real
   per-application time cost is screening questions and cover letters — neither of
   which the old plan addressed at all.
5. **Nothing closed the loop.** No application outcomes, so filters and thresholds
   could only ever be tuned by guessing.
6. **No cost ceiling and no version control** were both simply missing.

---

### Phase 0 — Foundation ✅

- [x] Project skeleton (`config/`, `db/`, `scraper/`, `excel/`, `resume/`, `submitter/`, `tests/`)
- [x] `requirements.txt` covering all phases
- [x] `.gitignore` (secrets, DB, logs, resumes, audit artifacts)
- [x] `.env.example` + `python-dotenv` loading; no hardcoded secrets
- [x] `config/config.yaml` as the single source of tunables
- [x] `config/loader.py` — dotted-path lookup, UA templating, logging setup
- [x] Constraints recorded (now §C1–§C11 above)
- [x] Virtualenv created, dependencies installed
- [x] Documentation written
- [x] `db/migrate.py` — additive-only column migration, run on every startup
- [x] **`git init` + first commit** (§C11) — pushed to GitHub 2026-09-06

### Phase 1 — Job Discovery ✅

Verified live: 10 companies, 3,110 postings seen, 105 matched, 0 duplicates on re-run.

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
- [ ] Narrow `filters.title_keywords` to your actual target roles — **needs your input**

### Phase 2 — Excel Tracking Sheet ✅

105 jobs exported live; re-export is a clean no-op.

- [x] `excel/tracker.py` module, driven off `exported_to_excel`
- [x] Columns: Company · Job Title · Application Link · JD · Location · Posting Date ·
      Required Skills · Date Found · Application Status · ATS Match Score
- [x] Create workbook if absent; append to it if present
- [x] Append-only semantics — never duplicate or clobber an existing row
- [x] Preserve your edits to Status when re-exporting
- [x] Update in place when a posting changed (`content_hash` moved) rather than re-appending
- [x] Application Link written as a real clickable hyperlink
- [x] JD truncated to a cell-safe length, full text still in the DB
- [x] Frozen header, column widths, autofilter, hidden `Job Key` join column
- [x] Conditional formatting on Status (7 states, not duplicated per export)
- [x] Data-validation dropdown for Status values
- [x] CLI: `export` (`--all` to re-export everything)
- [x] Unit tests: append idempotency, status preservation, formatting (14 tests)
- [x] Verified live against the real DB (105 jobs)

### Phase 3A — ATS Scoring ✅

Verified live on 105 real postings and calibrated.

- [x] `resume/parser.py` — structured text from .docx, .pdf, .txt/.md
- [x] Auto-detect the base resume in `resume/`, ignoring Word lock files
- [x] `resume/scorer.py` — weighted keyword overlap between resume and JD
- [x] Works standalone: no API key, no cost, unit-tested
- [x] Scores against the *requirements* section, not company blurb/benefits
- [x] Skills weighted above generic JD prose; company-name tokens excluded
- [x] Threshold calibrated on 105 real postings (max 47%, p90 31%, median 20%)
- [x] Score written back to the DB and into the Excel sheet
- [x] Below-threshold → "Manual Review"; the flag clears symmetrically
- [x] Excel respects user-set statuses while letting system flags through
- [x] CLI: `score`, `reparse`
- [x] Verified live: 105 jobs ranked, backend roles top, frontend bottom

### Phase 3B — Resume Tailoring ⚠️ built, never run

- [x] `resume/tailor.py` — Claude API call (`claude-opus-5`, adaptive thinking, streaming)
- [x] Anti-fabrication guard: prompt rule + post-hoc check for invented skills,
      metrics, dates, employers (§C8)
- [x] Guard shares one skill vocabulary with the scorer so they can't drift
- [x] Reject any tailored output that fails the fabrication check
- [x] `resume/writer.py` — ATS-safe .docx (no tables, columns, text boxes, graphics)
- [x] Employers, dates, education copied through verbatim — never rewritten
- [x] Review note (`*_review.txt`) listing rewrites, omissions, honest gaps
- [x] Deterministic output filenames tying a resume version to a job
- [x] Skip re-tailoring a job that already has an output file
- [x] CLI: `tailor [--job-id N] [--limit N]`
- [x] 42 offline unit tests for scorer, guard, parser, writer, pipeline
- [!] **Live tailoring run — blocked on you**: base resume in `resume/` + `ANTHROPIC_API_KEY`

**New — cost control (§C10). None of this exists today:**

- [ ] `resume.max_spend_per_run_usd` — hard stop *before* exceeding it, not after
- [ ] Log per-call input/output tokens and running cost to the DB
- [ ] `--estimate` — print projected cost for a selection and exit without calling
- [ ] Model tiering: draft on a cheaper model, escalate to Opus for the shortlist only
- [ ] Cache the JD→requirements analysis so re-tailoring is not re-paid for

**Done when:** a real tailored .docx exists for a real posting, the guard has been
seen both to pass *and* to reject, and a run's cost is known before it starts.

### Phase 5 — Pipeline Integrity ✅

**Verified live 2026-09-06.** The lifecycle columns had been sitting in the model
and the live DB with no code reading or writing them; every job had been
`is_open=True` since the day it was found. Now reconciled on every successful
scrape (`scraper/lifecycle.py`).

First real run: 3,172 postings seen across 10 boards, **3 vanished postings
closed** (1 Figma, 2 Instacart), 0 wrongly closed, and the median age of an open
job dropped from 83 to 74 days.

The load-bearing detail: **databricks and palantir matched 0 title filters that
run, and their 4 stored jobs correctly stayed open** — closure is judged against
every posting the board returned, not the filtered subset. Reconciling against the
filtered set instead would have closed 4 live jobs.

- [x] Runner advances `last_seen_at` for every job returned by a **successful** fetch
- [x] After a successful fetch, that company's jobs which were *not* returned get
      `is_open=False` + `closed_at`
- [x] A failed or partial fetch closes **nothing** — an outage must never look like a
      mass closure. Covered by an integration test that runs a failing board through
      the real runner
- [x] Closure is per-company, and only for companies actually scraped in that run
- [x] Reopening: a posting that reappears clears `closed_at` and goes open again
- [x] `score` and `tailor` skip closed jobs by default (`--include-closed` to override)
- [x] Excel: a `Closed` status flows through without clobbering a user-set status,
      using the precedence rule P2 already established
- [x] `stats` reports open vs closed and median posting age
- [x] Staleness guard: warn when a company hasn't been successfully scraped in N days
- [x] Unit tests: 19 new tests — closure on success, **no** closure on failure, reopen,
      status precedence, empty-board guard, cross-company isolation

**Two safety rules worth not undoing:**

- **An empty board closes nothing** (`limits.close_on_empty_board: false`). An
  empty 200 is indistinguishable from a board that broke, and being wrong closes
  every job for that company.
- **Only a successful fetch closes anything.** The runner reaches the reconciler
  only after the failure paths have already `continue`d.

**Also fixed here:** stored datetimes come back from SQLite **naive** while
`utcnow()` is **tz-aware**, so any comparison between them raises `TypeError`.
Every comparison now goes through `lifecycle.as_utc()`.

### Phase 6 — Breadth ❌ — **DO SECOND**

P1 proved discovery *works*; it was never *used*. The DB holds 10 companies. Stripe
alone had 580 postings and matched 6 — that ratio is what a real inventory has to
overcome by volume.

- [ ] **Ashby scraper** (`scraper/ashby.py`) — public posting API, same JSON shape as
      Greenhouse/Lever. One module for a large slice of the startup market the current
      two miss; the highest coverage-per-line available anywhere in this plan
- [ ] Wire Ashby into `discovery.probe_sources` and `portals`
- [ ] Source real seed name lists (YC companies, tech-scaleup lists) — per §C3,
      startups are the rich seed, Fortune-1000 names are not
- [ ] Discovery at scale: resumable, checkpointed, safe to Ctrl-C and restart
- [ ] Cache negative probes so re-runs don't re-hammer boards for known misses
- [ ] Record probe outcome + date per candidate slug (a board can appear later)
- [ ] `discover --dry-run` to see candidate slugs before spending requests
- [ ] Politeness at scale: breadth comes from more hosts, never a shorter delay (§C5)
- [ ] Make `max_companies_per_run` meaningful — rotate so every company is scraped
      over a few days rather than all of them every run
- [ ] Cross-company duplicate detection (the same role reposted under two slugs)

**Done when:** ≥300 verified boards in `companies`, a full scrape completes in a
reasonable window under the existing rate limits, and all three ATSs feed the tracker.

**To agree with you:** is 300 the right target? It's a starting number, not a researched one.

### Phase 7 — Daily Loop ❌ — **DO THIRD**

Today the workflow is five-plus commands run by hand, which in practice means it is
run once and then not again.

- [ ] `main.py daily` — scrape → close stale → score → export, one command
- [ ] Idempotent and safe to run twice in a day
- [ ] A digest of what changed: new matches, newly closed, top new scores
- [ ] `--since` so "what's new since yesterday" is answerable
- [ ] Digest to a file (and optionally email), not only stdout
- [ ] Meaningful exit codes so it can sit behind `cron`/`launchd`
- [ ] A partially-failed run still reports what succeeded
- [ ] Split the CLI into `cli/` — `main.py` is ~330 lines and grows a command per phase

**Done when:** one command, run daily, produces a digest you actually read.

### Phase 8 — Relevance v2 ❌

The current scorer is keyword overlap, and its own calibration is the argument for
this phase: on 105 postings the *best* match scored 47% and the median 20%. That is
a weak signal to rank on, and it cannot tell a must-have from a nice-to-have, or a
senior role from a new-grad one.

- [ ] Structured JD facts extracted once and stored: seniority, years of experience,
      visa/citizenship requirements, comp range, remote/hybrid/onsite
- [ ] Hard filters from those facts — a role demanding 10+ YOE shouldn't rank at all
- [ ] Must-have vs nice-to-have weighting within the requirements section
- [ ] LLM re-rank of only the top N (bounded, cheap) producing a fit verdict and a
      named gap list — not another opaque number
- [ ] `score --why <job-id>` showing which terms drove the score
- [ ] Keep the free keyword scorer as the wide first pass; the LLM only re-ranks
- [ ] Recalibrate `resume.min_score` against the new distribution, documented in
      `config.yaml` the way the current number is

**Done when:** the top 20 by score are ones you agree are the top 20 — and
disagreements are explainable.

### Phase 4 — Assisted Apply ❌ (recast from "Submission")

**Gate: your go/no-go on this recast before any code is written.**

The old framing was "automate the submission". Everything in the assessment below
argues against that, and §C9 already commits to human-in-the-loop. So the
deliverable changes: **assemble the application, prefill the form, hand the browser
over.** The final click is always yours. This keeps every real benefit — no
retyping, no re-uploading, no re-answering the same screening question — and drops
the ToS, CAPTCHA, and account-flag risk that made the original framing the
worst-ROI phase in the plan.

#### Reliability assessment (done — read before deciding where manual review stays)

| Portal | Automating the form | Verdict |
|---|---|---|
| **Greenhouse** | Most predictable. Stable, mostly static markup, consistent field names, plain file input for the resume. Embedded iframes on some company-hosted pages are the main annoyance. | **Confident** — worth automating fill + upload |
| **Lever** | Also predictable, simpler than Greenhouse. Fewer custom questions in practice. | **Confident** — same treatment |
| **Workday** | Hostile to automation: per-tenant subdomains, heavy JS, shadow DOM, mandatory account creation, email verification, multi-step wizards that lose state. Selectors differ per tenant, so "supporting Workday" means supporting each company separately. | **Not recommended** — track manually |
| **Custom / in-house** | Unbounded variety, no shared structure to code against. | **Not automatable generically** — flag for manual |

Cutting across all four: **CAPTCHAs, bot detection, and account flags are the real
failure mode, not selectors.**

- [x] Written reliability assessment (above)
- [ ] `config/profile.yaml` — name, contact, work history, EEO answers, links
- [ ] **Application packet** per job: tailored resume + cover letter + prefilled
      answers + link in one folder — useful even with zero browser automation
- [ ] Cover letter generation under the same anti-fabrication guard as the resume (§C8)
- [ ] **Screening-answer bank**: answers keyed by question similarity, reused across
      applications, always approved by you before first use
- [ ] `submitter/base.py` — shared fill/verify lifecycle
- [ ] Playwright session, **headed always** — you watch, you click
- [ ] Greenhouse filler (standard fields + resume upload), stops before Submit
- [ ] Lever filler (standard fields + resume upload), stops before Submit
- [ ] Custom/screening question detection — surfaced, never auto-answered
- [ ] CAPTCHA detection = hard stop, never an evasion attempt
- [ ] Daily cap enforced from `limits.max_applications_per_day`
- [ ] Audit log: timestamp, screenshot, exact resume version, field values
- [ ] `Application` table + status write-back to DB and Excel
- [ ] Verified on a real posting, hand-off point confirmed before any submit

**Explicitly out of scope:** clicking Submit unattended, solving CAPTCHAs, Workday,
custom portals.

### Phase 9 — Outcome Feedback ❌

Nothing currently learns from what happened. Without this, the filters, the
threshold, and the target company list are tuned by intuition forever.

- [ ] `Application` lifecycle: applied → response → screen → onsite → offer/reject
- [ ] Status changes read back from the Excel sheet you already edit
- [ ] Funnel stats: response rate by company, by title keyword, by score band
- [ ] **Does the score actually predict a response?** Report the correlation
      honestly, including if the answer is "no" — that result is what would justify P8
- [ ] Ghost detection: no response after N days → flag, stop counting as live
- [ ] Feed the evidence back: which title keywords and companies to drop
- [ ] `main.py report` — the one view of the whole search

**Done when:** you can answer "is this working, and where is it leaking?" with data
rather than a feeling.

---

## 7. What I need from you

| # | Item | Unblocks |
|---|---|---|
| 1 | **Base resume** (`.docx`/`.pdf`) into `resume/` — confirmed absent as of 2026-09-06 | P3B live run |
| 2 | **`ANTHROPIC_API_KEY`** in `.env` — no `.env` file exists yet | P3B, P8 |
| 3 | **Your actual target roles** — the filters are a broad SWE default | Signal quality, everywhere |
| 4 | **Profile data** — contact, work history, EEO answers | P4 |
| 5 | **Go/no-go on the P4 recast** — prefill-and-hand-over instead of auto-submit. This is a scope *reduction*; confirm it's the one you want | P4 |
| 6 | **Breadth target** — is 300 boards right? | P6 |

**None of items 1–6 block `git init`, P5, P6, or P7.** That is the point of the
reorder: there is roughly a week of high-value, fully unblocked work available right
now, and the old plan had all of it sitting behind you.
