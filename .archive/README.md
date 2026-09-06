# Job Applier Agent

Personal job-search automation. Built in four phases; **Phase 1 (job discovery) is complete**.

Read `constraints.txt` before changing anything — it records the scope decisions,
politeness rules, and hard limits this project is built around.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in later; Phase 1 needs no API key
```

## Usage

```bash
# Phase 1 — discovery
.venv/bin/python main.py init-db     # create tables, load seed companies
.venv/bin/python main.py scrape      # scrape every active company
.venv/bin/python main.py stats       # counts + most recent finds
.venv/bin/python main.py failures    # which boards failed and why

# Phase 2 — tracking sheet
.venv/bin/python main.py export      # append new jobs to the Excel tracker
.venv/bin/python main.py export --all

# Phase 3 — scoring and tailoring
.venv/bin/python main.py score       # rank jobs vs your resume (free, no API)
.venv/bin/python main.py score --rescore --top 20
.venv/bin/python main.py tailor --limit 3    # needs ANTHROPIC_API_KEY
.venv/bin/python main.py reparse     # re-derive requirements after a heuristic change
```

### Phase 3 setup

1. Drop your base resume (`.docx` or `.pdf`) into `resume/` — it's auto-detected,
   and gitignored.
2. Put `ANTHROPIC_API_KEY` in `.env` (only needed for `tailor`, not `score`).

`score` costs nothing and needs no key: it ranks the whole backlog so you can see
where you actually stand before spending anything on tailoring.

**The score is a ranking, not a grade.** Calibrated on 105 real postings, a
strong-fit resume scored 47% at best and 20% at the median — it measures coverage
of a JD's salient terms, and no real resume approaches 100%. Tune
`resume.min_score` in `config.yaml` rather than reading it as a percentage grade.

**Nothing invents experience.** Claude may only rephrase, reorder, and re-emphasise
what your resume already says. Every tailored output is checked afterwards for
invented skills, metrics, dates, and employers; anything that fails is rejected and
flagged for manual review rather than written out.

### Scaling the company list

There is no public directory of every company on Greenhouse/Lever, so the
inventory is built by probing. Feed it any list of company names:

```bash
.venv/bin/python main.py discover --names my_companies.txt
.venv/bin/python main.py discover --names fortune1000.csv --limit 200
```

It derives candidate slugs from each name, asks both ATSs whether that board
exists, and stores the hits. Later `scrape` runs pick them up automatically.

You can also import a pre-built inventory CSV with `name,slug,source` columns:

```bash
.venv/bin/python main.py import-csv --file ats_companies.csv
```

Note: Fortune-1000 names have a low hit rate here — big enterprises mostly use
Workday/Taleo/iCIMS, not Greenhouse/Lever. Startup and tech-scaleup lists are a
much richer seed.

## Configuration

Everything tunable lives in `config/config.yaml`:

| Section | What it controls |
|---|---|
| `http` | robots.txt enforcement, per-host delay, jitter, retries, backoff |
| `portals` | which ATSs are enabled |
| `companies` | seed company list; whether to scrape discovered companies |
| `filters` | title keywords, exclusions, location, JD keyword requirements |
| `limits` | companies per run, applications/day (Phase 4), auto-deactivation |
| `discovery` | which ATSs to probe, corporate suffixes to strip from names |

**The role filters are currently a broad software-engineering default — narrow
`filters.title_keywords` to your actual targets.**

## Layout

```
config/     config.yaml + loader (YAML + .env, logging setup)
db/         SQLAlchemy models (Company, Job, ScrapeLog) and session handling
scraper/    http_client (robots + rate limiting), base, greenhouse, lever,
            filters, discovery, runner
tests/      offline unit tests for filters, parsers, hashing, slug derivation
data/       SQLite database (gitignored)
logs/       run logs (gitignored)
resume/     base resume + tailored output (Phase 3)
submitter/  submission + audit log (Phase 4)
```

## Why no Playwright in Phase 1

Greenhouse and Lever both publish stable JSON APIs that return every posting
with full description in a single request. Driving a headless browser at them
would be far slower, far more fragile, and much heavier on their servers for no
gain. Playwright stays in `requirements.txt` for Phase 4 form submission and for
any future JS-heavy portal (Workday, custom sites). See `constraints.txt` §4.

## Testing

```bash
.venv/bin/python -m pytest tests/ -q
```

All parser tests run offline against captured payload shapes — no network.

## Status

See `PLAN.md` for the itemised build plan and what is ticked off.

- **Phase 1 — Job discovery: done.** 10 companies, 3,110 postings scraped,
  105 matched filters, zero duplicates on re-run.
- **Phase 2 — Excel tracking: done.** 105 jobs exported; re-export is a no-op;
  user edits to Status are preserved, system flags still get through.
- **Phase 3 — Tailoring + scoring: code complete.** Scoring verified live and
  calibrated on 105 real postings. Tailoring needs your resume + API key to run.
- **Phase 4 — Submission: not started.** Reliability assessment written
  (`PLAN.md`): automate Greenhouse + Lever, keep Workday/custom manual.

77 tests, all passing, all offline.
