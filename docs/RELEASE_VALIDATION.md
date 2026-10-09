# Release validation

Local verification completed 2026-10-09 03:31 UTC.

## Implemented and exercised

- Python 3.14.6 and locked dependencies run in the project-local `.venv` through uv.
- 50 enabled employers across Greenhouse, Lever, Ashby and Workday; real collection exercised all four adapters.
- Persistent schema-versioned JSON, durable identities and lifecycle history, reviewed overrides, atomic local publication and Markdown filters.
- Scheduled GitHub refresh at `17 */3 * * *`, manual refresh/dry run/forced details, offline CI and contribution forms.
- 87 offline tests pass. Ruff lint and formatting checks pass. Workflow YAML and multiline Bash syntax validate.
- 50 manually reviewed job decisions across all four adapters match the final rules; zero unresolved errors in the reviewed published sample. See [audit evidence](classification-audit.json).

## Current live snapshot

- 938 catalog records; 416 verified undergraduate-compatible U.S. opportunities.
- 48/50 complete inventories on their latest attempts.
- 133 generated Markdown pages; README shows up to 200 newest discoveries.
- Carrier and Dexcom returned individual Workday rows without a public title or URL. Usable candidates are preserved, source incompleteness is visible, and these inventories cannot advance closure observations.
- RTX completed a full inventory on the second final refresh after earlier inventory instability.

## Repeated live collection

Full refreshes started 2026-10-09T03:18:52.955790Z and 2026-10-09T03:24:12.997468Z.

- All 937 existing records retained their identity and discovery date.
- Existing main-list relative ordering and normalized content hashes were preserved.
- 1 new source posting was discovered; no old records were rediscovered or removed.
- Positive inventory observations advanced; there were no false closures or duplicate internal IDs.
- Final offline normalization/rendering refined cached locations, explicit employer dates, and program metadata without inventing successful fetch timestamps.

## Activation

Git is initialized locally. No GitHub remote is configured, so scheduled execution and hosted-runner network reachability remain pending. Publish this repository to a public GitHub repository, enable Actions, permit the refresh workflow to commit generated files, and run the workflow manually once. Local refresh already works and never commits or pushes by itself.

## Practical limits

This is coverage of the configured employers, not every U.S. employer. Public Workday interfaces can change or return incomplete data. Exact employer posting dates, compensation, housing, sponsorship and duration remain unknown where the source provides no clear evidence. Ambiguous candidates stay in the review view. No browser fallback, database, or interactive website is included in this draft.
