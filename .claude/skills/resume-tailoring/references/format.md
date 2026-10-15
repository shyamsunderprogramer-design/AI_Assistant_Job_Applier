# Resume format

Apply this format to every tailored resume. `scripts/build_resume.py` implements all of it, so describe the content and let the script set the type.

## Lines and sections

| Line / section | Content | Font | Size | Style | Alignment |
|---|---|---|---|---|---|
| 1st line | Candidate name | Cambria | 18 pt | Bold | Center |
| 2nd line | Location, phone, email, LinkedIn, GitHub | Cambria | 11 pt | Regular; links clickable | Center |
| 3rd line | Professional title and primary technologies | Cambria | 11 pt | Bold | Center |
| Section headings | SUMMARY, HIGHLIGHTS, EXPERTISE, SKILLS, EXPERIENCE, PROJECTS, CERTIFICATIONS, EDUCATION | Cambria | 12 pt | Bold, uppercase | Left |
| Summary text | Background and experience | Cambria | 11 pt | Regular; selected key phrases bold | Justified |
| Highlights / expertise bullets | Main achievements, areas of expertise | Cambria | 11 pt | Regular; selected terms bold | Justified |
| Skills rows | Category, then tools: "Cloud: AWS; Azure; GCP." | Cambria | 11 pt | Category bold; tools regular, the JD's tools bold | Left |
| Job heading | Title and company; dates on the same row | Cambria | 11 pt | Bold | Title/company left; dates right |
| Experience subheadings | e.g. "Cloud Infrastructure", "CI/CD Automation" | Cambria | 11 pt | Bold | Left |
| Responsibility bullets | Work, tools, outcomes | Cambria | 11 pt | Regular; selective bold | Justified |
| Project heading | Project name | Cambria | 11 pt | Bold | Left |
| Project description | Purpose, technologies, contribution | Cambria | 11 pt | Regular | Justified |
| Certification rows | Name, issuer, year | Cambria | 11 pt | Name bold; rest regular | Left |
| Education rows | University, degree, years | Cambria | 11 pt | University bold; degree regular | Left; years right |

## Layout

| Setting | Value |
|---|---|
| Page | US Letter, portrait, 8.5 × 11 in |
| Margins | 0.65 in on all sides |
| Text color | Black |
| Line spacing | Single (1.0) |
| Name and contact spacing | 0 pt before, 3 pt after |
| Title line spacing | 0 pt before, 6 pt after |
| Section heading spacing | 8 pt before, 4 pt after |
| Body paragraph / bullet spacing | 0 pt before, 3 pt after |
| Bullet position | 0.15 in from the left text margin |
| Bullet text position | 0.30 in from the left text margin |
| Wrapped bullet lines | Under the first word of the bullet text (hanging indent) |
| Dates | Right-aligned tab stop at the right text margin |
| Line endings | Let text wrap; press Enter only for a new paragraph or bullet |
| Last line of a paragraph | Left as it falls, never stretched across the page |
| Page breaks | Headings stay with the text after them; don't split a bullet |
| Name and contact | In the document body on page 1, not a Word header |
| Section order | Summary → Highlights → Expertise → Skills → Experience → Projects → Certifications → Education; leave out empty sections |

## ATS safety

- One column. No tables, text boxes, images or icons.
- Bullets are a literal "•" plus a tab, not Word list numbering. Some parsers drop list numbering, and the bullets then run together into prose.
- Real section headings (Word's Heading 1, restyled) so parsers can find the sections.
- Two spaces before the date tab, so text extraction doesn't glue the title to the dates.
