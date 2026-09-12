# companies/ — worldwide company reference database

Separate from `data/jobs.db` (the application pipeline). This is a vetted
directory of real companies with visa-sponsorship evidence, stored in
`data/companies.db`.

## Why these sources

"No fake, no near-bankrupt, with sponsorship info" is what government
sponsor registries already enforce: a company on the UK register or with
approved US H-1B petitions is real, operating, and has sponsored visas.
Tier lists (Fortune Global 500) add the large stable companies.

## Usage

```bash
# Layer 1 — top-tier companies
python -m companies.seed_tier_lists

# Layer 2 — official sponsor registries
python -m companies.import_uk_sponsors            # auto-fetches gov.uk CSV
python -m companies.import_us_h1b [csv-or-url]    # USCIS H-1B Employer Data Hub
python -m companies.import_nl_ind <file>          # IND register (manual download)

# Layer 3 — enrichment (polite, resumable; run in batches)
python -m companies.enrich_careers --tier fortune500 --limit 200
python -m companies.enrich_careers --tier sponsor --limit 1000

# Export flat CSVs
python -m companies.export_csv
```

Raw downloads cache in `data/companies_build/raw/` — re-runs reuse them.
Every import is logged in the `import_log` table; every sponsorship fact
keeps its registry in `sponsorship.source`.

## Querying

```bash
sqlite3 data/companies.db "
  SELECT c.name, c.country, s.program
  FROM companies c JOIN sponsorship s ON s.company_id = c.id
  WHERE c.country = 'GB' LIMIT 20;"
```

Progress during a build: `data/companies_build/PROGRESS.md`.
