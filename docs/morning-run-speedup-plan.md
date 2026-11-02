# A faster morning run: a plan

*Written 2 Nov 2026. A plan only: nothing here is built until you approve it.*

> **Built 2 Nov 2026 (approved), switched off.** One correction: a fetch takes about **2½ hours**,
> not 50 minutes. Greenhouse keeps all ~4,200 of its boards on one host, and at the polite pace
> that host alone takes about 2¼ hours (this Mac's fetch of 2 Nov). Splitting by site cannot
> shorten that without reading Greenhouse faster, which the politeness rule forbids. The morning
> list is still ready in minutes, from the evening fetch; the night's postings follow about
> 2½ hours after the morning run starts.
>
> To switch it on, each with your yes: push the new workflow; one test run on GitHub; install the
> 8 pm job (`devops/launchd/com.AI_Assistant_Job_Applier.evening.plist`); set
> `cloud.trigger_runs: true` in `backend/config/config.yaml`; restart the board search once so it
> knows to pause (the one running now does not).

## The problem

The online fetch on GitHub is scheduled for 1:17 am, but GitHub starts it 6–7 hours late every day
(between about 6 and 8 am here), and it then takes about 3 hours. So when the morning run starts, today's
fetch is never ready: the run either waits for it, or reads all ~27,600 job boards from this Mac, which
takes about 2 hours (as on 2 Nov, 7:41–about 9:45).

## When jobs appear (your own data)

Postings from the last 30 days that state a time (about 8,900), by hour, Central time:

- busiest in the afternoon, **3–4 pm**;
- an even bigger spike at **1–2 am** (employers' systems publishing overnight);
- about **46%** appear between 9 pm and 6 am.

So one evening fetch is not enough on its own: half of each day's new jobs would wait a day.

## The plan: two fetches a day, each about 50 minutes

### 1. An evening fetch, started by the Mac

At **8 pm** the Mac presses GitHub's own "Run" button for the fetch (it starts within a minute, unlike
the schedule). If the Mac is asleep at 8 pm, it presses it when it wakes, but only until midnight;
after that the morning fetch covers it. GitHub's own schedule stays as a backup.

### 2. A morning fetch, started when you open the Mac

The morning run:

1. takes in the evening's fetch at once: your list, scores, sheet and summary are ready in
   **about 7 minutes**;
2. starts a second online fetch, for the jobs posted overnight;
3. when that one finishes (about 50 minutes later), takes in its new jobs, scores them, and adds them
   to the list, sheet and summary by itself.

### 3. Each fetch split into 4 parts that run at the same time

About **50 minutes instead of 3 hours**. Your project's page on GitHub is public, so the extra time
costs nothing.

The split is **by job site** (Greenhouse in one part, Lever and Ashby in another, and so on), so each
site is still read by one machine, at the same polite pace as today. Nothing is read faster from any
one site: the project's politeness rule stays as it is.

### 4. The board search pauses during each fetch

It pauses when a fetch starts and carries on from where it was when the fetch ends, so the two don't
compete for this Mac's internet.

## What stays the same

- No resume, application or personal detail goes to GitHub: as today, the online fetch reads only public
  job postings, and its results are locked before they leave GitHub.
- If GitHub is unreachable or a fetch fails, the morning run reads the boards from this Mac, as it does
  today.
- Scoring, tailoring and applying stay on this Mac.

## Size

About **1 to 1½ working days** (more than the half day said earlier: splitting the fetch needs a final
step that joins the four parts back together).

Testing needs **real runs on GitHub**: the same public job boards the daily fetch already reads, nothing
sent anywhere else. Each one needs your yes.

## Not in this plan

- Changing what the fetch looks for, or which boards it reads.
- Anything about applying.

---

## Technical notes (for whoever builds it)

- `.github/workflows/daily-scrape.yml`: one job today, cron `17 6 * * *` (started 12:05–13:15 UTC,
  25 Oct–1 Nov); `workflow_dispatch` already present; concurrency group `daily-scrape`, no cancelling.
  Split into a matrix of 4 site groups (balanced by last run's time per site from `scrape_log`), each
  restoring the cached `data/jobs.db`, scraping only its sites (`main.py scrape --sites ...`, new
  option), uploading its database; then one join job merges the four (`devops/merge_cloud.merge`),
  saves the cache, encrypts and uploads `jobs-db-encrypted` as now.
- `devops/cloud_results.py`: `today_run()` takes only runs created today, local time. Change to: every
  successful run not yet collected, started since the last one collected (at most ~20 hours back), taken
  in order. `COLLECTED` records each run taken in.
- Evening trigger: a new launchd job at 20:00 (`gh workflow run "Daily scrape"`), skipped when a run is
  queued or running or when it is past midnight.
- Morning: `main.py daily --cloud` collects, then starts a run; a follow-up step (new, e.g.
  `main.py daily --top-up`) waits for it (up to 2 h), merges, ranks new postings, exports, adds to the
  digest.
- Board search: pause/resume through the existing probe-runner stop and resume (it resumes at the name it
  stopped on).
- Tests: made-up runs and databases, fake `gh`; no real GitHub call in tests.
