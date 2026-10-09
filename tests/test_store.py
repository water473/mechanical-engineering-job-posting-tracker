from datetime import UTC, datetime
from pathlib import Path

import pytest

from meche_tracker.models import Catalog, HealthDocument, Job, Override, OverrideDocument, Posting
from meche_tracker.store import apply_overrides, json_text, load_state, publish

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


def initialize(root: Path):
    (root / "data").mkdir()
    (root / "data/jobs.json").write_text(json_text(Catalog()), encoding="utf-8")
    (root / "data/source_health.json").write_text(json_text(HealthDocument()), encoding="utf-8")
    (root / "data/overrides.json").write_text(json_text(OverrideDocument()), encoding="utf-8")


def sample_catalog():
    return Catalog(
        jobs=[
            Job(
                id="test",
                employer_id="example",
                employer_name="Example",
                industry="Hardware",
                source_id="example",
                ats="greenhouse",
                posting=Posting(
                    source_job_id="1", title="Intern", source_url="https://example.com/1"
                ),
                first_seen_at=NOW,
                last_seen_at=NOW,
                details_checked_at=NOW,
                last_verified_at=NOW,
            )
        ]
    )


def test_missing_and_corrupt_state_are_fatal_without_reset(tmp_path):
    with pytest.raises(ValueError, match="Missing state"):
        load_state(tmp_path)
    initialize(tmp_path)
    broken = tmp_path / "data/jobs.json"
    broken.write_text("{invalid", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid state"):
        load_state(tmp_path)
    assert broken.read_text() == "{invalid"


def test_incompatible_schema_is_rejected(tmp_path):
    initialize(tmp_path)
    (tmp_path / "data/jobs.json").write_text('{"schema_version": 2, "jobs": []}')
    with pytest.raises(ValueError, match="Invalid state"):
        load_state(tmp_path)


def test_publish_and_reload_preserve_clean_readable_json(tmp_path):
    initialize(tmp_path)
    catalog = sample_catalog()
    catalog.jobs[0].employer_name = "Éxample"
    publish(tmp_path, catalog, HealthDocument(), {"README.md": "# Tracker\n"})
    loaded, _, overrides = load_state(tmp_path)
    assert loaded == catalog
    assert overrides.overrides == []
    assert "Éxample" in (tmp_path / "data/jobs.json").read_text()
    assert (tmp_path / "README.md").read_text() == "# Tracker\n"


def test_dry_run_and_rejected_path_do_not_mutate(tmp_path):
    initialize(tmp_path)
    original = (tmp_path / "data/jobs.json").read_text()
    publish(tmp_path, sample_catalog(), HealthDocument(), {"README.md": "new"}, dry_run=True)
    assert not (tmp_path / "README.md").exists()
    assert (tmp_path / "data/jobs.json").read_text() == original
    for path in ["../outside", "/absolute", ".git/config", "data/overrides.json", "data/jobs.json"]:
        with pytest.raises(ValueError):
            publish(tmp_path, sample_catalog(), HealthDocument(), {path: "new"})
    assert (tmp_path / "data/jobs.json").read_text() == original


def test_publication_rolls_back_on_later_file_failure(tmp_path, monkeypatch):
    initialize(tmp_path)
    (tmp_path / "README.md").write_text("old")
    originals = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    import meche_tracker.store as storage

    real_replace = storage.os.replace
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated full disk")
        real_replace(source, target)

    monkeypatch.setattr(storage.os, "replace", fail_second)
    with pytest.raises(OSError, match="full disk"):
        publish(tmp_path, sample_catalog(), HealthDocument(), {"README.md": "new"})
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert not list(tmp_path.glob(".publish-*"))


def test_symlink_publication_cannot_escape_root(tmp_path):
    initialize(tmp_path)
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        publish(tmp_path, Catalog(), HealthDocument(), {"escape/outside.txt": "new"})


def test_obsolete_generated_views_are_removed_but_manual_docs_are_preserved(tmp_path):
    initialize(tmp_path)
    (tmp_path / "views/states").mkdir(parents=True)
    (tmp_path / "views/states/ma.md").write_text("obsolete job")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/development.md").write_text("documentation")
    publish(tmp_path, Catalog(), HealthDocument(), {"views/index.md": "new"})
    assert not (tmp_path / "views/states/ma.md").exists()
    assert (tmp_path / "views/index.md").read_text() == "new"
    assert (tmp_path / "docs/development.md").read_text() == "documentation"


def test_overrides_apply_validated_facts_without_editing_history():
    catalog = sample_catalog()
    overrides = OverrideDocument(
        overrides=[
            Override(
                job_id="test",
                reason="Reviewed eligibility wording",
                reviewed_at=NOW,
                posting_fields={"title": "Mechanical Engineering Intern"},
                classification_fields={"decision": "include", "housing": True},
            )
        ]
    )
    updated = apply_overrides(catalog, overrides)
    assert updated.jobs[0].posting.title == "Mechanical Engineering Intern"
    assert updated.jobs[0].classification.housing is True
    assert updated.jobs[0].last_verified_at == NOW
    assert catalog.jobs[0].posting.title == "Intern"


@pytest.mark.parametrize("field", ["last_verified_at", "first_seen_at", "source_job_id"])
def test_overrides_cannot_fabricate_identity_or_verification(field):
    overrides = OverrideDocument(
        overrides=[
            Override(
                job_id="test", reason="review", reviewed_at=NOW, posting_fields={field: "edited"}
            )
        ]
    )
    with pytest.raises(ValueError, match="disallowed fields"):
        apply_overrides(sample_catalog(), overrides)


def test_unknown_override_job_and_naive_review_time_are_rejected():
    overrides = OverrideDocument(
        overrides=[Override(job_id="missing", reason="review", reviewed_at=NOW)]
    )
    with pytest.raises(ValueError, match="unknown job"):
        apply_overrides(sample_catalog(), overrides)
    with pytest.raises(ValueError, match="timezone"):
        overrides.overrides[0].reviewed_at = NOW.replace(tzinfo=None)
