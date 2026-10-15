---
name: resume-tailoring
description: Tailor a master resume to one job description and produce a recruiter-ready Word resume. It runs a recruiter review (score out of 100, missing keywords, red flags), rewrites bullets in Google's XYZ order only where a number already exists, keeps experienced candidates at 3–4 pages, merges repeated skills sections, takes contact details from the person's profile, sets everything in the Cambria format, bolds the job's tools and skills, and finishes with a 6-second skim and ATS test. Never invents experience, numbers or tools. Use this skill whenever someone wants a resume tailored, targeted, rewritten or optimized for a job posting, wants to know how their resume scores against a JD, asks for ATS or keyword matching, or pastes a job description next to a resume, even if they don't say "tailor".
---

# Resume tailoring

Turn a **master resume** (long, everything the person has done) and **one job description** into a tailored resume a recruiter will read. The person must be able to defend every line in an interview. A tailored resume that claims something untrue costs them the job, so honesty outranks keyword score at every step.

## Inputs

- **Master resume**: .docx, .pdf or pasted text. This is the only source of facts.
- **Job description**: the posting text, plus the company name if known.
- **Profile (optional)**: current location, phone, email and LinkedIn. If given, these replace the resume's contact line, because the resume is often out of date and the profile is what the person keeps current.

If any of these is missing, ask for it once.

## The one rule: nothing is invented

Every claim in the output must already appear in the master resume. This covers each tool, number, employer, date, degree and responsibility. You may:
- reorder, shorten or merge the person's own lines;
- reword a line into the posting's vocabulary when the resume shows **the same work** in other words (for example, the resume says "built Terraform modules" and the JD says "infrastructure as code");
- drop lines that do nothing for this posting.

You may not add a tool the resume doesn't name, a number it doesn't state, or a duty it doesn't describe. If the JD wants something the resume lacks, list it as a **gap** for the person to answer. Never quietly write it in. When unsure, leave the line as written.

## Workflow

### 1. Read both documents
List the resume's jobs (title, employer, dates), degrees, and every tool it names. From the JD, pull the required and preferred skills, the tools, and the seniority. Check whether the posting limits who may apply. For example, US federal postings that say "Who may apply: U.S. citizens" exclude non-citizens; say so up front instead of tailoring.

### 2. Recruiter review (before rewriting)
Act as a senior recruiter at the company. Score the **master** resume against the JD:
- **Score out of 100**, as four parts of 25: skills match, experience level, keywords, and impact (results and numbers).
- **Top 5 missing keywords.** Only list ones the resume really doesn't show. If the resume says it in other words, it isn't missing; bring it into the JD's wording in step 3.
- **3 red flags**, each with a fix. A fix may never be "claim X". It can only be "show X, which you already did, more clearly".

This review guides the rewrite. Show it to the person afterwards; see `references/review-prompts.md`.

### 3. Tailor the content
- **Summary:** 3–5 sentences aimed at this posting, built only from the resume's own experience, with the person's real years of experience.
- **Bullets in XYZ order, only where the bullet already has its result.** "Accomplished [X] as measured by [Y], by doing [Z]." Rewrite "Led release management… reducing release cycles by 42%" as "Cut release cycles 42% by running release management for 10+ applications with Azure DevOps…". Do **not** reorder a bullet that has no number, and never add one. Instead, collect those bullets in a short **"Add a number to these"** list for the person. Only they know the figure.
- **Keep the tools in older roles.** Experienced candidates lose credibility when older jobs shrink to one vague line, because recruiters read the tools to understand the work. Keep at least 3 bullets per role where the master has them, and keep lines that name a tool.
- **Merge repeated sections.** Master resumes often have Key Expertise, Highlights of Impact and Core Competencies all saying the same things:
  - Fold Key Expertise rows into the skills section. When two rows cover the same topic (e.g. "Security & Compliance (DevSecOps)" and "Security / DevSecOps / Compliance"), make one row with the work first, then the tools.
  - Drop a Highlights line whose numbers the summary already gives; any other Highlights lines go at the end of the summary.
  - This applies to the tailored copy only. The master stays as it is.

### 4. Length: 3–4 pages for experienced candidates
For someone with about 8 or more years of experience, aim for **3 pages minimum and 4 maximum**. Four is a ceiling the content has to earn, not a target. Trim first the lines that match nothing in the posting **and** name no tool. Stop trimming at 3 pages. Never cut a job title, employer, date or degree. For early-career candidates, use 1–2 pages.

### 5. Contact line
Use the profile's location, phone and email when given. Keep the resume's own "(Open to Relocate)" note and the LinkedIn link. Keep the name and title line as the resume has them.

### 6. Format
Follow `references/format.md` exactly. It sets Cambria at 11 pt, black text, single spacing, 0.65" margins, bold job headings with dates on a right tab, bullets at 0.15"/0.30", and the section order. Use `scripts/build_resume.py` to produce the .docx. It applies the whole spec, so describe the content as JSON and let the script do the layout (see the docstring at the top of the script).

### 7. Bold the posting's keywords, selectively
Bold the JD's **tools and named skills** where the resume already uses them, for example **Kubernetes**, **Terraform**, **CI/CD**, **Infrastructure as Code**. Don't bold ordinary words the JD happens to share, such as "management", "design", "best", "platform" or "development".
- Bold a whole product name together: **Azure Key Vault**, **Azure DevOps**, **Spring Boot**.
- At most **3 per bullet, paragraph or skills row**, and each term once per line, so the eye lands on what matters.
- Bold in the summary, bullets and skills-row items. In skills rows the category name is bold anyway.
- Pass the list as `keywords` to the build script; it applies these rules.

### 8. Skim and ATS test (after building)
Read the finished resume as an ATS and as a hiring manager with 200 resumes and 6 seconds each:
- **First six seconds:** what a recruiter takes away from the top third of page 1.
- **Skipped sections:** what they'd skip and why, with a rewrite that uses only facts already in the resume.
- **ATS problems:** tables, text boxes, images, headers or footers holding content, odd section names, or dates a parser can't read.
- **Unsupported numbers:** any figure in your output that's not in the master resume. There should be none. If there is one, remove it.

## Output
Deliver:
1. **The tailored .docx** (and a PDF if Word or LibreOffice is available: `soffice --headless --convert-to pdf file.docx`).
2. **Recruiter review:** score and breakdown, missing keywords, red flags with fixes.
3. **What changed:** rewritten bullets (before → after), lines left out, sections merged.
4. **Gaps:** what the JD asks for that the resume doesn't show, phrased as questions to the person ("Have you used OKE? If so, where?").
5. **Add a number to these:** bullets that would be stronger with a figure only the person knows.
6. **Skim test** results.

Keep the report short and plain. The person reads it to decide what to send.
