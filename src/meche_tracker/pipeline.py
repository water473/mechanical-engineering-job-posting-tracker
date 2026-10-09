"""One orchestration path for manual, scheduled, and dry-run collection."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .adapters import collect_source
from .classification import classify, extract_pay
from .models import Catalog, Employer, FetchResult, OverrideDocument, RunReport, utc_now
from .normalization import (
    content_hash,
    description_locations,
    normalize_locations,
    parse_employer_date,
)
from .reconcile import reconcile
from .registry import load_employers
from .render import build_outputs
from .store import apply_overrides, load_state, publish

LOGGER = logging.getLogger(__name__)


@contextmanager
def writer_lock(root: Path):
    """An OS advisory lock releases even if a refresh is interrupted."""
    import fcntl

    path = root / ".tracker.lock"
    with path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another refresh or render is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def collect(
    employers: list[Employer],
    catalog: Catalog,
    now: datetime,
    force_details: bool = False,
) -> list[FetchResult]:
    # Leave one per-source budget before the overall thirty-minute ceiling.
    deadline = time.monotonic() + 25 * 60

    def worker(employer: Employer) -> list[FetchResult]:
        results = []
        for source in employer.sources:
            if not source.enabled:
                continue
            if time.monotonic() >= deadline:
                results.append(
                    FetchResult(
                        employer_id=employer.id,
                        source_id=source.id,
                        errors=["Overall collection budget exhausted"],
                    )
                )
                continue
            LOGGER.info("Collecting %s (%s)", employer.name, source.ats)
            try:
                result = collect_source(employer, source, catalog.jobs, now, force_details)
            except Exception as exc:
                # Unexpected source-specific failures must not erase another employer's data.
                LOGGER.exception("Collection failed for %s", source.id)
                result = FetchResult(
                    employer_id=employer.id,
                    source_id=source.id,
                    errors=[f"{type(exc).__name__}: {exc}"],
                )
            LOGGER.info(
                "%s: %d inventory / %d details, complete=%s, errors=%d",
                source.id,
                len(result.items),
                len(result.postings),
                result.complete,
                len(result.errors) + len(result.detail_errors),
            )
            results.append(result)
        return results

    results = []
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="employer") as executor:
        futures = [executor.submit(worker, employer) for employer in employers]
        for future in as_completed(futures):
            results.extend(future.result())
    return sorted(results, key=lambda result: result.source_id)


def classify_catalog(catalog: Catalog, overrides: OverrideDocument, now: datetime) -> Catalog:
    facts_only = overrides.model_copy(deep=True)
    for override in facts_only.overrides:
        override.classification_fields = {}
    classified = apply_overrides(catalog, facts_only)
    for job in classified.jobs:
        # Parser improvements also apply to cached facts without fabricating a fresh fetch.
        normalized = []
        for location in job.posting.locations:
            parsed = normalize_locations(location.raw, country_hint=location.country)
            if parsed:
                for value in parsed:
                    normalized.append(
                        location.model_copy(
                            update={
                                key: field
                                for key, field in value.model_dump().items()
                                if field is not None
                            }
                        )
                    )
            else:
                normalized.append(location)
        job.posting.locations = normalized
        if not any(location.country for location in job.posting.locations):
            explicit = description_locations(job.posting.description)
            if explicit:
                job.posting.locations = explicit
        if job.posting.employer_date is None:
            stated = re.search(
                r"(?im)^\s*Date Posted:\s*(20\d{2}-\d{2}-\d{2})\b",
                job.posting.description or "",
            )
            if stated:
                parsed_date, precision = parse_employer_date(stated.group(1))
                if parsed_date:
                    job.posting.employer_date = parsed_date
                    job.posting.employer_date_raw = stated.group(1)
                    job.posting.employer_date_precision = precision
                    job.posting.employer_date_kind = "date_posted"
        job.classification = classify(job.posting, today=now.date())
        if job.posting.pay is None and job.posting.description:
            job.posting.pay = extract_pay(job.posting.description)
    final = apply_overrides(classified, overrides)
    for job in final.jobs:
        job.posting.content_hash = content_hash(
            job.posting.model_dump(
                mode="json", exclude={"content_hash", "fingerprint", "source_updated_at"}
            )
        )
    return final


def refresh(
    root: Path,
    employer_ids: list[str] | None = None,
    *,
    dry_run: bool = False,
    force_details: bool = False,
    now: datetime | None = None,
) -> RunReport:
    now = now or utc_now()
    with writer_lock(root):
        employers = load_employers(root)
        catalog, health, overrides = load_state(root)
        ids = set(employer_ids or [])
        unknown = ids - {employer.id for employer in employers}
        if unknown:
            raise ValueError(f"unknown employer IDs: {', '.join(sorted(unknown))}")
        selected = [employer for employer in employers if not ids or employer.id in ids]
        if not any(source.enabled for employer in selected for source in employer.sources):
            raise ValueError("no enabled sources selected")
        results = collect(selected, catalog, now, force_details)
        if not any(result.complete for result in results):
            errors = [f"{r.source_id}: {'; '.join(r.errors)}" for r in results]
            raise ValueError(
                "all selected sources failed; previous publication retained: " + " | ".join(errors)
            )
        known = {
            (job.employer_id, job.source_id, job.posting.source_job_id) for job in catalog.jobs
        }
        for employer in employers:
            for source in employer.sources:
                known.update(
                    (job.employer_id, source.id, job.posting.source_job_id)
                    for job in catalog.jobs
                    if job.employer_id == employer.id
                    and job.ats == source.ats
                    and job.source_id in source.aliases
                )
        for result in results:
            # Preserve full inventories for completeness/closure; persist only useful candidates.
            result.postings = [
                posting
                for posting in result.postings
                if (result.employer_id, result.source_id, posting.source_job_id) in known
                or classify(posting, today=now.date()).decision != "exclude"
            ]
        catalog, health, report = reconcile(catalog, health, results, employers, now)
        catalog = classify_catalog(catalog, overrides, now)
        report.reviewed = sum(job.classification.decision == "review" for job in catalog.jobs)
        outputs = build_outputs(catalog, health, employers, now)
        publish(root, catalog, health, outputs, dry_run=dry_run)
        return report


def render(root: Path, *, now: datetime | None = None) -> None:
    now = now or utc_now()
    with writer_lock(root):
        employers = load_employers(root)
        catalog, health, overrides = load_state(root)
        catalog = classify_catalog(catalog, overrides, now)
        publish(root, catalog, health, build_outputs(catalog, health, employers, now))
