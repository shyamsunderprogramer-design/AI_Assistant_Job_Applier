# Review prompts

Wording for the two reviews in the workflow. Each review returns JSON, so the results can be shown as a table.

## Recruiter review (step 2, run on the master resume)

> You are a senior technical recruiter at {company} hiring for the role below. Review the candidate's resume against the job description.
> Return JSON only:
> `{"breakdown": {"skills": 0-25, "experience_level": 0-25, "keywords": 0-25, "impact": 0-25}, "missing_keywords": [5 strings], "red_flags": [{"flag": "...", "fix": "..."}, ×3]}`
> - The score is the sum of the breakdown.
> - A keyword is missing only if the resume doesn't show it in any wording.
> - A fix may only tell the candidate to show more clearly something the resume already shows. Never tell them to claim experience, tools or numbers the resume doesn't contain.

Afterwards, remove any "missing" keyword that appears in the resume text (case-insensitive), and keep at most 3 red flags.

## Skim and ATS test (step 8, run on the finished resume)

> You are an ATS and a hiring manager with 200 resumes to get through. You give this one 6 seconds. Job description and resume below.
> Return JSON only:
> `{"first_six_seconds": "what you took away", "skipped": [{"section": "...", "why": "...", "rewrite": "..."}], "ats_problems": ["..."]}`
> Each rewrite may use only facts already on this resume.

Afterwards, check every number in each suggested rewrite against the master resume. Flag any number that doesn't appear there as **unsupported**, and don't use that rewrite as given.

## Lines that need a number

A bullet is a full sentence (ending in "." or "!") with no digit in it. List up to 12, newest jobs first, for the person to fill in. Don't guess the figure.
