# Prompt for Claude Code (VS Code) — Automated Job Applier Agent

Copy everything below into Claude Code as your initial project prompt.

---

## Project: Automated Job Application Agent

I want to build a personal automation project (not a commercial product) that helps me apply
to jobs more efficiently. Set this up as a proper multi-module Python project with clean
separation between the phases below. Build it **incrementally, one phase at a time**, and pause
after each phase for me to test before moving to the next. Do not try to build everything in one shot.

### Tech preferences (adjust if you have a better suggestion, but explain why)
- Python 3.11+
- `playwright` for browser automation/scraping (more reliable against modern JS-heavy career
  sites than `selenium` or `requests`+`bs4` alone)
- `pandas` + `openpyxl` for the Excel output
- SQLite (via `sqlalchemy` or plain `sqlite3`) as a local database so I don't lose state between
  runs, don't apply to the same job twice, and can track status
- Anthropic API (Claude) for resume tailoring, JD parsing, and ATS-alignment scoring
- `python-dotenv` for API keys/config, never hardcoded
- `pytest` for basic tests on the parsing/scoring logic
- A `config.yaml` for things like target companies, career portal URLs, role keywords,
  application-per-day limits, and pacing/delay settings

---

### Phase 1 — Job Discovery (career-portal scraper)
Build a module that:
1. Takes a list of target company career-portal URLs (Greenhouse, Lever, Workday, or custom
   sites — start with Greenhouse and Lever since they have the most predictable structure)
2. Scrapes/polls each portal on a schedule for new postings matching my keyword/role filters
3. Extracts: job title, company, JD full text, application URL, location, posting date,
   required qualifications
4. De-duplicates against jobs already seen (store a hash or job ID in SQLite)
5. Respects each site's `robots.txt` and adds reasonable rate-limiting/delays between requests
   so we're not hammering any site
6. Logs failures per-portal so I know which sites the scraper can't handle yet (custom/non-
   standard career sites are expected to fail more often — flag them rather than crash)

Ask me clarifying questions about: which companies/portals I want to start with, and what
keyword/role filters to apply, before writing scraper code.

### Phase 2 — Excel Tracking Sheet
Build a module that:
1. Reads the newly discovered jobs from the database
2. Generates/updates an Excel workbook with columns: Company, Job Title, Application Link, JD
   (full text or summary), Location, Posting Date, Required Skills, Date Found, Application
   Status (default "Not Applied"), ATS Match Score (populated in Phase 3)
3. Appends new rows without duplicating or overwriting rows for jobs already tracked
4. Auto-formats (freeze header row, column widths, conditional formatting on status column)

### Phase 3 — Resume Tailoring + ATS Scoring
Build a module that:
1. Takes my base resume (I'll provide it as a `.docx` or `.pdf` — ask me for the format) and a
   specific JD
2. Uses the Claude API to rewrite/tailor resume bullet points to align with that JD's language
   and required skills — **without inventing experience I don't have**. It should only
   rephrase/reorder/emphasize real content from my base resume.
3. Outputs a clean, ATS-friendly `.docx` (no tables, columns, text boxes, or graphics that break
   ATS parsers)
4. Runs a keyword-overlap/match score between the tailored resume and the JD (simple TF-IDF or
   keyword-set overlap is fine to start — doesn't need to be fancy) and writes that score back
   into the Excel sheet
5. Flags any JD where the match score is below a threshold I set, so I can review those manually
   instead of relying on a low-confidence auto-tailor

### Phase 4 — Profile Creation & Submission
This is the highest-risk phase — most career portals' Terms of Service prohibit automated
bot submissions, and CAPTCHAs/account flags are common failure points. Build this as
**semi-automated, human-in-the-loop by default**:
1. Automate the mechanical parts: filling name/contact/work-history fields, uploading the
   tailored resume, filling standard fields from my profile data
2. For custom screening questions, either (a) flag them for me to answer manually, or (b) use
   the Claude API to draft an answer that I review and approve before submission — do not
   auto-submit unreviewed answers to open-ended questions
3. Add a `--dry-run` mode that fills everything but stops right before the final Submit click,
   so I can review the filled form first
4. Add a config flag `require_manual_confirm: true/false` per portal — default `true` for any
   portal we haven't tested extensively
5. Log every submission (or dry-run) with a timestamp, screenshot, and the exact resume version
   used, so I have a full audit trail

Before writing Phase 4 code, tell me explicitly which portals (Greenhouse, Lever, Workday,
custom) you're confident automating reliably vs. which ones are likely to break, so I can decide
where to keep manual review on.

---

### Project structure
Propose a clean folder structure (e.g. `scraper/`, `excel/`, `resume/`, `submitter/`, `db/`,
`config/`, `tests/`) before writing code, and confirm it with me.

### What I want from you right now
1. Confirm you understand the four phases above
2. Propose the project folder structure
3. Ask me any clarifying questions you need (target companies, resume file, keyword filters,
   how many applications/day, etc.)
4. Then scaffold Phase 1 only, and stop for my review before continuing
