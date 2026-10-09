"""Greenhouse's documented unauthenticated Job Board API."""

from datetime import datetime
from urllib.parse import quote

from meche_tracker.adapters.base import BaseAdapter, employer_date, records, required_text
from meche_tracker.http import SourceClient, SourceHTTPError
from meche_tracker.models import FetchResult, InventoryItem, Job, Posting, SourceConfig
from meche_tracker.normalization import (
    canonical_url,
    description_locations,
    fingerprint,
    html_text,
    normalize_locations,
)


class GreenhouseAdapter(BaseAdapter):
    def fetch(
        self,
        http: SourceClient,
        source: SourceConfig,
        known: dict[str, Job],
        now: datetime,
        force_details: bool,
        result: FetchResult,
    ) -> None:
        base = f"https://boards-api.greenhouse.io/v1/boards/{quote(source.token, safe='')}/jobs"
        payload = http.get_json(base, params={"content": "true"})
        jobs = records(payload, "jobs")
        result.pages_fetched = 1
        total = payload.get("meta", {}).get("total")
        if total is not None and (not isinstance(total, int) or total < 0):
            raise ValueError("Invalid Greenhouse total")
        result.expected_count = total if total is not None else len(jobs)
        seen: set[str] = set()
        candidates = []
        for job in jobs:
            job_id = required_text(job, "id")
            title = required_text(job, "title")
            url = canonical_url(
                job.get("absolute_url")
                or f"https://job-boards.greenhouse.io/{source.token}/jobs/{job_id}"
            )
            item = InventoryItem(
                source_job_id=job_id, title=title, source_url=url, fingerprint=fingerprint(job)
            )
            self.append_item(result, item, seen)
            if self.needs_details(item, source, known, now, force_details):
                candidates.append((item, job))
        if len(seen) != result.expected_count:
            raise ValueError("Greenhouse inventory count does not match meta.total")
        result.complete = True
        for item, listing in candidates:
            try:
                detail = http.get_json(f"{base}/{quote(item.source_job_id, safe='')}")
                if not isinstance(detail, dict) or str(detail.get("id")) != item.source_job_id:
                    raise ValueError("Greenhouse detail identity mismatch")
                combined = {**listing, **detail}
                description = html_text(combined.get("content"))
                locations = normalize_locations(combined.get("location"))
                if not locations or all(location.country is None for location in locations):
                    locations = description_locations(description) or locations
                result.postings.append(
                    Posting(
                        source_job_id=item.source_job_id,
                        title=required_text(combined, "title"),
                        source_url=item.source_url,
                        apply_url=item.source_url,
                        requisition_id=str(combined["requisition_id"])
                        if combined.get("requisition_id")
                        else None,
                        description=description,
                        locations=locations,
                        source_updated_at=combined.get("updated_at"),
                        fingerprint=item.fingerprint,
                        **employer_date(combined.get("first_published"), "first_published"),
                    )
                )
            except (SourceHTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
                self.detail_failure(result, item, exc)
                # An inventory/detail race is possible; retain facts for reconciliation.
                if isinstance(exc, SourceHTTPError) and exc.status_code in {404, 410}:
                    result.postings.append(self.closed_posting(item, "Employer detail removed"))
