from datetime import UTC, datetime, timedelta

import pytest

from meche_tracker import pipeline
from meche_tracker.models import (
    Catalog,
    Classification,
    Employer,
    FetchResult,
    HealthDocument,
    InventoryItem,
    Location,
    OverrideDocument,
    Posting,
    SourceConfig,
)
from meche_tracker.store import json_text, load_state

NOW = datetime(2026, 10, 8, 16, tzinfo=UTC)


def prepare(root):
    (root / "data").mkdir()
    (root / "config/employers").mkdir(parents=True)
    for path, doc in [
        ("jobs.json", Catalog()),
        ("source_health.json", HealthDocument()),
        ("overrides.json", OverrideDocument()),
    ]:
        (root / "data" / path).write_text(json_text(doc))
    source = SourceConfig(
        id="test-main",
        ats="greenhouse",
        enabled=True,
        board_url="https://job-boards.greenhouse.io/test",
        verified_at=NOW,
    )
    employer = Employer(
        id="test",
        name="Test",
        website="https://example.com",
        industry="Industrial",
        sources=[source],
    )
    (root / "config/employers/test.json").write_text(json_text(employer))


def result():
    return FetchResult(
        employer_id="test",
        source_id="test-main",
        complete=True,
        expected_count=1,
        items=[
            InventoryItem(
                source_job_id="123", title="Mechanical Intern", source_url="https://example.com/123"
            )
        ],
        postings=[
            Posting(
                source_job_id="123",
                title="Mechanical Intern",
                source_url="https://example.com/123",
                description="CAD",
                locations=[Location(raw="Boston, MA", country="US", state="MA")],
            )
        ],
    )


def test_offline_end_to_end_repeat_dry_run_and_render(tmp_path, monkeypatch):
    prepare(tmp_path)
    monkeypatch.setattr(pipeline, "collect", lambda *args: [result()])
    first = pipeline.refresh(tmp_path, now=NOW)
    assert first.discovered == 1
    initial, _, _ = load_state(tmp_path)
    second = pipeline.refresh(tmp_path, now=NOW + timedelta(hours=3))
    assert second.discovered == 0
    repeated, _, _ = load_state(tmp_path)
    assert repeated.jobs[0].id == initial.jobs[0].id
    assert repeated.jobs[0].posting.content_hash == initial.jobs[0].posting.content_hash
    assert repeated.jobs[0].last_seen_at > initial.jobs[0].last_seen_at
    assert "Mechanical Intern" in (tmp_path / "README.md").read_text()
    before = (tmp_path / "data/jobs.json").read_bytes()
    pipeline.refresh(tmp_path, dry_run=True, now=NOW + timedelta(hours=3))
    assert (tmp_path / "data/jobs.json").read_bytes() == before
    pipeline.render(tmp_path, now=NOW + timedelta(hours=3))
    catalog, _, _ = load_state(tmp_path)
    assert len(catalog.jobs) == 1
    assert catalog.jobs[0].first_seen_at == NOW
    assert catalog.jobs[0].classification == Classification(
        decision="include",
        reasons=["mechanical title, eligible major, or physical engineering responsibilities"],
        specialties=["design-product"],
        opportunity_types=["internship"],
    )


def test_all_sources_fail_leaves_canonical_data_unchanged(tmp_path, monkeypatch):
    prepare(tmp_path)
    before = (tmp_path / "data/jobs.json").read_bytes()
    monkeypatch.setattr(
        pipeline,
        "collect",
        lambda *args: [FetchResult(employer_id="test", source_id="test-main", errors=["Timeout"])],
    )
    with pytest.raises(ValueError, match="previous publication retained"):
        pipeline.refresh(tmp_path, now=NOW)
    assert (tmp_path / "data/jobs.json").read_bytes() == before


def test_unknown_employer_is_error_before_collection(tmp_path):
    prepare(tmp_path)
    with pytest.raises(ValueError, match="unknown employer"):
        pipeline.refresh(tmp_path, ["not-configured"], now=NOW)


def test_cached_date_location_enrichment_keeps_verification_time(tmp_path, monkeypatch):
    prepare(tmp_path)
    observed = result()
    observed.postings[0].description = "Date Posted:\n2026-09-25\nDesign hardware."
    monkeypatch.setattr(pipeline, "collect", lambda *args: [observed])
    pipeline.refresh(tmp_path, now=NOW)
    catalog, _, _ = load_state(tmp_path)
    assert catalog.jobs[0].posting.employer_date == "2026-09-25"
    assert catalog.jobs[0].posting.employer_date_precision == "date"
    assert catalog.jobs[0].details_checked_at == NOW
    # Simulate a candidate saved by an older parser, then improve it offline.
    catalog.jobs[0].posting.locations = [Location(raw="Auckland, NZ")]
    (tmp_path / "data/jobs.json").write_text(json_text(catalog))
    pipeline.render(tmp_path, now=NOW + timedelta(hours=1))
    catalog, _, _ = load_state(tmp_path)
    assert catalog.jobs[0].details_checked_at == NOW
    assert catalog.jobs[0].posting.locations[0].country == "NZ"
    assert catalog.jobs[0].classification.decision == "exclude"
