from datetime import UTC, datetime, timedelta

import pytest

from meche_tracker.models import (
    Catalog,
    Employer,
    FetchResult,
    HealthDocument,
    InventoryItem,
    Posting,
    SourceConfig,
    SourceHealth,
)
from meche_tracker.reconcile import reconcile

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


def employer(source_id="example", *, aliases=None, enabled=True):
    return Employer(
        id="example",
        name="Example",
        website="https://example.com",
        industry="Hardware",
        sources=[
            SourceConfig(
                id=source_id,
                ats="greenhouse",
                board_url="https://job-boards.greenhouse.io/example",
                enabled=enabled,
                verified_at=NOW if enabled else None,
                aliases=aliases or [],
            )
        ],
    )


def result(ids=("1", "2"), *, complete=True, source="example", details=True):
    return FetchResult(
        employer_id="example",
        source_id=source,
        items=[
            InventoryItem(
                source_job_id=value,
                title=f"Mechanical Intern {value}",
                source_url=f"https://example.com/jobs/{value}",
            )
            for value in ids
        ],
        postings=[
            Posting(
                source_job_id=value,
                title=f"Mechanical Intern {value}",
                source_url=f"https://example.com/jobs/{value}",
                description="Mechanical engineering students accepted",
            )
            for value in ids
        ]
        if details
        else [],
        complete=complete,
        expected_count=len(ids),
        pages_fetched=1,
    )


def initial():
    return reconcile(Catalog(), HealthDocument(), [result()], [employer()], NOW)[:2]


def by_posting(catalog, posting_id="1"):
    return next(job for job in catalog.jobs if job.posting.source_job_id == posting_id)


def test_discovery_and_unchanged_observation_preserve_identity_and_first_seen():
    catalog, health = initial()
    original = by_posting(catalog)
    updated, _, report = reconcile(
        catalog, health, [result(details=False)], [employer()], NOW + timedelta(hours=3)
    )
    job = by_posting(updated)
    assert job.id == original.id
    assert job.first_seen_at == NOW
    assert job.last_seen_at == NOW + timedelta(hours=3)
    assert job.details_checked_at == NOW
    assert job.last_verified_at == NOW
    assert job.availability == "open"
    assert report.discovered == 0
    assert original.last_seen_at == NOW


def test_two_complete_absences_require_three_hours_and_reopening_retains_history():
    catalog, health = initial()
    first_time = NOW + timedelta(hours=3)
    catalog, health, report = reconcile(catalog, health, [result(("2",))], [employer()], first_time)
    job = by_posting(catalog)
    assert job.availability == "suspected_closed"
    assert job.missing_count == 1
    assert report.closed == 0
    catalog, health, _ = reconcile(
        catalog, health, [result(("2",))], [employer()], first_time + timedelta(minutes=5)
    )
    assert by_posting(catalog).availability == "suspected_closed"
    catalog, health, report = reconcile(
        catalog, health, [result(("2",))], [employer()], first_time + timedelta(hours=3)
    )
    assert by_posting(catalog).availability == "closed"
    assert report.closed == 1
    catalog, _, report = reconcile(
        catalog, health, [result()], [employer()], first_time + timedelta(hours=6)
    )
    job = by_posting(catalog)
    assert job.availability == "open"
    assert job.first_seen_at == NOW
    assert job.reopened_at == first_time + timedelta(hours=6)
    assert job.closed_at is None
    assert job.missing_count == 0
    assert report.reopened == 1


def test_failed_or_incomplete_inventory_never_closes_known_jobs():
    catalog, health = initial()
    partial = result(("2",), complete=False)
    partial.errors = ["Pagination request failed"]
    updated, new_health, report = reconcile(
        catalog, health, [partial], [employer()], NOW + timedelta(hours=4)
    )
    assert by_posting(updated).availability == "open"
    assert by_posting(updated).missing_count == 0
    assert report.sources_complete == 0
    assert new_health.sources[0].last_success_at == NOW


def test_detail_error_retains_description_and_does_not_count_as_absence():
    catalog, health = initial()
    fetch = result(details=False)
    fetch.detail_errors = {"1": "HTTP 503"}
    updated, _, _ = reconcile(catalog, health, [fetch], [employer()], NOW + timedelta(hours=3))
    job = by_posting(updated)
    assert job.posting.description == by_posting(catalog).posting.description
    assert job.details_checked_at == NOW
    assert job.availability == "open"
    assert "failed" in job.status_reason
    assert job.last_seen_at == NOW + timedelta(hours=3)
    assert job.missing_count == 0


def test_unvisited_or_disabled_source_never_closes_but_freshness_expires():
    catalog, health = initial()
    updated, _, _ = reconcile(
        catalog, health, [], [employer(enabled=False)], NOW + timedelta(hours=49)
    )
    assert by_posting(updated).availability == "unverified"
    assert by_posting(updated).missing_count == 0
    assert by_posting(updated).closed_at is None


@pytest.mark.parametrize("previous,current", [(2, 0), (30, 10)])
def test_unexpected_inventory_drop_requires_consistent_later_confirmation(previous, current):
    catalog, health = initial()
    health.sources[0].last_complete_count = previous
    ids = tuple(str(value) for value in range(2, current + 2))
    fetch = result(ids)
    updated, health, report = reconcile(
        catalog, health, [fetch], [employer()], NOW + timedelta(hours=3)
    )
    assert by_posting(updated).availability == "open"
    assert report.sources_complete == 0
    assert health.sources[0].quarantined_count == current
    updated, health, report = reconcile(
        updated, health, [fetch], [employer()], NOW + timedelta(hours=4)
    )
    assert report.sources_complete == 0
    updated, health, report = reconcile(
        updated, health, [fetch], [employer()], NOW + timedelta(hours=6)
    )
    assert report.sources_complete == 1
    assert by_posting(updated).availability == "suspected_closed"
    assert health.sources[0].quarantined_count is None


def test_changing_drop_observation_resets_quarantine_confirmation():
    catalog, health = initial()
    health.sources[0].last_complete_count = 30
    _, health, _ = reconcile(
        catalog, health, [result(("2",))], [employer()], NOW + timedelta(hours=3)
    )
    updated, health, report = reconcile(
        catalog, health, [result(("2", "3"))], [employer()], NOW + timedelta(hours=6)
    )
    assert report.sources_complete == 0
    assert by_posting(updated).availability == "open"
    assert health.sources[0].quarantined_at == NOW + timedelta(hours=6)


def test_count_mismatch_is_not_a_complete_inventory():
    catalog, health = initial()
    fetch = result(("2",))
    fetch.expected_count = 500
    updated, health, report = reconcile(
        catalog, health, [fetch], [employer()], NOW + timedelta(hours=3)
    )
    assert by_posting(updated).availability == "open"
    assert report.sources_complete == 0
    assert "count mismatch" in health.sources[0].errors[0]


def test_explicit_source_migration_preserves_identity_and_health():
    catalog, health = initial()
    old_id = by_posting(catalog).id
    updated, health, report = reconcile(
        catalog,
        health,
        [result(source="replacement")],
        [employer("replacement", aliases=["example"])],
        NOW + timedelta(hours=3),
    )
    job = by_posting(updated)
    assert job.id == old_id
    assert job.source_id == "replacement"
    assert "greenhouse:example:1" in job.aliases
    assert len(updated.jobs) == 2
    assert len(health.sources) == 1
    assert health.sources[0].source_id == "replacement"
    assert report.discovered == 0


def test_explicit_closure_uses_employer_evidence():
    catalog, health = initial()
    fetch = result()
    fetch.postings[0].explicitly_closed = True
    fetch.postings[0].closure_reason = "Applications are closed"
    updated, _, report = reconcile(catalog, health, [fetch], [employer()], NOW + timedelta(hours=1))
    assert by_posting(updated).availability == "closed"
    assert by_posting(updated).status_reason == "Applications are closed"
    assert report.closed == 1


def test_minimal_detail_closure_preserves_known_role_facts():
    catalog, health = initial()
    fetch = result(details=False)
    fetch.postings = [
        Posting(
            source_job_id="1",
            title="Mechanical Intern 1",
            source_url="https://example.com/jobs/1",
            explicitly_closed=True,
            closure_reason="Employer detail endpoint returned 410",
        )
    ]
    updated, _, _ = reconcile(catalog, health, [fetch], [employer()], NOW + timedelta(hours=1))
    job = by_posting(updated)
    assert job.availability == "closed"
    assert job.posting.description == by_posting(catalog).posting.description
    assert job.posting.explicitly_closed is True
    assert job.details_checked_at == NOW + timedelta(hours=1)


def test_disabled_source_result_and_duplicate_result_are_rejected():
    with pytest.raises(ValueError, match="disabled"):
        reconcile(Catalog(), HealthDocument(), [result()], [employer(enabled=False)], NOW)
    with pytest.raises(ValueError, match="duplicate"):
        reconcile(Catalog(), HealthDocument(), [result(), result()], [employer()], NOW)


def test_new_inventory_shell_is_not_saved_before_details_are_available():
    catalog, _, report = reconcile(
        Catalog(), HealthDocument(), [result(details=False)], [employer()], NOW
    )
    assert catalog.jobs == []
    assert report.discovered == 0


def test_source_health_employer_mismatch_is_rejected():
    health = HealthDocument(sources=[SourceHealth(employer_id="wrong", source_id="example")])
    with pytest.raises(ValueError, match="another employer"):
        reconcile(Catalog(), health, [result()], [employer()], NOW)
