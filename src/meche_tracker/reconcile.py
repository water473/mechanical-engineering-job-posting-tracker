"""Reconcile observed inventories without confusing collection failures with closures."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from .models import (
    Catalog,
    Employer,
    FetchResult,
    HealthDocument,
    Job,
    RunReport,
    SourceConfig,
    SourceHealth,
)

MISS_INTERVAL = timedelta(hours=3)


def external_identity(ats: str, source_id: str, posting_id: str) -> str:
    """Identity aliases are explicit source identities, never fuzzy title matches."""
    return f"{ats}:{source_id}:{posting_id}"


def job_identity(ats: str, source_id: str, posting_id: str) -> str:
    identity = external_identity(ats, source_id, posting_id)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _source_jobs(jobs: list[Job], employer: Employer, source: SourceConfig) -> list[Job]:
    names = {source.id, *source.aliases}
    return [
        job
        for job in jobs
        if job.employer_id == employer.id and job.ats == source.ats and job.source_id in names
    ]


def _accepted_inventory(
    result: FetchResult, status: SourceHealth, now: datetime
) -> tuple[bool, str | None]:
    count = len({item.source_job_id for item in result.items})
    if not result.complete:
        return False, None
    if result.expected_count is not None and count != result.expected_count:
        return (
            False,
            f"Inventory count mismatch: expected {result.expected_count}, observed {count}",
        )
    previous = status.last_complete_count
    unexpected_drop = previous is not None and (
        (previous > 0 and count == 0) or (previous >= 20 and count < previous * 0.4)
    )
    if unexpected_drop:
        confirmed = (
            status.quarantined_count == count
            and status.quarantined_at is not None
            and now - status.quarantined_at >= MISS_INTERVAL
        )
        if not confirmed:
            if status.quarantined_count != count or status.quarantined_at is None:
                status.quarantined_count = count
                status.quarantined_at = now
            return False, f"Inventory drop quarantined: {previous} to {count} postings"
    status.quarantined_count = None
    status.quarantined_at = None
    return True, None


def reconcile(
    catalog: Catalog,
    health: HealthDocument,
    results: list[FetchResult],
    employers: list[Employer],
    now: datetime,
) -> tuple[Catalog, HealthDocument, RunReport]:
    """Return new validated state; input state is never modified.

    Inventory completeness alone controls missing observations. A successful
    details response controls field freshness, independently of feed presence.
    Explicit source aliases allow a configured board migration to retain IDs.
    """
    if now.tzinfo is None:
        raise ValueError("reconciliation time must include a timezone")
    updated = catalog.model_copy(deep=True)
    new_health = health.model_copy(deep=True)
    report = RunReport(started_at=now)
    employers_by_id = {employer.id: employer for employer in employers}
    health_by_id = {entry.source_id: entry for entry in new_health.sources}
    handled: set[str] = set()
    for result in results:
        if result.source_id in handled:
            raise ValueError(f"duplicate collection result for {result.source_id}")
        handled.add(result.source_id)
        employer = employers_by_id.get(result.employer_id)
        if employer is None:
            raise ValueError(f"unknown employer {result.employer_id}")
        source = next((value for value in employer.sources if value.id == result.source_id), None)
        if source is None or not source.enabled:
            raise ValueError(f"unconfigured or disabled source {result.source_id}")
        status = health_by_id.get(source.id)
        if status is None:
            status = next(
                (health_by_id[alias] for alias in source.aliases if alias in health_by_id),
                None,
            )
            if status is not None:
                health_by_id.pop(status.source_id)
                status.source_id = source.id
            else:
                status = SourceHealth(employer_id=employer.id, source_id=source.id)
                new_health.sources.append(status)
            health_by_id[source.id] = status
        if status.employer_id != employer.id:
            raise ValueError(f"source {source.id} belongs to another employer in saved health")
        accepted, quarantine_error = _accepted_inventory(result, status, now)
        status.last_attempt_at = now
        status.pages_fetched = result.pages_fetched
        status.complete = accepted
        status.errors = list(result.errors)
        if quarantine_error:
            status.errors.append(quarantine_error)
        status.detail_error_count = len(result.detail_errors)
        if accepted:
            status.last_success_at = now
            status.last_complete_count = len({item.source_job_id for item in result.items})
            status.consecutive_failures = 0
            report.sources_complete += 1
        else:
            status.consecutive_failures += 1
        if status.errors or result.detail_errors or not accepted:
            report.source_errors += 1
            report.errors.extend(f"{source.id}: {message}" for message in status.errors)
            report.errors.extend(
                f"{source.id}/{posting_id}: {message}"
                for posting_id, message in sorted(result.detail_errors.items())
            )
        report.sources_checked += 1
        owned = _source_jobs(updated.jobs, employer, source)
        known = {job.posting.source_job_id: job for job in owned}
        observed = {item.source_job_id: item for item in result.items}
        details = {posting.source_job_id: posting for posting in result.postings}
        # Details returned after pagination failed are still valuable observations.
        observed_ids = set(observed) | set(details)
        for posting_id in sorted(observed_ids):
            posting = details.get(posting_id)
            job = known.get(posting_id)
            if job is None:
                # Do not persist a shell with no details; retry its discovery next run.
                if posting is None:
                    continue
                job = Job(
                    id=job_identity(source.ats, source.id, posting_id),
                    employer_id=employer.id,
                    employer_name=employer.name,
                    industry=employer.industry,
                    source_id=source.id,
                    ats=source.ats,
                    posting=posting.model_copy(deep=True),
                    first_seen_at=now,
                    last_seen_at=now,
                )
                updated.jobs.append(job)
                known[posting_id] = job
                report.discovered += 1
            was_missing = job.availability in {"suspected_closed", "closed"}
            if job.source_id != source.id:
                old_identity = external_identity(job.ats, job.source_id, posting_id)
                job.aliases = sorted(set(job.aliases) | {old_identity})
                job.source_id = source.id
            job.employer_name = employer.name
            job.industry = employer.industry
            job.last_seen_at = now
            job.missing_since = None
            job.missing_count = 0
            if posting is not None:
                if posting.explicitly_closed:
                    # A 404/410 detail response often contains only a closure
                    # signal. Keep previously known role facts in the archive.
                    job.posting.explicitly_closed = True
                    job.posting.closure_reason = posting.closure_reason
                else:
                    job.posting = posting.model_copy(deep=True)
                job.details_checked_at = now
            item = observed.get(posting_id)
            if item is not None:
                job.posting.listed = item.listed
            if posting is not None and posting.explicitly_closed:
                if job.availability != "closed":
                    report.closed += 1
                job.availability = "closed"
                job.closed_at = job.closed_at or now
                job.status_reason = (
                    posting.closure_reason or "Employer explicitly closed the posting"
                )
                continue
            if was_missing:
                job.reopened_at = now
                report.reopened += 1
            job.closed_at = None
            job.last_verified_at = (
                min(job.last_seen_at, job.details_checked_at)
                if job.details_checked_at is not None
                else None
            )
            if job.last_verified_at is None or now - job.last_verified_at > timedelta(hours=48):
                job.availability = "unverified"
                job.status_reason = "Posting details have not been verified within 48 hours"
            else:
                job.availability = "open"
                job.status_reason = (
                    "Present in inventory; latest detail check failed, retaining prior verification"
                    if posting_id in result.detail_errors
                    else "Present in employer inventory"
                )
        if accepted:
            for job in owned:
                if job.posting.source_job_id in observed_ids or job.availability == "closed":
                    continue
                if job.missing_since is None:
                    job.missing_since = now
                    job.missing_count = 1
                    job.availability = "suspected_closed"
                    job.status_reason = "Absent from one complete employer inventory"
                elif now - job.missing_since >= MISS_INTERVAL:
                    job.missing_count = 2
                    job.availability = "closed"
                    job.closed_at = now
                    job.status_reason = (
                        "Absent from two complete inventories at least three hours apart"
                    )
                    report.closed += 1
    # Sources not visited or disabled retain their closure history; only freshness expires.
    for job in updated.jobs:
        if job.availability == "open" and (
            job.last_verified_at is None or now - job.last_verified_at > timedelta(hours=48)
        ):
            job.availability = "unverified"
            job.status_reason = "Last successful verification is older than 48 hours"
    report.employers_checked = len({result.employer_id for result in results})
    updated.jobs.sort(key=lambda job: job.id)
    new_health.sources.sort(key=lambda status: status.source_id)
    updated.generated_at = now
    if updated.initial_inventory_at is None and report.sources_complete:
        updated.initial_inventory_at = now
    new_health.generated_at = now
    return updated, new_health, report
