"""Validated JSON state and staged publication with rollback on local failures."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from pydantic import ValidationError

from .models import Catalog, Classification, HealthDocument, OverrideDocument, Posting

POSTING_OVERRIDE_FIELDS = {
    "title",
    "source_url",
    "apply_url",
    "requisition_id",
    "description",
    "locations",
    "workplace",
    "employment_type",
    "employer_date",
    "employer_date_kind",
    "employer_date_precision",
    "employer_date_raw",
    "pay",
    "listed",
}
CLASSIFICATION_OVERRIDE_FIELDS = set(Classification.model_fields)


def _read(path: Path, model: type):
    if not path.is_file():
        raise ValueError(f"Missing state file: {path}")
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as error:
        raise ValueError(f"Invalid state file {path}: {error}") from error


def _validate_overrides(overrides: OverrideDocument) -> None:
    seen: set[str] = set()
    for override in overrides.overrides:
        if override.job_id in seen:
            raise ValueError(f"Duplicate override for {override.job_id}")
        seen.add(override.job_id)
        if override.reviewed_at.tzinfo is None:
            raise ValueError(f"Override {override.job_id} requires an aware review timestamp")
        invalid_posting = set(override.posting_fields) - POSTING_OVERRIDE_FIELDS
        invalid_classification = (
            set(override.classification_fields) - CLASSIFICATION_OVERRIDE_FIELDS
        )
        if invalid_posting or invalid_classification:
            raise ValueError(
                f"Override {override.job_id} contains disallowed fields: "
                f"{sorted(invalid_posting | invalid_classification)}"
            )


def load_state(root: Path) -> tuple[Catalog, HealthDocument, OverrideDocument]:
    """Load all three state documents. Missing or corrupt state is fatal."""
    catalog = _read(root / "data/jobs.json", Catalog)
    health = _read(root / "data/source_health.json", HealthDocument)
    overrides = _read(root / "data/overrides.json", OverrideDocument)
    if len({job.id for job in catalog.jobs}) != len(catalog.jobs):
        raise ValueError("Catalog contains duplicate job IDs")
    if len({source.source_id for source in health.sources}) != len(health.sources):
        raise ValueError("Source health contains duplicate source IDs")
    _validate_overrides(overrides)
    return catalog, health, overrides


def apply_overrides(catalog: Catalog, overrides: OverrideDocument) -> Catalog:
    """Apply reviewed facts after automatic classification, preserving tracker times."""
    _validate_overrides(overrides)
    updated = catalog.model_copy(deep=True)
    jobs = {job.id: job for job in updated.jobs}
    for override in overrides.overrides:
        if override.job_id not in jobs:
            raise ValueError(f"Override references unknown job {override.job_id}")
        job = jobs[override.job_id]
        posting_values = job.posting.model_dump()
        posting_values.update(override.posting_fields)
        job.posting = Posting.model_validate(posting_values)
        classification_values = job.classification.model_dump()
        classification_values.update(override.classification_fields)
        job.classification = Classification.model_validate(classification_values)
    return updated


def json_text(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _safe_relative(path: str) -> Path:
    relative = Path(path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"..", ".git", ".venv"} for part in relative.parts)
    ):
        raise ValueError(f"Unsafe publication path {path!r}")
    if relative == Path("data/overrides.json"):
        raise ValueError("Generated output must not overwrite reviewed overrides")
    return relative


def publish(
    root: Path,
    catalog: Catalog,
    health: HealthDocument,
    outputs: dict[str, str],
    dry_run: bool = False,
) -> None:
    """Validate all files first, then replace each atomically with rollback.

    A Git commit supplies the cross-file publication boundary in automation.
    Local failures restore the prior dataset and views before propagating.
    """
    catalog = Catalog.model_validate(catalog.model_dump())
    health = HealthDocument.model_validate(health.model_dump())
    catalog.jobs.sort(key=lambda job: job.id)
    health.sources.sort(key=lambda source: source.source_id)
    if len({job.id for job in catalog.jobs}) != len(catalog.jobs):
        raise ValueError("Catalog contains duplicate job IDs")
    if len({source.source_id for source in health.sources}) != len(health.sources):
        raise ValueError("Source health contains duplicate source IDs")
    canonical = {"data/jobs.json", "data/source_health.json"}
    if canonical.intersection(outputs):
        raise ValueError("Generated outputs cannot replace canonical state documents")
    content = {
        "data/jobs.json": json_text(catalog),
        "data/source_health.json": json_text(health),
        **outputs,
    }
    root = root.resolve()
    for relative, value in content.items():
        _safe_relative(relative)
        if not isinstance(value, str):
            raise TypeError(f"Publication content for {relative} must be text")
        target = root / relative
        if not target.resolve().is_relative_to(root):
            raise ValueError(f"Publication target escapes project root: {relative}")
    # All view Markdown is generated. Remove obsolete facets when their last
    # eligible job disappears so stale pages cannot continue advertising it.
    obsolete = sorted(
        path
        for path in (root / "views").rglob("*.md")
        if path.relative_to(root).as_posix() not in content
    )
    for target in obsolete:
        if not target.resolve().is_relative_to(root):
            raise ValueError(f"Obsolete view escapes project root: {target}")
    if dry_run:
        return
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".publish-", dir=root) as temporary:
        stage = Path(temporary)
        backups: dict[Path, Path | None] = {}
        installed: list[Path] = []
        for index, (relative, value) in enumerate(sorted(content.items())):
            target = root / relative
            candidate = stage / f"candidate-{index}"
            with candidate.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            backup = None
            if target.exists():
                if not target.is_file():
                    raise ValueError(f"Publication target is not a file: {relative}")
                backup = stage / f"backup-{index}"
                shutil.copyfile(target, backup)
            backups[target] = backup
        for index, target in enumerate(obsolete):
            backup = stage / f"obsolete-{index}"
            shutil.copyfile(target, backup)
            backups[target] = backup
        try:
            for index, relative in enumerate(sorted(content)):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage / f"candidate-{index}", target)
                installed.append(target)
            for target in obsolete:
                target.unlink()
                installed.append(target)
        except BaseException:
            for target in reversed(installed):
                backup = backups[target]
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(backup, target)
            raise
