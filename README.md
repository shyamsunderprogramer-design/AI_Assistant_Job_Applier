# AI_Assistant_Job_Applier

Personal job-search automation for one person. Scrapes real ATS job boards, tracks
every posting in SQLite, mirrors them into an editable Excel sheet, scores each one
against your resume, tailors that resume per job — without inventing a single
thing you haven't done — and fills in the application forms with you.

**This file is the whole project's documentation: setup, the pages, decisions, and plan.**
Last verified against the code on **2026-10-24**.
Auto Apply recovery verification on **2026-10-26**: legacy runs now always stop before Submit;
only **Apply to these** retains the restricted chosen-jobs automatic path. Current evidence,
remaining failures and acceptance are in [the recovery report](docs/phase3-recovery-verification.md).
The approved [Phase 3 history-durability repair](docs/phase3-history-durability-repair.md) now passes
the full regression suite, and it went live on **2026-10-29** ([activation report](docs/phase3-activation-2026-10-29.md)).
It replaces the old `README.md` + `constraints.txt` + `PLAN.md` split (originals in
`docs/archive/`). Code comments cite the constraint sections as **§C1–§C11** below;
those numbers are stable, don't renumber them.

### Status at a glance

| Part | State | Evidence |
|---|---|---|
| **P0** Foundation | ✅ Done | venv, config, logging, additive migrations |
| **P1** Job discovery | ✅ Done, verified live | 25,861 active boards on 11 ATSs · 13,692 open postings · 0 dupes on re-run |
| **P2** Excel tracker | ✅ Done, verified live | re-export is a no-op · your Status edits survive |
| **P3A** ATS scoring | ✅ Done, recalibrated | 13,686 open postings scored, 1,735 of them 70%+; role fit added (see **P8**) |
| **P3B** Resume tailoring | ✅ Done, **runs free** | Claude Code on the plan you already pay for, or a local model; an API key is optional (§C10) |
| **Auto-search** | ✅ Done, verified live | Reads any resume, derives the search — no keywords to write |
| **P5** Pipeline integrity | ✅ Done, verified live | Closes vanished postings; a simulated outage closes nothing |
| **P6** Breadth | ✅ Done, verified live | 10 boards → **25,861** on 11 ATSs, plus 12 job feeds and 5 job-alert senders read from your mailbox |
| **P7** Daily loop | ✅ Done | The **morning run** at 6 AM, with Start, Stop and an on/off switch on the Jobs page |
| **P8** Relevance v2 | ✅ Done, verified live | Role fit, plus each posting's experience, salary, workplace, job type and clearance / citizenship / visa rules as Jobs-page columns and filters |
| **P4** Assisted apply | ✅ Built, used on real applications | Greenhouse, Workday and JazzHR forms filled; any other posting followed to the employer's form. You press Submit unless you switch on *Submit by itself*, which sends Greenhouse forms only (§C9) |
| **Auto Apply v3.0** | ✅ Phases 1–3 done, verified; Phase 3 live since 29 Oct (technical PASS) | Your chosen jobs kept in the database, each with an eligibility decision, its documents made ready and checked, its questions asked first, any board (assisted), applied to one at a time, followed live and linked to its record; limits and risks in [docs/auto-apply-v3.md](docs/auto-apply-v3.md) |
| **P9** Outcome feedback | ⏳ Partly | The **Applied** page keeps each application's stages, and replies in your mailbox move them on. Funnel statistics not built |
| **P10** Resume Tailoring | ✅ Done, verified live | Requirement-by-requirement match, gaps, version checks, checked .docx + PDF — see [Resume Tailoring](#resume-tailoring) |
| **Web app** | ✅ Done | 11 pages and a page per job — see [The web app, page by page](#2-the-web-app-page-by-page). `python -m backend.api` on :8770 |
| **Watch list** | ✅ Done | Follow companies by name or board link, save jobs; both go first in Auto-apply |
| **Mac app** | ✅ Done | Double-click install, then a Welcome page with five steps ([docs/mac-install.md](docs/mac-install.md)) |
| **Board finder** | ⏳ Running | 378,840 of 1,361,080 company names probed (27.8%), 14,322 boards found, ~2,400 names/hr |

**1,869 tests: 1,868 passing, 1 live test skipped unless asked for; all offline.** Under git (§C11).

**The model is free by default.** Tailoring and cover letters are the only part
that needs one. A model on your own machine (Ollama) does it at no cost, and
**Claude Code** can be the writer on a Claude plan you already pay for — also no
extra cost (`RESUME_PROVIDER=claude-code`, or choose it in Settings). Paid APIs (Anthropic, OpenAI, xAI, OpenRouter) and a self-hosted
OmniRoute gateway are supported and entirely optional — see §C10. Nobody with a
chat subscription should have to buy API credit to use this.

### Contents

1. [Quick start](#1-quick-start) · 2. [The web app, page by page](#2-the-web-app-page-by-page)
· 3. [Commands](#3-commands) · 4. [Configuration](#4-configuration) · 5. [Layout](#5-layout)
· 6. [Constraints & decisions](#6-constraints--decisions-c1c11) · 7. [Build plan](#7-build-plan)
· 8. [What I need from you](#8-what-i-need-from-you)

---

## 1. Quick start

**On a Mac, without Terminal:** download `Job-Applier-mac-<version>.zip` from the Releases
page and double-click **Job Applier**: it installs itself and opens a Welcome page. See
[docs/mac-install.md](docs/mac-install.md). The rest of this section is for running it from a
clone.

**Once it is set up, the daily routine runs by itself.** The **morning run** starts at
6 AM (`devops/launchd/` → `devops/daily_pipeline.sh`) and waits for the Mac to be awake
and open. It scrapes every board, retires postings that vanished, re-ranks against
your resume, updates the spreadsheet, writes a digest of what changed, and drafts the
missing-skill suggestions for the top 10 jobs. On a scheduled morning it takes the
postings a GitHub cloud run already read, when there is one (`devops/cloud_results.py`).
A morning missed while the Mac was off starts when the app next sees it. It tailors
no resume and sends nothing. Its Start, Stop and 6 AM switch are on the Jobs page.
The same core, by hand:

```bash
.venv/bin/python main.py daily
```

Everything below is the setup and the individual pieces.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env          # discovery and scoring need no API key
```

**The web app** is where most of the work happens day to day:

```bash
.venv/bin/python -m backend.api        # then open http://localhost:8770
```

It lists the jobs ranked against your resume, and each job page walks one
application through: read the posting, tailor the resume, write the letter,
send it. Every page is described in [§2](#2-the-web-app-page-by-page). On macOS,
`devops/launchd/` keeps it running in the background. Templates are cached and
edited Python is not reloaded, so after changing either, restart it:

```bash
launchctl kickstart -k gui/$(id -u)/com.AI_Assistant_Job_Applier.webapp
```

A fetch, score or apply run started from a page runs in its own session and
carries on through the restart.

**If the page says "127.0.0.1 refused to connect"**, the app is not running. Look in
System Settings → General → Login Items & Extensions → *Allow in the Background*: macOS
lists the app's background jobs under **Python Software Foundation**, not under Job
Applier, and switching that off stops the web app. Switch it back on, then start it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.AI_Assistant_Job_Applier.webapp.plist
```

**Then drop your resume — any resume — into `ml/resume/`** and run:

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
`backend/config/search_profile.yaml`, which the scraper reads in preference to
`config.yaml`. **The file is yours to edit**; regenerate with `profile --force`.

Only resume *tailoring* and cover letters need a model, and the default ones
cost nothing (see §C10). Everything above — discovery, ranking, tracking — needs
no model at all.

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

## 2. The web app, page by page

Eleven pages, in the order of the menu, plus a page per job. For each: what it is
for, what you do there, and what happens when you press each button.

How they fit together: **Welcome** (the first time) → **Jobs** (find) → a job's page
or the **Watch list** (choose) → **Auto-apply** (apply, with **Questions** for what the
forms ask) → **Applied** (what happened next). **Health** says whether the runs
nobody watched worked.

### Jobs — `/`

Every open posting your search matched, ranked by fit against your resume.

1. **Morning run** — runs by itself at 6 AM (*Start by itself at 6 AM*). **Start now**
   runs it at any time; **Stop** ends it and keeps what it fetched. What it does is
   under [Quick start](#1-quick-start).
2. **Get latest postings** (`main.py scrape`) and **Re-score** (`main.py score --rescore`)
   run one step by hand. The status line under them follows whatever is running.
3. Narrow the list: search, the chips (Today · This week · Remote · 70%+ · Salary
   published · $150K+ · Within my experience · I'm eligible · Not yet applied ·
   ★ Watch list · Under 3 months), the clearance, citizenship, visa and board menus,
   and up to three *Filter by* rules on any column. Press a column heading to sort.
4. Press **★** on a row to save the job to the Watch list; press the row to open its page.
5. **Tick jobs to apply to them** (the box at the start of each row; the box in the heading
   ticks or unticks every job the filters show, drawn or not). The bar says how many are
   ticked. Ticks are kept by the app (`data/apply/chosen.json`), so they stay through filters,
   reloads and other pages until you untick them or press **Clear selection**. **Review and add
   to Auto-apply** checks each one by the rules every apply run keeps (below, under
   Auto-apply), beside the jobs already waiting on that list: *Goes on the next run*, *Later*
   (one a company each run, twelve hours apart, or today's limit), *On Auto-apply already*
   (with its state there; adding it again adds nothing), or *Can't go*, with the reason.
   **Add to Auto-apply** puts the ones that can go on the Auto-apply page's *Your chosen jobs*
   list, each once; the ones that can't stay ticked. Ticking and adding send nothing.

Also on the page: your resume (**Upload**, **Replace**, **Remove**; the whole search is
made from it, and the line beside it says what it searches for), a reminder while
*Your details* are unfinished, progress bars for the board finder and the careers-site
finder, and a pop-up when your mailbox has news about an application.

### A job's page — `/job/<id>`

One posting, and one application walked through.

1. **Read it.** Fit, posted age, workplace, job type (**Change** corrects it), experience
   asked, salary, board and location; the clearance, citizenship and visa tags (**Edit**),
   with *Not for you* when your profile rules the job out; *What they ask for*; the full
   posting. An aggregator's 30-word teaser gets a box to paste the real posting into.
   **Company's page · Add** keeps the link to this job on the company's own careers site, beside
   the job site's link (`employer_url`, `backend/core/joblinks.py`). This job's own posting
   is applied on: **Open the application form** goes there, and so does **Auto-apply** for a
   posting found on a job site such as Adzuna (a Greenhouse, Workday or JazzHR posting is still
   filled on its own form). A careers site's front page or search is kept as a link to look
   from and never taken for the form. The app reads which it is from the link (an id in it
   means one job); **It's the careers site** / **It's this job's posting** says otherwise.
   Only web links to a named site are kept (no scripts, files, this computer or private
   networks, or passwords in the link). Kept when the postings are fetched again and through
   the morning's cloud merge.
2. **Make a folder for this application.** It keeps the exact resume and letter you sent.
3. **Tailor your resume.** **Copy the prompt** for any chat (free), or the writer button,
   which runs the model chosen in Settings (five to ten minutes). Then: the tailored
   resume's score (**Re-check**), its ATS match before → after, **Where did you use …?**
   to add a tool you really used, and **Find missing skills**.
4. **Write the cover letter**, the same two ways. **Edit it here**, download it as PDF
   or DOCX; earlier versions are kept.
5. **Send it**: **Auto-apply** (below), or **Open the form** yourself; then set the status
   and **Save**. Recording it is what stops the role coming back round.

**Auto-apply** opens the app's own browser at the posting, follows it to the employer's
form when the posting is on a listing site, and fills everything your profile and saved
answers cover, with this job's resume and letter. Then it waits for you to check it and
press Submit. A Greenhouse form with nothing left for you goes by itself when *Submit by
itself* is on. Lever's CAPTCHA turns the app's browser away, so a Lever form opens in
your own Chrome with every answer listed to copy. When a site answers Submit in words
the app does not know, the page asks **Did it go?**

Once you have applied, the top of the page shows **Your application**: its stage,
**Update** for what happened next, and **Mark the application closed** once the employer
takes the posting down. **Interview prep** (Applied or Interviewing) writes likely
questions, STAR answers built from your resume, topics to study with links, and a PDF.

**☆ Watch** keeps the job on your Watch list.

### Watch list — `/watchlist`

Companies you follow, and jobs you saved.

- **Companies.** Type a name, or paste a link to its job board, and press **Watch**. The
  app finds its careers page and the board behind it (a minute or two), adds the board
  to the morning fetch and reads it straight away. Each company lists its open roles
  that fit your search, best fit first; roles that arrived since you last looked are
  marked **New** until **Mark seen**. **Find its careers page** or **Look again** when no
  board was found; **Remove** stops watching.
- **Saved jobs.** Every job you starred, open or closed, with the closing date when the
  posting names one. **★** takes one off.

Both feed Auto-apply: saved jobs go first in *Apply to next 10*, roles at watched
companies second.

### Auto-apply — `/apply`

Where applications are sent from, three ways.

**Your chosen jobs — Apply to these.** The jobs you ticked on the Jobs page and added here,
kept in the database (`backend/apply/execution.py`, tables `apply_items`, `apply_runs`,
`apply_events`), so a crash or a restart of the app or the Mac never loses where each stands.
Each shows its state, what is happening, what it needs from you, and how it ended:

*Queued → Validating → Preparing → Ready → Running → Waiting for you → Applied*, or *Failed*,
*Skipped*, *Cancelled*, or *Pending confirmation* when it may have gone and the app cannot
tell. Only the allowed moves are made, and every one is in the **Live activity** list.

- **Apply to these** (or **Resume**) goes through exactly these, never the app's own picks:
  best fit first, one a company, twelve hours apart, up to today's limit. Each job is checked
  again just before it goes, and again before its form opens. **Pause** stops before the next
  form; **Cancel** takes one off before it goes; **Cancel waiting** and **Clear finished** work
  on the whole list.
- **Never twice:** one live entry per job and one live run, each held by a unique index, so
  two processes starting or adding at the same moment cannot both succeed; a job is taken by
  a conditional update, and every move names the state it expects, so a move made meanwhile
  (your Cancel, your answer, a recovery) is the one that stands. An application is recorded
  once, whoever says it went: the date it went and a later stage (Interviewing, Offer,
  Rejected) are never overwritten, and it has one line in `data/applications.jsonl`. A job
  that may have gone (no confirmation in time, the app stopping with its form open, a form for
  your own browser) waits as *Pending confirmation* until you press **Yes, it went** or **No,
  it wasn't sent**; **Retry** is refused until then.
- **Cancel never stops a form that is open:** it works until the form opens (a job cancelled
  while its documents are made, or the moment before its form opens, is never opened); with
  its form open it is refused, with the reason, and the run records how the form ended. A job
  applied to another way meanwhile (its own page, an employer's email) becomes *Applied*.
- **After a crash or restart** (checked when the app starts and when the page loads): jobs
  whose form had not opened go back in the queue; jobs whose form was open become *Pending
  confirmation*. A run still going is left alone, even one that started a moment ago, and a
  job cancelled meanwhile stays cancelled. An older database gains the tables and the guards
  at the app's next start without losing a row (`data_engineering/db/migrate.py`: a run left
  live beside another is marked interrupted, a job on the list twice keeps its first entry).
- Refused while another apply run's browser is open, before your details are filled in, and
  when none can go now. *Submit by itself* is not changed by any of this.
- **Live (Phase 2, 25 Oct 2026):** the page is sent each step as it is written, by server-sent
  events (`GET /apply/chosen/stream`): every move, every line a run writes (*Tailoring the
  resume…*, *Opening the Workday form*, *Filled 14 of 16 fields*), the run's own news. Each job
  shows the step it is at and for how long, what it needs from you, and how it ended; **Steps**
  opens its whole history, to the second (`GET /apply/chosen/events?item=`). The card says
  *Live*, *Reconnecting…* or *Checking every few seconds*. A reconnect (a refresh, a dropped
  connection, the app restarting, the laptop reopened) resumes from the last step it had, by
  the browser's own Last-Event-ID, so nothing is missed or shown twice; every job carries the
  id of its latest step, so an older picture never replaces a newer one. Without the stream the
  page looks every 2 seconds while anything moves (20 when nothing does) and tries the stream
  again a minute later. Applied is shown only from the queue's own state: a form's "Thank you"
  is a step, not a confirmation. Tests: `qa/tests/test_live_progress.py`.

**Next up — Apply to next 10.** The list shows the ten it will apply to, in this order:
jobs you saved, then roles at companies you watch, then your best fits (higher fit
first; at the same fit, the fresher posting). Each row says why it was picked, its age
and board, and whether its resume is tailored yet or will be when it goes. Saved jobs it
cannot take are listed under it, with the reason. The rules:

- Greenhouse, Workday and JazzHR postings only: the boards it can fill. A saved job on
  any other board is left for **Auto-apply** on its own page.
- Status *Not Applied* or *Manual Review*, and fit at least `resume.min_score` (60%),
  except for a saved job.
- Posted within `apply.max_age_months` (4 months), in a place your search names: never abroad.
- One job per company, and not the same company again within 12 hours.
- No more a day than `limits.max_applications_per_day` (15) or your profile's own
  limit (10), whichever is lower.

Pressing it opens the app's browser and works down the list: the resume (tailored now
if it is not yet), a cover letter if there is none, then the form. Every form in this legacy run
waits up to 15 minutes for you to press Submit, even with *Submit by itself* on; close its tab to skip it. Only a
confirmation page counts as Applied.

**Ready to apply — Prepare now, then Start applying.** *Prepare now* gets your best
matches ready first: jobs you saved, then the rest at 80%+ fit (`apply.auto.min_fit`),
up to 10, one per company, none older than 4 months. For each it makes the folder,
tailors the resume, writes the cover letter and lists the form's questions no saved
answer covers. Answer those on **Questions**; **Start applying** then goes through the
ready list the same way. **Skip** drops one, and a note marks any that Start applying
will leave out (too old, or abroad).

No resume is tailored before you press one of these, or Auto-apply on a job (your
choice, 21 Oct 2026).

### Applied — `/applications`

Every application, newest first.

1. The counts: applied, waiting, interviewing, offers, rejected.
2. **Email updates.** Your mailbox is read every half hour while the app runs (**Check
   email now** reads it at once). A rejection, a closed position, an interview, an
   assessment or an offer moves the application on; an employer's "thank you for
   applying" marks the job Applied. Read-only: nothing in the mailbox changes. Needs
   `MAIL_ADDRESS` and `MAIL_APP_PASSWORD` in `.env`.
3. Search, or filter by stage, and **Open →** one: its stages (add what happened next,
   with a date and a note), what you sent (resume, cover letter, the filled form, the
   thank-you page), the answers given, and the posting as it was the day you applied,
   kept after it is taken down.

### Questions — `/apply/questions`

What application forms ask that your profile does not answer.

1. **Submit by itself.** Off unless you switch it on (also in Settings → Your details).
   It applies only to **Apply to these**, for an **Eligible** job on Greenhouse's own board,
   after checking its documents and required answers, with no CAPTCHA puzzle or legal question
   left for you. Warnings, unverified jobs, other boards and all legacy runs wait for you.
2. **Your written answers.** Long answers you typed on forms, kept with the question,
   the company and the date. A later draft starts from them and never names the other
   company. **Copy** or **Delete**.
3. **Answer once, used on every application.** The same question in each employer's own
   words, grouped: one answer fills them all.
4. **Applications.** Each ready application and what it still needs; a question several
   employers ask is shown once. **Save answers.** Desired salary is answered for you:
   80–95% of the posted maximum, or of the market's when none is posted.

### Tailor — `/tailor`

Resume Tailoring for any job description, including one that is not in the list. How
it works is under [Resume Tailoring](#resume-tailoring).

1. Your saved resume is used unless you choose another; paste the job description or
   upload it as a file.
2. Choose the length, template, font and colour, and press **Create tailored resume**
   (five to ten minutes; each stage shows as it goes).
3. The result: the estimated match, **Download DOCX** / **PDF**, and the tabs: Tailored
   Resume (preview it, **Edit the text**), Recruiter Review, Keywords, Match Report,
   Alignment Summary, Version Checks, Changes Made, Gaps, Skill Roadmap.

A job page's **Tailor ↗** opens this page with that posting filled in.

### Resume — `/resume/build`, `/resume/check`, `/resume/history`

Three tabs for the resume itself.

- **Build.** Start from your master resume, another file or a blank page; it saves as
  you type. Claude turns rough notes into bullets only from what you wrote, and flags a
  number or tool you did not give. Template, font and colour; **Preview**; download PDF
  or DOCX; **Make this my master resume** (every tailoring starts from the master, and
  the old one is kept).
- **Check.** A score out of 100 with no posting (contact details, sections a parser can
  find, bullets that show results, dates, length, layout) and *What to fix*, filtered by
  problems, warnings and tips. A file checked here is read, checked and deleted.
- **Tailored versions.** Every tailored resume, newest first: search, sort, **Compare the
  two ticked** side by side, **Delete ticked** (never the one an application attached).

### Skills — `/skills`

What your best-matching jobs ask for. Nothing here changes your resume.

1. Choose how many jobs to read (the best 100, 300 or 600, or all scored) and **Scan again**.
2. **To learn next:** the skills those jobs ask for that your resume does not show, ranked
   by how many ask. Each opens a roadmap: the closest thing you have, a plan, a project,
   and what to say in an interview today.
3. **Lead with these:** your skills that most of those jobs want.

### Assistant — `/assistant`

A chat about your jobs, resumes and applications, on your Claude Code sign-in (no API
key). It can look things up and **propose** changes, such as tailoring a resume, applying,
setting a status or remembering an answer, and nothing happens until you press
**Confirm**. **New conversation** starts afresh.

### Health — `/health`

Whether the unattended runs worked, over the last seven days. First what needs you;
then new jobs found, open, closed, applied, ready to apply, the last backup and the free
space; each morning run's result and how long it took; scraping by day (boards read,
failures, new jobs, boards that turned the app away for asking too often); boards
failing three or more times; your applications by status. Opening the page brings it
up to date; **Make a fresh report** rebuilds it.

### Settings — `/setup`, `/applicant`

What this public repo deliberately does not carry, kept on this Mac.

- **Job-site account.** One email and password for Workday-style sites, kept in the Mac's
  Keychain only. Auto-apply signs in with it, or fills in a new account for you to agree
  to and create.
- **Sign in with the chat plan you already have.** Claude, ChatGPT or Gemini write your
  resumes on your own plan.
- **Which model writes your resume.** The plans above, a local model, or a paid key
  (marked *costs money*).
- **Keys**, saved to `.env`: model API keys, the job-feed keys (Adzuna, USAJOBS, Jooble,
  Careerjet, Findwork), the mailbox, the contact address the app gives site owners, the
  cloud results passphrase and the database location. A saved secret is never shown again.
- **Your details** (`/applicant`): name, contact, address, links, current job; work
  authorisation and sponsorship (never guessed: left blank, the form stops for you);
  citizenship, green card and clearance (these drive *I'm eligible*); and **Submitting**,
  the same *Submit by itself* switch.

### Welcome — `/welcome`

A new install's five steps: your resume → what to look for (made from the resume; the
titles, places and words to skip can be changed) → your details → who writes your
tailored resumes → find your jobs. The first fetch takes about an hour; after that the
morning run does it.

## 3. Commands

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
main.py prune --location-only --apply            # only the ones outside the US
main.py import-names                             # filter a big company export into a name list
main.py careers                                  # find each company's careers page and its board

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
main.py tailor --limit 3                 # with the model chosen in Settings
main.py tailor --job-id N                # one job, aiming for the ATS target
main.py brief N                          # a tailoring prompt for job N, to paste into any chat (free)
main.py letter N                         # the same for a cover letter (--write: write it with the model)
main.py accept N --file reply.json       # read the chat's reply back in, guard it, write the resume
main.py packet N                         # job N's folder: resume, letter, job summary
main.py suggest --top 10                 # missing-skill suggestions for the best jobs
main.py reparse                          # re-derive requirements after a heuristic change
main.py job-types                        # read each posting's job type again (no network)

# More postings: job feeds searched by title
main.py feeds                            # keyless feeds always, keyed ones once their key is in .env
main.py feeds --only remotive,themuse --dry-run
main.py adzuna                           # Adzuna's index (free key)
main.py signup-keys                      # guided sign-up for the feed keys; it saves each one

# Applying (§C9) — the Auto-apply page runs these
main.py prepare-apply                    # Prepare now: tailor, letters, questions for the top matches
main.py apply --limit 10                 # Apply to next 10, in the app's own browser
main.py apply --prepared                 # Start applying: the jobs Prepare now made ready
main.py apply --chosen                   # Apply to these: the jobs you ticked and added to Auto-apply
main.py apply --job-id N                 # one job, as its page's Auto-apply does
main.py setup-helper                     # open the apply browser once, to set up its autofill extension

# Background jobs
main.py status --watch                   # progress of the long-running ones

# The long-running board finder (walks the whole company list, resumable)
python -m devops.probe_runner            # start or resume
python -m devops.probe_runner --status   # where it is
python -m devops.probe_runner --stop     # stop after the current batch
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

### Resume Tailoring

Open it from the header's **Tailor** button, or press **Or let Claude write it**
on any job page, which runs it for that job with your saved resume and shows the
result right under the button. A run takes about five to ten minutes.

1. **Reading** — DOCX in reading order (tables, lists, links, headers, footers)
   or PDF page by page; a scanned PDF is read with OCR and marked low-confidence.
2. **Resume information** — sections, each job with its own bullets, every fact
   traceable to the line it came from.
3. **Requirements** — each one from the posting's own words, marked required or
   preferred, compound items split, synonyms joined (`k8s` = Kubernetes).
4. **Matching** — each requirement is **Direct**, **Related**, **Unclear** or
   **Not found**, with the resume line that shows it. A tool only in the skills
   list is Unclear; a similar technology is Related, with the reason.
5. **The draft** — rewritten from the resume's own facts only; no invented tool,
   number, title or date (§C8). Job titles are kept as written.
6. **Checking** — the .docx and PDF are drawn page by page and checked for
   clipped or overlapping text, broken bullets, stranded headings and anything
   missing; a problem is fixed and checked again before the result is shown.

**The score** is `sum(weight × value) / total weight`, with required items
weighing 3 and preferred 1, Direct counting 1.0 and Related 0.5. It is the
tool's estimate, shown with its calculation — not an employer's ATS result.

**Version checks** compare versions named in the resume with release dates from
[endoflife.date](https://endoflife.date) (cached 30 days), plus any you enter in
`ml/tailoring/release_dates.json`. A date that cannot be confirmed is *Unknown*,
never guessed.

Results tabs: Tailored Resume (editable, with downloads), Match Report,
Alignment Summary, Version Checks, Changes Made, Gaps. The uploaded file is never
changed. With Microsoft Word installed, Word also lays out the .docx so its pages
are checked the way Word shows them (the web app needs Word to be granted access
to `data/tailoring/word` once).

**A teaser is not a posting.** Aggregators such as Jooble often carry 30–40 words.
Those jobs show a *Paste the full posting* box, and tailoring waits for the real
text rather than spending minutes on a paragraph.

## 4. Configuration

Everything tunable lives in `backend/config/config.yaml`. No behaviour is hardcoded.

| Section | What it controls |
|---|---|
| `database` | SQLite path |
| `excel` | tracker workbook path |
| `resume` | base resume path (null = auto-detect), output dir, `min_score`, overwrite, `max_spend_per_run_usd`, per-model `pricing` |
| `http` | robots.txt enforcement, per-host delay, jitter, retries, backoff, UA; `host_delays` for hosts that need a longer gap (Workable: 4s) |
| `portals` | per-ATS switches (greenhouse, lever, ashby, workday) |
| `companies` | seed list; whether to also scrape discovered companies |
| `filters` | JD keyword requirements; `use_derived_profile` (default true) makes the resume-derived search win over the hand-written lists here |
| `limits` | companies per run, applications/day (P4), auto-deactivation, posting-closure safety (`close_on_empty_board`), staleness warning (`stale_company_warn_days`) |
| `discovery` | which ATSs to probe, corporate suffixes to strip; `probe_delay_seconds` and `name_workers` set the board finder's pace (0.5s, 6 names at a time) |
| `tailoring` | `use_word`: also lay the .docx out in Microsoft Word to check its pages (off) |
| `logging` | level and log file |
| `adzuna` | titles, pages, freshness and call cap for Adzuna's index (runs only with its key) |
| `feeds` | freshness, calls per keyed feed, when a feed's posting is retired (21 days), `disabled`, and each feed's titles or categories |
| `apply` | `max_age_months` (4: the oldest posting the batch runs pick), `auto.min_fit` (0.80) and `auto.per_day` (10) for Prepare now, the autofill helper extension; `wait_minutes` (default 15) is how long a form waits for you |

Your own answers live in `backend/config/applicant.yaml` (gitignored), which the
*Your details* page writes. Its `apply` part holds `stop_before_submit` (true unless
you switch on *Submit by itself*) and `max_per_day` (10).

**You should not need to touch `filters.title_keywords`.** `main.py profile`
derives the search from your resume and writes `backend/config/search_profile.yaml`,
which the scraper prefers. The lists in `config.yaml` are the fallback for when
no resume has been supplied. Set `filters.use_derived_profile: false` to force
the hand-written lists instead.

Location matching is word-boundary, so `"us"` matches `US-Remote` but not `Aarhus`.
Exclusions win over inclusions — that is what separates `Remote - USA` from
`Remote - India` when both match `remote`.

## 5. Layout

Organised by the software-engineering role that owns each part.

```
main.py               the CLI entrypoint (stays at the root)

frontend/             UI engineer
  templates/          Flask/Jinja pages (§2): index (Jobs), job, watchlist, apply
                      (Auto-apply), applications + application (Applied), questions,
                      tailor, resume_build / resume_check / resume_history, skills,
                      assistant, health, setup + applicant (Settings), welcome
  static/             app.css, theme.css (light and dark)
  console.html        the status console served by devops/jobstatus.py

backend/              backend engineer
  api/                the Flask web app — python -m backend.api: app.py, plus one
                      module per page group (auto_apply, watch_pages, tracker_pages,
                      resume_pages, morning_pages, inbox_pages, welcome_pages,
                      written_pages, chosen_pages)
  apply/              applying: queue (who goes next), runner (the browser run),
                      prepare (Prepare now), chosen (jobs ticked on the Jobs page and
                      added to Auto-apply), execution (their queue: states, runs,
                      recovery), profile (your details), form fillers for
                      greenhouse, workday, jazzhr and anyform (any other form),
                      answers, general, essays, written (saved answers), salary,
                      accounts (Keychain), inbox (email updates), tracker (stages)
  core/               shared job logic: jobage, jobfields, locations, eligibility,
                      watchlist, status, daily, setup_state, key_signup
  config/             config.yaml + loader (YAML + .env, logging setup),
                      boards.csv, applicant.yaml.example

data_engineering/     data engineer
  scraper/            http_client (robots + rate limiting), base, greenhouse, lever,
                      ashby, workday, workable, recruitee, teamtailor, bamboohr,
                      breezy, jazzhr, jobvite,
                      filters, discovery, runner, lifecycle (closure + staleness),
                      mailbox, job_alerts + alert_parsers + alert_import (job-alert
                      emails), company_import, adzuna
    feeds/            job feeds searched by title: usajobs, jooble, careerjet,
                      findwork, themuse, remotive, remoteok, himalayas, jobicy,
                      hn_hiring, weworkremotely, workingnomads — keyless ones run daily, keyed ones once their
                      key is in .env (python main.py feeds)
  companies/          the worldwide company inventory (import, enrich, refine)
  universities/       university career boards
  db/                 SQLAlchemy models, session, additive migrations
  excel/              tracker.py — DB -> editable workbook, append-only
  boards.py           boards.csv <-> database

ml/                   AI / ML engineer
  resume/             parser, profile (resume -> search), scorer, fit, titles, tailor,
                      guard, letter, letter_edit, llm, plans (chat-plan sign-in), cost,
                      writer, pdf, packet, pipeline, typeset, style, ats, recheck,
                      additions ("I've used this"), suggest (a job's missing skills),
                      skills (reading a skills section), builder (Resume Build),
                      health (Resume Check), progress
                      (+ your base resume and tailored output, gitignored)
  tailoring/          Resume Tailoring: reader (DOCX/PDF/OCR), structure,
                      requirements, synonyms, matching, keywords, versions, draft,
                      rounds, review, roadmap, market (the Skills page), history,
                      documents (render + page checks), pipeline
  benchmark_resume.py tailoring benchmark

devops/               DevOps / SRE
  launchd/            macOS scheduled jobs (daily, discover, enrich, backup, health, webapp)
  daily_pipeline.sh   the morning run: lock, wait for the Mac, daily, board export,
                      suggestions; also the discover, enrich, backup and health jobs
  probe_runner.py     the long-running board prober
  health_report.py    the weekly report behind the Health page
  backup_db.py        nightly copy of jobs.db and your settings
  cloud_results.py    the GitHub run's postings brought in, and the board list sent
                      to it, both encrypted with RESULTS_PASSPHRASE
  mac/                the Mac app: installer, Job Applier.applescript, build_app.sh
  jobstatus.py, pushstatus.py, build_dashboard.py, run_summary.py,
  actions_summary.py, merge_cloud.py, fetch_cloud_results.sh, n8n workflow
.github/workflows/    the cloud daily scrape (GitHub requires this path)

qa/tests/             QA engineer: offline unit tests
docs/archive/         the pre-merge README / PLAN / constraints, unedited

data/                 SQLite databases, logs, state, xlsx (gitignored)
logs/                 run logs (gitignored)
```

### Testing

```bash
.venv/bin/python -m pytest qa/tests/ -q
```

All parser tests run offline against captured payload shapes — no network. Since 24 Oct 2026
`qa/tests/conftest.py` enforces it for every test: no connection to another host or to the local
model servers or the running app, no `claude`/`gemini`/`ollama`/`gh`/`open`/`osascript`, the
person's own state files redirected, and environment variables restored after each test. The
one live test (Word lays out a .docx) runs only with `JOB_APPLIER_LIVE_WORD=1`. Since 25 Oct
2026 two more guards: no test may open `data/jobs.db` (asked for it, `init_engine` refuses),
and the person's own records (`data/applications.jsonl`, `data/apply/*.json`, the watch list,
the written answers) must be as they were after every test, or that test fails and names them.

`qa/tests/test_execution_hardening.py` (25 Oct 2026) puts the chosen jobs' queue under
pressure with real processes on a real database file: six starting a run, taking one job,
adding one job or recording one application at the same moment (exactly one succeeds); a
worker killed while preparing, with its form open, or waiting for you, then the app started
again; databases from before the queue and from its first build upgraded; an import cut short;
and every control in every state. Its child processes refuse the network and every program
but `ps` themselves, and get no keys.

---

## 6. Constraints & decisions (§C1–§C11)

These are the hard rules and the reasoning behind them. **Read this before changing
architecture or adding a portal.** Section numbers are cited from code comments and
`config.yaml`; they are stable.

### §C1 — Project nature
Personal automation for one person's job search. Not a commercial product, not a
service run on behalf of others. Built incrementally; each phase is reviewed and
tested before the next begins. Do not build ahead.

### §C2 — Scope: portals
**In scope**, each board's own public listing, one module each in `data_engineering/scraper/`
(boards held: active on 2026-10-24, 25,861 in all):

| ATS | endpoint | boards held |
|---|---|---|
| BambooHR | `<slug>.bamboohr.com/careers/list` | 8,781 |
| Greenhouse | `boards-api.greenhouse.io` | 4,080 |
| Workday | `POST /wday/cxs/<tenant>/<site>/jobs` | 2,749 |
| JazzHR | `<slug>.applytojob.com/apply` | 2,722 |
| Breezy HR | `<slug>.breezy.hr/json` | 1,872 |
| Ashby | `api.ashbyhq.com/posting-api` | 1,372 |
| Lever | `api.lever.co/v0/postings` | 1,340 |
| Workable | `POST apply.workable.com/api/v1/accounts/<slug>/jobs` | 1,151 |
| Recruitee | `<slug>.recruitee.com/api/offers/` | 664 |
| Teamtailor | `<slug>.teamtailor.com/jobs.rss` | 627 |
| Jobvite | `jobs.jobvite.com` | 503 |

Beside the boards: **job feeds** searched by title or category
(`data_engineering/scraper/feeds/`: USAJOBS, Jooble, Careerjet, Findwork, The Muse,
Remotive, Remote OK, Himalayas, Jobicy, HN Who is hiring, We Work Remotely, Working
Nomads) and Adzuna; and **job-alert emails** from Indeed, Dice, Glassdoor, ZipRecruiter
and Monster, read from your mailbox. A feed's copy of a posting already open from
another source is skipped (`feeds/common.py`), since a board's copy has the full
description and often a form the app can fill.

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

**Out of scope:** iCIMS, Taleo/Oracle, SuccessFactors, and custom in-house sites.
BambooHR has since moved into scope: `bamboohr.py` reads its public careers listing,
and it is now the largest source of boards.

**Rule:** one scraper module per **ATS type**, never per company. A company is a
tenant of an ATS, not a portal. Adding a company is a config/DB row, never code.

Workday moved **into** scope (`data_engineering/scraper/workday.py`): its public career-site API
turned out to be reachable without JS, and it is where the large employers are.
The rest of the "out of scope" list stayed out, and the reason is no longer only
effort — it is `robots.txt`. Checked 2026-09-14 against the campus ATS
directory in `data_engineering/universities/data`:

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
(`data_engineering/universities/import_boards.py`).

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
- **One documented exception: keyed official APIs.** `api.adzuna.com/robots.txt`
  disallows everything — it is addressed to crawlers — while Adzuna's API terms
  invite keyed use. `data_engineering/scraper/adzuna.py` therefore calls it
  outside `PoliteClient`, only with the person's own key, one call per 3s and at
  most `adzuna.max_calls` per run, far inside the free tier. Crawling a site
  that says no is still never done.

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

`qa/tests/test_writer_layout.py` pins each of these to the defect it fixes.

### §C9 — Applying: human in the loop
Most career portals' Terms of Service prohibit automated submission, so P4 was recast
as *prefill and hand over* (see the plan below): **the app fills the form and you press
Submit.** There is one exception, and it exists only if you switch it on.

- **By default every form stops before Submit** (`apply.stop_before_submit: true` in
  `backend/config/applicant.yaml`). The app's browser fills it, points out Submit and
  waits up to `apply.wait_minutes` (15) for you; closing the tab skips the job.
- **Submit by itself** (the Questions page, or Settings → Your details) is off unless you
  switch it on. It applies only to **Apply to these**, for an **Eligible** job on
  **Greenhouse's own board**, after Phase 3's document and required-question checks,
  with no CAPTCHA puzzle or declaration left for you. Legacy batch, morning prepared,
  job-page and assistant runs always wait for you. Workday and JazzHR forms, and every other form a
  posting leads to (`backend/apply/anyform.py`), always wait for you. The app never
  ticks a consent box.
- **CAPTCHAs are a stop condition**, never solved or evaded. Lever's turns the app's
  browser away even when you press Submit, so a Lever form opens in your own Chrome
  with every answer listed to copy.
- **Legal declarations are never guessed.** Work authorisation and sponsorship come from
  your details; left blank, the form stops for you.
- **Answers come from you.** A form is answered from your profile and the answers you
  saved on the Questions page. An essay draft is written from your own resume and
  earlier answers, and is never sent until you have edited it and pressed Save.
- **Only a confirmation page counts as applied.** When a site answers in words the app
  does not know, the job page asks you whether it went.
- **Limits:** no more a day than the lower of `limits.max_applications_per_day` (15) and
  your profile's `apply.max_per_day` (10); one job per company, and not the same company
  again within 12 hours. Apply to next 10, Prepare now and Start applying skip postings
  older than `apply.max_age_months` (4) and postings abroad.
- **Every application is kept**, in `data/applications.jsonl` and its run folder: what
  was sent (resume, cover letter, answers), the filled form and the thank-you page. The
  Applied page shows them.

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

**Claude Code as the writer** (`claude-code`) runs Claude through the Claude
Code CLI on your own plan: no API key and no per-call charge. A call that does not
answer within six minutes gives up rather than holding the page.

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
`logs/`, `backend/config/search_profile.yaml`, and **every resume document** out of the
repo — so no API key, no scraped job data, and no resume is ever committed.

That last one was a real near-miss: the pattern was `ml/resume/base_resume.*`, so a
resume saved under the person's own name was **not** ignored. It is now matched
by extension (`ml/resume/*.docx`, `*.pdf`, …). Nothing leaked — but check
`git status` before committing if you add a new file type there. **Check that before adding a file**: the
repo is public, and the DB in particular holds the full text of every posting.

`data/` being gitignored also means the database is *not* backed up by git. A
destructive DB change is unrecoverable; copy `data/jobs.db` before one.

---

## 7. Build plan

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
| ~~2~~ | ~~**P6 — Breadth**~~ ✅ Done 2026-09-11, extended since — 25,861 boards, 11 ATSs. | 10 boards / 105 jobs is a demo, not a job search. Biggest lever on outcomes, and Ashby is nearly free coverage. | No |
| ~~3~~ | ~~**P7 — Daily loop**~~ ✅ Done 2026-09-12. | Cheap glue that turns 5+ commands into one habit. Makes everything downstream actually get used. | No |
| ~~4~~ | ~~**P3B — Live tailoring**~~ | ✅ Done 2026-09-19 — and it needed no key in the end: a local model does it in 31s for nothing. | — |
| ~~5~~ | ~~**P8 — Relevance v2**~~ | ✅ Role fit done 2026-09-18; each posting's experience, pay, workplace and eligibility read since. Seniority weighting and an LLM re-rank still open. | Partly |
| ~~6~~ | ~~**P4 — Assisted apply**~~ | ✅ Built, and used on real applications since 2026-10-13 (§C9). | — |
| 7 | **P9 — Outcome feedback** | Partly built: the Applied page and email updates record what happened to each application. Funnel statistics wait for more applications. | Partly |

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

- [x] Project skeleton (`backend/config/`, `data_engineering/db/`, `data_engineering/scraper/`, `data_engineering/excel/`, `ml/resume/`, `submitter/`, `qa/tests/`)
- [x] `requirements.txt` covering all phases
- [x] `.gitignore` (secrets, DB, logs, resumes, audit artifacts)
- [x] `.env.example` + `python-dotenv` loading; no hardcoded secrets
- [x] `backend/config/config.yaml` as the single source of tunables
- [x] `backend/config/loader.py` — dotted-path lookup, UA templating, logging setup
- [x] Constraints recorded (now §C1–§C11 above)
- [x] Virtualenv created, dependencies installed
- [x] Documentation written
- [x] `data_engineering/db/migrate.py` — additive-only column migration, run on every startup
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
      rather than by asking**: `ml/resume/profile.py` reads the resume and writes the
      search (2026-09-09)

### Phase 2 — Excel Tracking Sheet ✅

105 jobs exported live; re-export is a clean no-op.

- [x] `data_engineering/excel/tracker.py` module, driven off `exported_to_excel`
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

- [x] `ml/resume/parser.py` — structured text from .docx, .pdf, .txt/.md
- [x] Auto-detect the base resume in `ml/resume/`, ignoring Word lock files
- [x] `ml/resume/scorer.py` — weighted keyword overlap between resume and JD
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

- [x] `ml/resume/tailor.py` — Claude API call (`claude-opus-5`, adaptive thinking, streaming)
- [x] Anti-fabrication guard: prompt rule + post-hoc check for invented skills,
      metrics, dates, employers (§C8)
- [x] Guard shares one skill vocabulary with the scorer so they can't drift
- [x] Reject any tailored output that fails the fabrication check
- [x] `ml/resume/writer.py` — ATS-safe .docx (no tables, columns, text boxes, graphics)
- [x] Employers, dates, education copied through verbatim — never rewritten
- [x] Review note (`*_review.txt`) listing rewrites, omissions, honest gaps
- [x] Deterministic output filenames tying a resume version to a job
- [x] Skip re-tailoring a job that already has an output file
- [x] CLI: `tailor [--job-id N] [--limit N]`
- [x] 42 offline unit tests for scorer, guard, parser, writer, pipeline
- [!] **Live tailoring run — blocked on you**: base resume in `ml/resume/` + `ANTHROPIC_API_KEY`

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

- [x] `ml/resume/profile.py` — held titles, years, seniority, role families, locations
- [x] Title evidence weighted 10× body evidence — unweighted counting ranked
      "network engineer" (6 body mentions of *networking*) above SRE, the person's
      actual job title
- [x] Weak families dropped but **reported** as "considered", so they can be added back
- [x] Exclusions derived from seniority — no junior roles for a lead, no staff/principal
      exclusions for someone who should be seeing them
- [x] An exclusion may never cancel a search term (a self-defeating filter returns nothing)
- [x] A non-technical resume falls back to its own held titles rather than being
      handed a software engineer's search — narrow, but never wrong
- [x] Saved to `backend/config/search_profile.yaml`, printed, and editable; `--force` regenerates
- [x] `data_engineering/scraper/filters.resolve_filter` prefers the derived profile over `config.yaml`
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
scrape (`data_engineering/scraper/lifecycle.py`).

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

- [x] **Ashby scraper** (`data_engineering/scraper/ashby.py`) — the third big startup ATS, and
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

**It runs every morning at 6 AM** from launchd: `devops/launchd/com.AI_Assistant_Job_Applier.daily.plist`
starts `devops/daily_pipeline.sh`, which holds a lock, waits for the Mac to be awake and
open, runs `main.py daily --cloud` and the steps after it (see [Quick start](#1-quick-start)).
Its Start, Stop and on/off switch are on the Jobs page, and the Health page shows how
each morning went. Setting the agents up by hand is in `devops/launchd/README.md`.

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

- [x] Facts read from each posting's own words: years of experience, pay, workplace
      (`backend/core/jobfields.py`), job type, and clearance / citizenship / visa
      sponsorship (`backend/core/eligibility.py`). They are Jobs-page columns, and a job
      page lets you correct them
- [~] Filters from those facts, as chips you switch on (*Within my experience*,
      *I'm eligible*, *Salary published*, *Remote*); they do not change the ranking
- [ ] Must-have vs nice-to-have weighting within the requirements section
- [ ] LLM re-rank of only the top N (bounded, cheap) producing a fit verdict and a
      named gap list — not another opaque number
- [ ] `score --why <job-id>` showing which terms drove the score
- [ ] Keep the free keyword scorer as the wide first pass; the LLM only re-ranks
- [ ] Recalibrate `resume.min_score` against the new distribution, documented in
      `config.yaml` the way the current number is

**Done when:** the top 20 by score are ones you agree are the top 20 — and
disagreements are explainable.

### Phase 4 — Assisted Apply ✅ built, and used on real applications

The recast below was accepted and built: the app fills the form and you press Submit,
with one opt-in exception for Greenhouse forms (§C9). The pages are described in
[§2](#2-the-web-app-page-by-page): a job's **Auto-apply**, and **Apply to next 10**,
**Prepare now** and **Start applying** on the Auto-apply page. What follows is the plan
as it was written, with each item marked as built.

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
| **Workday** | Hostile to automation: per-tenant subdomains, heavy JS, shadow DOM, mandatory account creation, email verification, multi-step wizards that lose state. Selectors differ per tenant, so "supporting Workday" means supporting each company separately. | **Not recommended** at the time — since built: every step is filled, while signing in and Submit stay yours (`backend/apply/workday.py`; the account is in the Keychain) |
| **Custom / in-house** | Unbounded variety, no shared structure to code against. | **Not automatable generically** — flag for manual |

Cutting across all four: **CAPTCHAs, bot detection, and account flags are the real
failure mode, not selectors.**

- [x] Written reliability assessment (above)
- [x] Your profile: `backend/config/applicant.yaml`, written by the *Your details* page
      (name, contact, address, links, current job, work authorisation, clearance)
- [x] **Application packet** per job: the folder with the job summary, the tailored
      resume and the cover letter (`ml/resume/packet.py`)
- [x] Cover letter generation under the same anti-fabrication guard as the resume (§C8)
- [x] **Screening-answer bank**: answers saved once on the Questions page and reused in
      each form's own choices; written answers kept as material for later drafts
- [x] Shared fill-and-verify run: `backend/apply/runner.py`, with one filler per form type
      (no `submitter/` package; the plan's name for it was not kept)
- [x] One visible browser for the whole run (Chrome with its own profile): you watch, you click
- [x] Greenhouse filler (every field the profile covers, resume and letter attached);
      stops before Submit unless *Submit by itself* is on
- [~] Lever: filled in the app's browser to learn the questions, then handed to your own
      Chrome with every answer listed, because Lever's CAPTCHA turns the app's browser away
- [x] Workday and JazzHR fillers; any other posting followed to the employer's form and
      filled (`anyform.py`)
- [x] Custom and screening questions found and listed; answered only from your profile and
      your saved answers
- [x] CAPTCHA = hard stop, never an evasion attempt
- [x] Daily cap, from `limits.max_applications_per_day` and your profile's `apply.max_per_day`
- [x] Audit log: `data/applications.jsonl` and each run's folder, with the resume sent, the
      filled form and the thank-you page
- [~] Status written back to the job (Applied) and shown on the Applied page; stages are
      kept in `data/apply/stages.json`, not in a database table
- [x] Verified on real postings (first on 2026-10-13)

**Still out of scope:** solving CAPTCHAs, and submitting anything but a Greenhouse form
with nothing left for you, and only with *Submit by itself* on.

### Phase 9 — Outcome Feedback ⏳ partly

What happened to each application is now recorded; nothing learns from it yet.
Without that second half, the filters, the threshold, and the target company list are
still tuned by intuition.

- [x] Application lifecycle on the **Applied** page: heard back → recruiter screen →
      technical → final → offer or rejected, plus position closed, withdrew and no
      response, each with a date and a note (`backend/apply/tracker.py`)
- [x] Replies read from your mailbox move an application on, and an employer's
      "thank you for applying" marks the job Applied (`backend/apply/inbox.py`)
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

## 8. What I need from you

| # | Item | Unblocks |
|---|---|---|
| ~~1~~ | ~~Base resume into `ml/resume/`~~ — ✅ supplied 2026-09-06 | done |
| ~~3~~ | ~~Your actual target roles~~ — ✅ now derived from the resume automatically | done |
| ~~2~~ | ~~`ANTHROPIC_API_KEY`~~ — ✅ not needed: Ollama or Claude Code write for free | done |
| ~~4~~ | ~~**Profile data**~~ — ✅ entered on the *Your details* page | done |
| ~~5~~ | ~~**Go/no-go on the P4 recast**~~ — ✅ prefill and hand over, with *Submit by itself* as your opt-in for Greenhouse forms | done |
| ~~6~~ | ~~**Breadth target**~~ — ✅ 300 boards was passed long ago: 25,861 now | done |
| 7 | **Your auto-apply workflow**, step by step. Built so far (23–24 Oct 2026): nothing older than 4 months is applied to, and *Apply to next 10* is on the Auto-apply page, taking saved jobs, then watched companies, then best fit. The next step is yours to name | the Auto-apply page |
| 8 | **Word access** (optional) — grant Word access to `data/tailoring/word` once | Word page checks in the web app |

Items 1–6 are done; item 7 is the work in progress.
