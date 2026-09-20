# Job Applier Agent

Personal job-search automation for one person. Scrapes real ATS job boards, tracks
every posting in SQLite, mirrors them into an editable Excel sheet, scores each one
against your resume, and tailors that resume per job — without inventing a single
thing you haven't done.

**This file is the whole project's documentation: setup, decisions, and plan.**
Last verified against the code on **2026-09-20**.
It replaces the old `README.md` + `constraints.txt` + `PLAN.md` split (originals in
`.archive/`). Code comments cite the constraint sections as **§C1–§C10** below;
those numbers are stable, don't renumber them.

### Status at a glance

| Part | State | Evidence |
|---|---|---|
| **P0** Foundation | ✅ Done | venv, config, logging, additive migrations |
| **P1** Job discovery | ✅ Done, verified live | 2,867 boards · 90,311 postings seen in one run · 0 dupes on re-run |
| **P2** Excel tracker | ✅ Done, verified live | re-export is a no-op · your Status edits survive |
| **P3A** ATS scoring | ✅ Done, recalibrated | 2,509 postings scored; role fit added (see **P8**) |
| **P3B** Resume tailoring | ✅ Done, **runs free** | `gemma4:e4b` locally: 31s per resume, £0, guard clean |
| **Auto-search** | ✅ Done, verified live | Reads any resume, derives the search — no keywords to write |
| **P5** Pipeline integrity | ✅ Done, verified live | Closes vanished postings; a simulated outage closes nothing |
| **P6** Breadth | ✅ Done, verified live | 10 boards → **2,867**; Ashby, Workday and Workable added |
| **P7** Daily loop | ✅ Done | `main.py daily` — one command, one digest |
| **P8** Relevance v2 | ✅ Done, verified live | Role fit: 971 wrong-field postings fell below threshold, 0 remained |
| **P4** Assisted apply | ⚠️ Built, untested | Fills Greenhouse forms and stops at the CAPTCHA (§C9) |
| **P9** Outcome feedback | ❌ Not started | Needs real applications first |

**599 tests, all passing, all offline.** Under git (§C11).

**The model is free by default.** Tailoring and cover letters are the only part
that needs one, and a model on your own machine does it in about thirty seconds
at no cost. Paid APIs (Anthropic, OpenAI, xAI, OpenRouter) and a self-hosted
OmniRoute gateway are supported and entirely optional — see §C10. Nobody with a
chat subscription should have to buy API credit to use this.

### Contents

1. [Quick start](#1-quick-start) · 2. [Commands](#2-commands) · 3. [Configuration](#3-configuration)
· 4. [Layout](#4-layout) · 5. [Constraints & decisions](#5-constraints--decisions-c1c11)
· 6. [Build plan](#6-build-plan) · 7. [What I need from you](#7-what-i-need-from-you)

---

## 1. Quick start

**Once it is set up, this is the whole daily routine:**

```bash
.venv/bin/python main.py daily
```

That scrapes every board, retires postings that vanished, re-ranks against your
resume, updates the spreadsheet, and prints a digest of what changed. Everything
below is the setup and the individual pieces.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # discovery and scoring need no API key
```

**Then drop your resume — any resume — into `resume/`** and run:

```bash
.venv/bin/python main.py init-db     # create tables, load seed companies
.venv/bin/python main.py profile     # READ THE RESUME, WRITE THE SEARCH
.venv/bin/python main.py scrape      # scrape every active company
.venv/bin/python main.py export      # append new jobs to the Excel tracker
.venv/bin/python main.py score       # rank them against your resume — free, no API
```

`profile` is the step that means you never write a keyword list. It reads the
resume and derives the job titles to search for, the ones to exclude, your
seniority, and where you can work — then saves that to
`config/search_profile.yaml`, which the scraper reads in preference to
`config.yaml`. **The file is yours to edit**; regenerate with `profile --force`.

Only resume *tailoring* needs an `ANTHROPIC_API_KEY` in `.env`. Everything
above — discovery, ranking, tracking — is free and works offline of the API.

Two things worth understanding before you read a number out of this tool:

> **The score is a ranking, not a grade.** On 111 real postings against a real
> resume: 76% at best, 46% median. It measures coverage of a JD's salient terms,
> and no real resume approaches 100%. **Recalibrate `resume.min_score` after any
> big change to your resume or filters** — the original 0.25 came from a sample
> resume and let 110 of 111 jobs through once a real one was in place.

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
main.py discover --names data/seed_companies.txt  # name -> slug -> live ATS probe
main.py discover --names my_list.txt --dry-run   # show the cost, send nothing
main.py import-csv --file ats_companies.csv      # pre-built name,slug,source
main.py prune                                    # preview jobs that no longer match
main.py prune --apply                            # ...and remove them

# Tracking sheet
main.py export                           # append new jobs
main.py export --all                     # re-export everything

# The daily loop — everything below, in one command
main.py daily                            # scrape -> retire -> rank -> export -> digest
main.py daily --no-scrape                # re-rank what is stored, no network
main.py daily --since-hours 72           # widen what counts as "new"

# Reading your mailbox for companies that are hiring now
main.py scan-mail                        # read-only; prints setup if no credentials
main.py scan-mail --mbox export.mbox     # offline, from a Takeout export

# Scoring and tailoring
main.py score                            # free, no API key
main.py profile                          # derive the search from the resume
main.py profile --show                   # print it without saving
main.py profile --force                  # regenerate, overwriting your edits
main.py score --rescore --top 20
main.py score --include-closed           # closed postings are skipped by default
main.py tailor --estimate                # projected cost, sends nothing
main.py tailor --limit 3                 # needs ANTHROPIC_API_KEY
main.py reparse                          # re-derive requirements after a heuristic change
```

### Scaling the company list

There is no public directory of every company on Greenhouse/Lever/Ashby — each
runs its own board at a slug, so the name→slug map has to be **built, not fetched**
(§C3). `discover` derives candidate slugs from each name, asks each ATS whether that
board exists, and stores the hits; later `scrape` runs pick them up automatically.

`data/seed_companies.txt` ships 182 tech scale-up names as a starting point —
**148 of them were found** (81%). Add your own freely, one per line.

Discovery sends a request per *guess*, so two things protect the boards it asks
(§C5): every outcome is cached in `probe_log` and never re-requested, and
`--dry-run` prints the probe count and estimated runtime before anything is sent.
A long run is safe to interrupt — findings are written as they happen.

Fortune-1000 names have a low hit rate: big enterprises mostly use Workday, Taleo,
and iCIMS. **Startup and tech-scaleup lists are a much richer seed.**

### Keeping the list honest

Filters apply at scrape time, so narrowing a search leaves the jobs that matched
the *old* one sitting in the DB, still ranked and still recommended.
`main.py prune` re-applies the current search to everything stored, previews what
no longer matches, and removes it on `--apply` — including the sheet rows.
Anything you have acted on (Applied, Rejected, …) is never touched.

## 3. Configuration

Everything tunable lives in `config/config.yaml`. No behaviour is hardcoded.

| Section | What it controls |
|---|---|
| `database` | SQLite path |
| `excel` | tracker workbook path |
| `resume` | base resume path (null = auto-detect), output dir, `min_score`, overwrite, `max_spend_per_run_usd`, per-model `pricing` |
| `http` | robots.txt enforcement, per-host delay, jitter, retries, backoff, UA |
| `portals` | which ATSs are enabled (greenhouse, lever, ashby) |
| `companies` | seed list; whether to also scrape discovered companies |
| `filters` | JD keyword requirements; `use_derived_profile` (default true) makes the resume-derived search win over the hand-written lists here |
| `limits` | companies per run, applications/day (P4), auto-deactivation, posting-closure safety (`close_on_empty_board`), staleness warning (`stale_company_warn_days`) |
| `discovery` | which ATSs to probe, corporate suffixes to strip from names |
| `logging` | level and log file |

**You should not need to touch `filters.title_keywords`.** `main.py profile`
derives the search from your resume and writes `config/search_profile.yaml`,
which the scraper prefers. The lists in `config.yaml` are the fallback for when
no resume has been supplied. Set `filters.use_derived_profile: false` to force
the hand-written lists instead.

Location matching is word-boundary, so `"us"` matches `US-Remote` but not `Aarhus`.
Exclusions win over inclusions — that is what separates `Remote - USA` from
`Remote - India` when both match `remote`.

## 4. Layout

```
config/     config.yaml + loader (YAML + .env, logging setup)
db/         SQLAlchemy models (Company, Job, ScrapeLog), session, additive migrations
scraper/    http_client (robots + rate limiting), base, greenhouse, lever,
            ashby, filters, discovery (+ probe cache), runner,
            lifecycle (closure + staleness)
excel/      tracker.py — DB -> editable workbook, append-only
resume/     parser, profile (resume -> search), scorer, tailor, guard,
            cost (spend cap + ledger), writer, pipeline
            (+ your base resume, gitignored)
submitter/  submission + audit log — EMPTY, Phase 4 not started
tests/      260 offline unit tests
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
**In scope**, all public JSON APIs, one module each in `scraper/`:

| ATS | endpoint | boards held |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io` | 1,643 |
| Lever | `api.lever.co/v0/postings` | 618 |
| Ashby | `api.ashbyhq.com/posting-api` | 536 |
| Workday | `POST /wday/cxs/<tenant>/<site>/jobs` | 70 |
| Workable | `POST apply.workable.com/api/v1/accounts/<slug>/jobs` | added 2026-09-19 |

**Workable matters more than its board count suggests.** The first three are a
tech-sector habit: of the companies whose boards we can read, 43% are software,
IT, internet or marketing firms, and manufacturers, distributors and trucking
firms are almost absent. Measured against the full company list, only **0.021%**
of supply-chain-adjacent companies (1,030,301 of them) have a board on the
original three. Workable's customers skew smaller and much broader.

**SmartRecruiters is deliberately out**, despite having a clean public API:

    User-agent: LinkedInBot
    Allow: /v1/companies/
    User-agent: *
    Disallow: /

The API is open to one named crawler and closed to everyone else. Impersonating
that crawler is not something this project does. Workable, by contrast, opens it
explicitly — `Disallow:` empty, plus `Content-Signal: search=yes, ai-input=yes`
— which is why one is here and the other is not.

**Out of scope:** iCIMS, Taleo/Oracle, SuccessFactors, BambooHR, and custom
in-house sites.

**Rule:** one scraper module per **ATS type**, never per company. A company is a
tenant of an ATS, not a portal. Adding a company is a config/DB row, never code.

Workday moved **into** scope (`scraper/workday.py`): its public career-site API
turned out to be reachable without JS, and it is where the large employers are.
The rest of the "out of scope" list stayed out, and the reason is no longer only
effort — it is `robots.txt`. Checked 2026-09-14 against the campus ATS
directory in `universities/data`:

| ATS | schools | what `robots.txt` says to `User-agent: *` |
|---|---|---|
| NEOGOV (`schooljobs.com`, `governmentjobs.com`) | 94 | `Disallow: /` — search engines whitelisted, nobody else |
| PeopleAdmin (`*.peopleadmin.com`) | 50 | `Disallow: /`, with `/postings` opened to Twitterbot only |
| Paycom | 32 | `Disallow: /` but **allows** `/api/ats/job-postings/` — and then serves a block page to any client that asks |
| UKG / UltiPro | 15 | `Allow: */JobBoard/`, **`Disallow: */JobBoardView`** — the HTML page is open, the JSON listing behind it is not |
| ADP | 31 | `robots.txt` redirects to a login; no answer to read |

The JSON is real — UltiPro returns clean titles and city/state locations — and
it is a line this tool does not cross. `http.respect_robots` stays `true`
(§C5), which means these four platforms are unreachable to us regardless of how
much work we put in, and no amount of scraper code changes that. The honest
consequence is worth stating plainly: **~220 campus boards, the majority of the
higher-ed inventory, cannot be scraped by this tool.**

What is left for universities is what already works: **Workday and Greenhouse**,
which together carry 46 distinct campus boards and need no new code at all
(`universities/import_boards.py`).

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

**ATS-safe is not the same as unformatted**, and the first writer confused the
two. Measured on a real output: 145 paragraphs, every one styled `Normal`, with
43 bullets carrying a marker and 64 body lines carrying none — four bullets
under one heading with a marker on exactly one. Bullets were decided by whether
the *source* line still had its marker, and PDF extraction keeps it on some
lines and drops it from others. After the rewrite (2026-09-19): 186 of 186
bullets, all with hanging indents, and 5 real `Heading 1` sections.

The layout rules, each one costing a parser nothing:

- bullets decided **per section**, because a section either is a list or is not;
- a hanging indent, so wrapped text aligns under text rather than under the dot;
- real Heading styles — a fair number of parsers locate EXPERIENCE and EDUCATION
  by style rather than by reading the words;
- `keep_with_next` on headings and job titles, so neither is stranded at a page
  foot and no job splits from its first bullet;
- job dates on a right tab stop, since spaces break when the font changes and a
  table breaks the parser;
- one separator on the contact line, because resumes arrive with `|`, ` - ` and
  ` * ` mixed on one line after years of edits.

`tests/test_writer_layout.py` pins each of these to the defect it fixes.

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
Tailoring and cover letters are the **only** part of this tool that can spend
money, and by default they do not.

**Free is the default and the recommended route.** `resume.provider: ollama`
runs a model on your own machine: no key, no network, and the resume never
leaves the laptop. Measured 2026-09-19 on a real posting, `gemma4:e4b` produced
a clean tailoring in **31 seconds** and passed the fabrication guard. The job
page's primary button is still "copy the prompt", for a chat subscription you
are probably already paying for; the second button runs the local model. Neither
costs anything.

Two things worth knowing before choosing a local model:

- **Size matters more than cleverness.** `muse-glimmer` (18 GB) wrote well but
  used 69% of a 24 GB machine's memory, swapped to disk, and the second run
  never returned inside a 900-second timeout. `gemma4:e4b` (9.5 GB) fits, and
  does the same job in half a minute with 104 page-outs instead of 683,000.
- **A rejection is not always the model's fault.** Three guard false positives
  were found this way — `KPIs`, `S&OP` and `Functional` were all flagged as
  invented while printed verbatim in the resume. Check the resume before
  blaming the model.

**Paid providers are supported and entirely optional:** `anthropic`, `openai`,
`grok`, `openrouter`, and a self-hosted `omniroute` gateway. They are chosen on
the Settings page, which states plainly which of them charge. A provider needing
a key it has not got is never offered as a button that can only fail.

When a paid provider *is* selected, three brakes apply:

- `resume.max_spend_per_run_usd` (default **$2.00**) is checked *before* each
  call, from that call's projected cost. The run stops cleanly rather than
  discovering the cap by exceeding it. `0` disables.
- `tailor --estimate` prices a run from its real prompts and sends nothing.
- `resume.overwrite: false` skips jobs that already have output.

Every run reports what it actually spent. Prices live in `config.yaml` because a
hardcoded stale price produces a confidently wrong estimate — check the
provider's pricing page before a large run. A model the price table has never
heard of is costed at the **most expensive** model it knows, so an unpriced
name cannot quietly render the cap inert.

### §C11 — Version control
Under git since 2026-09-06, pushed to
`github.com/shyamsunderprogramer-design/AI_Assistant_Job_Applier` (**public**).

`.gitignore` keeps `.env`, `data/` (the SQLite DB and the tracker workbook),
`logs/`, `config/search_profile.yaml`, and **every resume document** out of the
repo — so no API key, no scraped job data, and no resume is ever committed.

That last one was a real near-miss: the pattern was `resume/base_resume.*`, so a
resume saved under the person's own name was **not** ignored. It is now matched
by extension (`resume/*.docx`, `*.pdf`, …). Nothing leaked — but check
`git status` before committing if you add a new file type there. **Check that before adding a file**: the
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
| ~~2~~ | ~~**P6 — Breadth**~~ ✅ Done 2026-09-11, extended since — 2,867 boards, 5 ATSs. | 10 boards / 105 jobs is a demo, not a job search. Biggest lever on outcomes, and Ashby is nearly free coverage. | No |
| ~~3~~ | ~~**P7 — Daily loop**~~ ✅ Done 2026-09-12. | Cheap glue that turns 5+ commands into one habit. Makes everything downstream actually get used. | No |
| ~~4~~ | ~~**P3B — Live tailoring**~~ ✅ Done 2026-09-19 — and it needed no key in the end: a local model does it in 31s for nothing. | — |
| ~~5~~ | ~~**P8 — Relevance v2**~~ ✅ Role fit done 2026-09-18; structured JD facts still open. | Partly |
| 6 | **P4 — Assisted apply** | Built, untested end to end. Blocked on `config/applicant.yaml`, which asks two legal declarations this tool will not guess. | Yes — fill the form |
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
- [x] Narrow the title filters to the user's actual roles — **solved by derivation
      rather than by asking**: `resume/profile.py` reads the resume and writes the
      search (2026-09-09)

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

### Phase 3B — Resume Tailoring ✅ built, run, and free

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

**Cost control (§C10) — built 2026-09-11:**

- [x] `resume.max_spend_per_run_usd` — the projected cost of each call is checked
      **before** it is sent, and the run stops cleanly when the next one would
      breach the cap. A cap discovered by exceeding it is not a cap
- [x] Per-call input/output tokens and USD recorded on a run ledger, reported at
      the end of every run — so the estimate can be checked against reality
- [x] Cached and cache-write tokens counted as input, so the ledger never
      under-reports
- [x] `tailor --estimate` — measures the **real** prompts and prints projected
      cost without sending anything
- [x] An unknown model prices at the most expensive known rate, never zero —
      pricing it at zero would make every estimate say "free" and the cap inert
- [x] Prices live in `config.yaml`, not hardcoded, so a stale price cannot
      silently produce a wrong estimate
- [x] 19 tests
- [ ] Model tiering: draft on a cheaper model, escalate to Opus for the shortlist
- [ ] Cache the JD→requirements analysis so re-tailoring is not re-paid for

Measured on the real backlog: **~$0.14 per job** with `claude-opus-5`, so the
full 80-job list is roughly $11 — which is exactly why the default cap is $2.

**Done when:** a real tailored .docx exists for a real posting, the guard has been
seen both to pass *and* to reject, and a run's cost is known before it starts.

### Auto-search — the resume writes the filters ✅

**Verified live 2026-09-09.** `filters.title_keywords` used to be a list the user
had to write *before they knew the answer*, so it stayed at its generic default
and searched for the wrong jobs. Against an 11-year SRE/DevOps resume it was
hunting "software engineer / backend / full stack" — and **excluding "staff" and
"principal"**, the two levels that person should most have been seeing.

`main.py profile` now reads the resume and derives the search: held titles,
skills, years of experience, seniority band, and location. Offline, deterministic,
no API key.

- [x] `resume/profile.py` — held titles, years, seniority, role families, locations
- [x] Title evidence weighted 10× body evidence — unweighted counting ranked
      "network engineer" (6 body mentions of *networking*) above SRE, the person's
      actual job title
- [x] Weak families dropped but **reported** as "considered", so they can be added back
- [x] Exclusions derived from seniority — no junior roles for a lead, no staff/principal
      exclusions for someone who should be seeing them
- [x] An exclusion may never cancel a search term (a self-defeating filter returns nothing)
- [x] A non-technical resume falls back to its own held titles rather than being
      handed a software engineer's search — narrow, but never wrong
- [x] Saved to `config/search_profile.yaml`, printed, and editable; `--force` regenerates
- [x] `scraper/filters.resolve_filter` prefers the derived profile over `config.yaml`
- [x] 22 unit tests, most covering cases where a wrong guess would be silent
- [ ] `refine_with_claude()` — the API-backed layer for careers the offline
      vocabulary does not cover (a nurse, an accountant). Hook exists, needs a key.

**Live result:** the derived search matched 6 of 3,184 postings across the same
10 boards — including **Staff Site Reliability Engineer** (Attentive),
**Staff+ SRE** (Anthropic), and **Staff/Senior Infrastructure Engineer**
(Coinbase). Every one of those would have been thrown away by the old filters.

The low match count is an honest signal, not a bug: these 10 seed companies are
product startups that hire mostly product engineers. It is the argument for P6.

**Also recalibrated:** `resume.min_score` was 0.25, derived from a throwaway
sample resume. Against the real one, 110 of 111 jobs cleared it and the "Manual
Review" flag stopped filtering anything. Real distribution is max 76%, p90 60%,
median 46% — threshold is now **0.60** (15 jobs). Recalibrate after any big
resume change with `score --rescore`.

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

### Phase 6 — Breadth ✅

**Verified live 2026-09-11.** P1 proved discovery *worked*; it had never been
*used*. The DB held 10 companies, which is why a correct SRE search matched only
6 of 3,184 postings — the search was right, the company list was a demo.

**Result: 10 boards → 149.** 148 companies found from 182 names (81% hit rate),
13,242 postings scanned, and the matched-job count went from 6 to 95.

- [x] **Ashby scraper** (`scraper/ashby.py`) — the third big startup ATS, and
      companies on it were entirely invisible before. **45 of the 149 found
      companies are Ashby-only** (Benchling, Cedar, Hims & Hers, …)
- [x] Ashby's payload is richer than the other two: salary is captured into the
      description, and remote-ness is folded into the location string — a fully
      remote role reads "New York, NY (HQ)" and a "remote" filter would drop it
- [x] `secondaryLocations` deliberately excluded from the filter string: a US
      role also open to "Remote (Canada)" would be killed by a "canada" exclusion
- [x] Wired into `portals`, `discovery.probe_sources`, and the CSV importer
- [x] `data/seed_companies.txt` — 182 tech scale-up names (§C3: startups, not
      the Fortune 1000)
- [x] **Probe cache** (`probe_log`): every (source, slug) outcome is remembered
      and never re-requested. A politeness feature as much as a speed one — the
      cheapest request is the one never sent (§C5)
- [x] Resumable: outcomes persist as they happen, so an interrupted run keeps
      everything its probes cost
- [x] `discover --dry-run` prints the probe count and runtime before spending it
- [x] Live progress while running, so a 18-minute run is not a blank screen
- [x] 16 discovery tests + 20 Ashby tests
- [ ] Rotate companies across runs so `max_companies_per_run` becomes meaningful
- [ ] Cross-company duplicate detection (one role reposted under two slugs)

**Three bugs this phase surfaced, all fixed:**

1. **robots.txt handling was backwards.** `http_client` treated a 401/403 on
   robots.txt as a blanket disallow *while citing RFC 9309 as the reason* — but
   §2.3.1.3 of that RFC says a 4xx means the file is UNAVAILABLE and the crawler
   may proceed, and Google's crawler documents the same. Python's stdlib
   `RobotFileParser` has the same non-compliant behaviour, which is likely where
   it came from. It made `api.ashbyhq.com` — a documented **public** job-board
   API — unscrapeable. Now standard-compliant, with
   `http.strict_robots_on_4xx: true` to restore the cautious reading. A real
   `Disallow` directive is honoured exactly as before, and a 5xx still disallows.

2. **Foreign roles reached the ranked list.** The location blocklist had 13
   entries, so "Sweden (Remote)", "Spain (Remote)", "The Netherlands | Remote",
   "Remote - European Union" and "Remote - UK" all matched the "remote" include
   term and named no blocked country. Now 149 places, consulted only when the
   resume's own contact line is US-based — and never excluding somewhere the
   person actually is.

3. **Narrowing a search left the old jobs behind.** Filters run at scrape time,
   so the DB had accumulated every job matching any filter generation ever used:
   126 stale postings, still ranked, still recommended. `main.py prune` fixes it,
   and also clears their sheet rows — which exposed that `openpyxl.delete_rows`
   leaves a deleted row's dimensions and styling behind, so removing 111 rows
   left 111 blank ones. The sheet body is rebuilt instead.

**Done when:** ≥300 verified boards. **Currently 149** — the seed list is the
limit, not the machinery. Feed `discover` a longer name list to go further.

### Phase 7 — Daily Loop ✅

**Built 2026-09-12.** Everything it runs already existed as a separate command,
and that was the problem: a five-command routine gets run once, enthusiastically,
and then never again. The tool only pays off if it runs daily, so the friction of
remembering the order was the thing most worth removing.

- [x] `main.py daily` — scrape → retire vanished postings → rank → export → digest
- [x] **A failing stage never aborts the run.** The scrape can die and yesterday's
      ranking still reaches you; each stage is caught, recorded, and the next runs
- [x] The digest leads with **what changed**, not with totals that look identical
      every morning — new roles with apply links, then what closed
- [x] `--since-hours` controls what counts as new; `--no-scrape` re-ranks offline
- [x] Digest written to `data/digest.txt` as well as stdout, because a cron run's
      stdout goes nowhere a human will look
- [x] Meaningful exit codes: `0` clean, `1` a stage failed, `2` nothing ran —
      so cron/launchd can act without a human reading the output
- [x] Stale-company and failed-board warnings surface in the digest
- [x] 16 tests, most of them about partial failure
- [ ] Email the digest rather than only writing it
- [ ] Split the CLI into `cli/` — `main.py` is past 700 lines

**To run it every morning at 8am**, add to `crontab -e`:

```
0 8 * * * cd /path/to/AI_Assistant_Job_Applier && .venv/bin/python main.py daily
```

**Done when:** one command, run daily, produces a digest you actually read.

### Phase 8 — Relevance v2 ✅ (role fit), remainder open

**Done 2026-09-18: the scorer now asks whether the job is the kind of job you
do**, not only whether you have the skills. It could not before, and the
measurement was stark — a data engineer's resume scored:

    97%  Senior Data Engineer        <- his field
    94%  Senior DevSecOps Engineer   <- somebody else's

Three points apart, because both descriptions are full of AWS, Python,
Kubernetes, Terraform and CI/CD, and coverage counts shared vocabulary.
`role_fit()` reads the posting's **title** against the titles the profile
searches for and the titles the person has actually held, then multiplies:
`WRONG_ROLE_FLOOR + (1 - WRONG_ROLE_FLOOR) * fit`, floor 0.45. A wrong-field
posting with genuinely shared skills is pushed down, never erased.

Verified on live data: of 971 software, data and devops postings sitting in a
supply-chain search, **all 971 fell below threshold and none remained**, while
buyers, planners and procurement analysts took the top of the list.

Two traps found by checking *both* directions, since widening must not become
"keep everything":

- ATS titles are full of punctuation that carries no meaning. "Engineer, Data
  Platform", "Engineer - Cloud Infrastructure" and "Engineer (Data Platform)"
  are the same job; matching now splits on non-word characters and pads, so
  `sre` is never found inside `Presenter`.
- A missing synonym reads as a wrong field. `ROLE_FAMILIES["devops"]` had no
  "site reliability" or "sre", so real SRE postings were being penalised. When a
  family looks over-strict, check its `searches` list before the scorer.

**Still open**, and unchanged by the above: structured JD facts, hard filters,
must-have weighting, and `score --why`. The keyword-overlap criticism below
still stands for everything except role fit.

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

### Phase 4 — Assisted Apply ⚠️ built, untested end to end

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
| ~~1~~ | ~~Base resume into `resume/`~~ — ✅ supplied 2026-09-06 | done |
| ~~3~~ | ~~Your actual target roles~~ — ✅ now derived from the resume automatically | done |
| 2 | **`ANTHROPIC_API_KEY`** in `.env` — no `.env` file exists yet | P3B, P8 |
| 4 | **Profile data** — contact, work history, EEO answers | P4 |
| 5 | **Go/no-go on the P4 recast** — prefill-and-hand-over instead of auto-submit. This is a scope *reduction*; confirm it's the one you want | P4 |
| 6 | **Breadth target** — is 300 boards right? | P6 |

**None of items 1–6 block `git init`, P5, P6, or P7.** That is the point of the
reorder: there is roughly a week of high-value, fully unblocked work available right
now, and the old plan had all of it sitting behind you.
