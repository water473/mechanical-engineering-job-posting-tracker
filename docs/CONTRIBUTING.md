# Contributing

Use the employer issue form to suggest coverage and the posting correction form
to report a specific field. Supply the official employer URL and the date you
checked it. Do not submit personal application data.

## Add or change an employer

Create one JSON file in `config/employers/`, using an existing file from the same
ATS as the working example. The `Employer` model requires:

| Field | Meaning |
| --- | --- |
| `id` | Stable lowercase letters, digits, hyphens or underscores |
| `name` | Employer display name |
| `website` | Official employer website |
| `industry` | Human-readable sector |
| `sources` | One or more public ATS source configurations |

Each source has a globally unique `id`, a supported `ats`, an HTTPS
`board_url` on that ATS's public domain and `enabled`. Greenhouse, Lever and
Ashby should set an explicit `board_token`; Workday should set `tenant`,
`site` and `locale`. Workday's tenant is not necessarily the employer name:
RTX uses `globalhr`, and GE Appliances uses `haier`.

Start a new source with `enabled: false`. Confirm its association with the
employer, check the unauthenticated JSON endpoint and inspect its pagination
before enabling it. An enabled source requires a timezone-aware `verified_at`
timestamp. Record what you checked and its limits in `verification_note`.
Do not use internal, restricted, confidential or authenticated career sites.

`student_only` is true only for a verified student-specific board; it is not a
way to make a general board's roles pass classification. Optional
`candidate_title_patterns` are valid regular expressions for finding unusual
student-program titles. Keep them narrow and test representative titles.

Treat a stable source ID as part of the persistent job identity. Do not rename it
just to improve display wording. For a genuine migration, use reviewed
`aliases` and test whether the ATS posting identity is preserved; never merge
records merely because their titles look alike.

Then run:

```sh
uv run --locked meche-tracker validate
uv run --locked meche-tracker sources check
uv run --locked meche-tracker refresh --dry-run
uv run --locked ruff check .
uv run --locked pytest -q
```

A successful first page is onboarding evidence. Full pagination, count
consistency and source health must also pass in the collector before reporting
complete monitoring.

## Correct an existing posting

Do not edit the generated README or views. Change the parser when a source-wide
rule is wrong; use an override when a reviewed correction belongs to one posting.

`data/overrides.json` is an `OverrideDocument` with `schema_version: 1` and an
`overrides` array. Every entry requires the exact stored `job_id`, a nonempty
`reason`, a timezone-aware `reviewed_at`, and optional `posting_fields` and
`classification_fields` objects.

Put the employer evidence URL, the relevant statement and your interpretation
in `reason`. The correction must be traceable even after the posting changes.
Duplicate job overrides, unknown job IDs, unsupported fields and invalid values
fail validation.

Allowed posting corrections are `title`, `source_url`, `apply_url`,
`requisition_id`, `description`, `locations`, `workplace`,
`employment_type`, `employer_date`, `employer_date_kind`,
`employer_date_precision`, `employer_date_raw`, `pay` and `listed`.

Classification corrections use the fields on `Classification`, including
`decision`, `reasons`, `specialties`, `opportunity_types`, `terms`,
`degrees`, `graduate_only`, `kind`, program dates/duration, explicit benefit
flags and `eligibility_statements`. Use the model's actual enum values.

Overrides cannot rewrite collection timestamps, identity, source health or
availability. A report that a role has closed should trigger source verification.
Do not turn a timeout into a manual closure or describe an inclusion correction
as a statement from the employer.

After reviewing an override, validate and regenerate:

```sh
uv run --locked meche-tracker validate
uv run --locked meche-tracker render
uv run --locked pytest -q
```

Review the generated diff together with the override. Preserve null or unknown
values when the source does not support a positive or negative assertion.

## Tests and data

Include a fixture or focused regression test for changes to parsing,
classification, pagination or lifecycle behavior. Include failure cases where
they affect correctness: empty versus malformed payloads, duplicated pages,
mixed countries, expired programs and ambiguous qualifications.

Do not infer visa support from company reputation, infer a posting date from the
time we found it, or replace a source statement with a confident guess. Strip
personal information from fixtures. Contributions are covered by the repository
license; preserve relevant third-party notices.
