"""Shared collection lifecycle and detail refresh policy."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx

from meche_tracker.http import SourceClient, SourceHTTPError
from meche_tracker.models import (
    Employer,
    FetchResult,
    InventoryItem,
    Job,
    Pay,
    Posting,
    SourceConfig,
    Workplace,
)
from meche_tracker.normalization import is_student_candidate, parse_employer_date


def required_text(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
        raise ValueError(f"Missing or invalid {key}")
    return str(value).strip()


def records(value: Any, key: str | None = None) -> list[dict[str, Any]]:
    if key is not None:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"Missing {key} response envelope")
        value = value[key]
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("Expected a list of job objects")
    return value


def workplace(value: Any) -> Workplace:
    key = str(value or "").replace("-", "").replace(" ", "").lower()
    return {"onsite": "onsite", "remote": "remote", "hybrid": "hybrid"}.get(key, "unknown")


def employer_date(raw: Any, kind: str) -> dict[str, Any]:
    parsed, precision = parse_employer_date(raw)
    return {
        "employer_date": parsed,
        "employer_date_kind": kind if parsed else None,
        "employer_date_precision": precision,
        "employer_date_raw": str(raw) if raw is not None else None,
    }


def pay_range(value: Any) -> Pay | None:
    if not isinstance(value, dict):
        return None
    unit = str(value.get("interval") or value.get("unitText") or "").lower().strip()
    unit = {
        "hourly": "hour",
        "yearly": "year",
        "annual": "year",
        "annually": "year",
        "1 year": "year",
        "1 hour": "hour",
        "1 month": "month",
        "1 week": "week",
    }.get(unit, unit)
    minimum = value.get("min", value.get("minValue"))
    maximum = value.get("max", value.get("maxValue"))
    return Pay(
        minimum=minimum if isinstance(minimum, (int, float)) else None,
        maximum=maximum if isinstance(maximum, (int, float)) else None,
        currency=value.get("currency") or value.get("currencyCode"),
        unit=unit if unit in {"hour", "year", "month", "week"} else None,
    )


class BaseAdapter:
    max_pages = 250

    def __init__(self, client: httpx.Client | None = None):
        self.client = client

    def collect(
        self,
        employer: Employer,
        source: SourceConfig,
        existing: list[Job],
        now: datetime,
        force_details: bool = False,
    ) -> FetchResult:
        result = FetchResult(employer_id=employer.id, source_id=source.id)
        http = SourceClient(self.client)
        known = {
            job.posting.source_job_id: job
            for job in existing
            if job.employer_id == employer.id
            and job.ats == source.ats
            and job.source_id in {source.id, *source.aliases}
        }
        try:
            self.fetch(http, source, known, now, force_details, result)
        except (SourceHTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
            result.complete = False
            result.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            http.close()
        return result

    def fetch(
        self,
        http: SourceClient,
        source: SourceConfig,
        known: dict[str, Job],
        now: datetime,
        force_details: bool,
        result: FetchResult,
    ) -> None:
        raise NotImplementedError

    @staticmethod
    def needs_details(
        item: InventoryItem,
        source: SourceConfig,
        known: dict[str, Job],
        now: datetime,
        force: bool,
    ) -> bool:
        previous = known.get(item.source_job_id)
        if not previous and not is_student_candidate(item.title, source):
            return False
        return bool(
            force
            or previous is None
            or previous.details_checked_at is None
            or now - previous.details_checked_at >= timedelta(hours=24)
            or item.fingerprint != previous.posting.fingerprint
            or item.listed != previous.posting.listed
            or previous.availability in {"closed", "suspected_closed", "unverified"}
        )

    @staticmethod
    def append_item(result: FetchResult, item: InventoryItem, seen: set[str]) -> None:
        if item.source_job_id in seen:
            raise ValueError(f"Duplicate source posting ID: {item.source_job_id}")
        seen.add(item.source_job_id)
        result.items.append(item)

    @staticmethod
    def detail_failure(result: FetchResult, item: InventoryItem, exc: Exception) -> None:
        result.detail_errors[item.source_job_id] = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def closed_posting(item: InventoryItem, reason: str) -> Posting:
        return Posting(
            source_job_id=item.source_job_id,
            title=item.title,
            source_url=item.source_url,
            fingerprint=item.fingerprint,
            explicitly_closed=True,
            closure_reason=reason,
        )
