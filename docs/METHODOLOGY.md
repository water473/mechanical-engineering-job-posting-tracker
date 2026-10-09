# Methodology

This tracker discovers U.S. mechanical-engineering internships and co-ops on
employers' public applicant tracking systems. It is a periodically verified
directory, not a guarantee that an employer is still accepting applications at
the instant a reader clicks.

## Coverage and provenance

The initial registry contains 50 organizations: 29 Workday, 11 Greenhouse,
6 Ashby and 4 Lever. It spans aerospace/defense, automotive, industrial/HVAC,
medical devices, energy and robotics/consumer manufacturing. Toyota and Toyota
Research Institute are separately monitored recruiting organizations with a
shared parent ecosystem.

The initial checks returned valid JSON for all 50 public inventory endpoints.
Workday used empty search text and checked first, second and final advertised
pages. Lever was traversed to a short terminal page. These bounded checks are
recorded in `employer-onboarding.json`; they are not substitutes for the
collector's full pagination on each run. There are no initially disabled
sources. An enabled source may subsequently fail, which is visible in health
data.

Employer board identifiers were seeded from a public MIT-licensed registry and
verified directly. Live jobs come from the employer ATS, not copied lists or
third-party aggregators. Each posting retains its source URL and source posting
identity. See `../THIRD_PARTY_NOTICES.md` for attribution.

## What belongs in the directory

Classification is independent from whether the employer still lists a role.

- `include`: a current-student internship/co-op with a known U.S. location
  and evidence of mechanical relevance in its title, eligible major or physical
  engineering responsibilities.
- `review`: missing or ambiguous geography or mechanical relevance.
- `exclude`: evidence places it outside scope, such as a nonstudent role,
  intern-to-full-time conversion, exclusively non-U.S. locations, an explicitly
  ended program or unrelated work.

These are explainable text rules, not employer certifications. Every result
carries `reasons`. Specialties cover design/product, manufacturing/process,
test/validation, thermal/fluids, structures/materials, robotics/mechatronics,
quality/reliability and HVAC/building systems.

A role can be both an internship and a co-op. A stated term is retained without
inventing a year if none is stated. A past-looking season alone does not prove
closure. Explicit program dates are stronger evidence. Graduate-only roles
are flagged separately, while early-interest and talent-pool listings retain
their own `kind` instead of masquerading as ordinary requisitions.

## Facts, dates and unknowns

Keep source observations separate from tracker events:

| Field | What it establishes |
| --- | --- |
| `posting.employer_date` | Date supplied by the employer/ATS |
| `employer_date_kind` | Whether that field is publication, republication or posting date |
| `employer_date_precision`, `employer_date_raw` | Precision and original representation |
| `first_seen_at` | First discovery by this tracker |
| `last_seen_at` | Most recent positive inventory observation |
| `details_checked_at` | Most recent successful detail retrieval |
| `last_verified_at` | Older of inventory presence and detail verification |
| `reopened_at`, `closed_at` | Observed lifecycle transitions |

Discovery time is not the employer's posting date. Relative dates remain
distinguishable from exact source dates. Unknown pay, duration, housing,
relocation or sponsorship stays unknown. Pay retains the stated currency and
interval; hourly compensation is not silently annualized.

Eligibility text is retained as employer statements. Citizenship, U.S.-person,
export-control and clearance wording are not interchangeable. A company's past
sponsorship history does not establish sponsorship for a particular internship.

## Availability and closure

The collector reads a complete employer inventory with pagination, without
using a narrow search query as proof that other postings disappeared. Malformed
responses, page failures, repeated pages, inconsistent totals and request errors
prevent the run from being accepted as a complete inventory.

| Availability | Evidence |
| --- | --- |
| `open` | Observed in the employer inventory with details verified within 48 hours |
| `suspected_closed` | Missing from one accepted complete inventory |
| `closed` | Explicit employer closure, or two accepted complete misses at least three hours apart |
| `unverified` | Verification is stale or detail verification failed |

Only accepted complete inventories advance missing observations. Two manual
refreshes a few minutes apart do not satisfy the three-hour interval. A posting
that returns resets the missing state and records reopening when applicable.

An unexpected collapse is quarantined before it can close jobs. The guard
covers a positive inventory becoming empty and inventories of at least 20
falling below 40% of their prior size. A matching smaller complete inventory
must be observed at least three hours later before that reduction is accepted.

The normal detail refresh window is 24 hours, with new/changed postings checked
earlier. After 48 hours without current inventory-and-detail evidence, an open
record becomes unverified. A source outage does not close its jobs. The source
health document records attempts, last accepted success, completeness,
pagination and errors.

## Identity and persistence

Source IDs and ATS posting IDs determine stable identity. Explicit reviewed
source aliases preserve a migrated board's history when the posting identity
is unchanged. Similar titles are not sufficient to merge requisitions:
different locations or requisition numbers can represent different opportunities.

The canonical catalog keeps excluded and closed records for history and audit.
Generated Markdown presents selected views; its visible row count is not the
number of all employer openings.

Source facts, classification and availability are separate so a parser fix does
not invent an employer closure. Reviewed overrides are applied after automatic
classification and cannot alter identity, verification times or lifecycle state.
The reason and review timestamp make the correction auditable.

## Operating limits

The scheduled workflow targets a three-hour cadence. GitHub scheduling delays,
employer changes, endpoint limits and outages can extend it. A successful local
onboarding check does not establish future reachability from hosted runners.
Use each job's verification timestamp and the source health record when judging
freshness.

Coverage is limited to the configured employers and four supported ATS platforms.
A successful board with zero matching internships is useful coverage, not a
reason to add unrelated roles. Public ATS dates and text rules can still be
ambiguous; corrections should cite the employer and improve the relevant rule
or add a reviewed override.
