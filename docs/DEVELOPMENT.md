# Development

The collector and generators run on Python 3.14.6 with uv 0.12.17. There is no
application server or database to operate. Git stores the canonical JSON records
and the generated Markdown views.

## Set up and verify

From the repository root:

```sh
uv python install 3.14.6
uv venv --python 3.14.6 .venv
uv sync --locked
uv run --locked meche-tracker validate
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
```

CI installs the locked dependencies, then runs lint, tests and validation without
requiring live employer APIs. Tests use fixtures and controlled HTTP responses.
Keep live discovery out of the offline test suite.
All these commands run inside the project-local `.venv`; do not install project
dependencies globally. `.python-version` and `uv.lock` are shared with CI.

## Run the collector

```sh
uv run --locked meche-tracker sources check
uv run --locked meche-tracker refresh --employer spacex --dry-run
uv run --locked meche-tracker refresh --employer spacex
uv run --locked meche-tracker refresh
uv run --locked meche-tracker refresh --employer spacex --force-details
uv run --locked meche-tracker render
uv run --locked meche-tracker query --help
```

Source checks and refreshes make unauthenticated requests to public employer ATS
endpoints. A dry run still fetches data but does not publish changes. Use the
single-employer option while developing an adapter. Force detail refresh only
when testing a changed parser or correcting stale extraction.

Global options precede the subcommand: for example,
`uv run --locked meche-tracker --root . validate`. Put `--root` before
`refresh`, `render`, `query` or `sources`; it is not a subcommand option.

## Combine filters

Repeated values within a facet use OR; different facets use AND. Unknown facts
do not satisfy a requested benefit or compensation threshold. State values are
postal abbreviations, and specialty names match the generated view slugs.

```sh
uv run --locked meche-tracker query --state CA --state WA --type internship --season summer --year 2027 --format markdown
uv run --locked meche-tracker query --specialty manufacturing-process --pay-available yes --minimum-pay 25 --pay-unit hour --pay-currency USD
uv run --locked meche-tracker query --employer spacex --housing yes --sort posted --format markdown
uv run --locked meche-tracker query --graduate-only --format markdown
uv run --locked meche-tracker query --degree unspecified --format markdown
uv run --locked meche-tracker query --status closed --include-stale --include-graduate --format markdown
```

Available facets also include industry, Census region, workplace, start/end
dates, duration in weeks, discovery recency, verification age, relocation,
sponsorship, early-interest/talent-pool kind, availability and review decision.
The default query uses the same verified undergraduate criteria as the README.

GitHub refreshes publish commits to `main`; they do not update another local
checkout automatically. Run `git pull --ff-only` to retrieve published data
before making local changes.

The normal refresh reads the complete job inventory, then fetches details for new
or changed candidate postings and for details older than 24 hours. Errors are
recorded per source. Pagination failure must never be translated into an empty
successful board.

## Repository responsibilities

| Path | Responsibility |
| --- | --- |
| `config/employers/*.json` | Reviewed employer and public source configuration |
| `src/meche_tracker/adapters/` | ATS inventory and detail retrieval |
| `src/meche_tracker/models.py` | Strict, shared source and record schemas |
| `src/meche_tracker/normalization.py` | Source text and location normalization |
| `src/meche_tracker/classification.py` | Explainable inclusion and extracted attributes |
| `src/meche_tracker/reconcile.py` | Independent availability, verification and history |
| `src/meche_tracker/store.py` | Validated state, overrides and staged publication |
| `data/jobs.json` | Canonical catalog and lifecycle history |
| `data/source_health.json` | Source attempts, completeness and errors |
| `data/overrides.json` | Reviewed manual corrections; never generated |
| `README.md`, `views/**/*.md` | Generated presentation |
| `docs/employer-onboarding.json` | Dated, bounded evidence from initial source checks |

The shared models reject unknown fields. Adding a field means updating its model,
relevant extraction and rendering, and a meaningful fixture that exercises the
behavior. Preserve source IDs, posting IDs and history when changing parsers.

## Scheduled publication

The refresh workflow runs at minute 17 every three hours in UTC. GitHub's
scheduler can delay or skip runs, so the schedule is not a freshness guarantee.
The recorded verification times and source health establish actual freshness.

Manual runs on the default branch accept an employer ID, a dry-run switch and a
force-details switch. Manual dry run defaults to false; enable it to inspect
a refresh without publishing. CI can run on pull
requests; the publishing workflow has only scheduled and manual triggers.

The workflow installs pinned Python/uv versions, validates state, refreshes,
validates again and stages only:

- `README.md`
- `data/jobs.json`
- `data/source_health.json`
- Markdown files under `views/`, including deleted generated views

The writer uses a shared concurrency group. It refuses to push if the default
branch advanced during collection. A normal push also rejects a later race;
there is no force push or automatic rebase of generated records.

Only the publishing job requests `contents: write`. The GitHub token is exposed
to the final Git publication step, not to collection. The default branch's
rules must permit the chosen automation account to commit generated outputs.
No remote repository or branch settings are created by the application.

## Recovery

A missing or corrupt canonical state file is an error, not an empty catalog.
Restore the last good committed state before retrying; do not delete state to
clear an exception. Local publication validates staged files and restores the
previous files if installation fails. Git commits provide the cross-file
publication boundary in automation.

For a failing source, inspect `data/source_health.json`, check its official board
and retry only that employer. A timeout, rate limit, HTML challenge or malformed
JSON is collection failure. Preserve its jobs as unverified rather than
asserting closure. Source onboarding from a local machine does not establish
reachability from a GitHub-hosted runner.

For a large legitimate reduction in an employer's inventory, wait for the next
eligible run. The reconciliation guard requires a matching smaller complete
inventory at least three hours later before accepting the drop.
