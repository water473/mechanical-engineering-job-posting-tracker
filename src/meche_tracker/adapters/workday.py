"""Employer-scoped Workday CXS adapter (an unofficial public-site interface)."""

from datetime import datetime
from urllib.parse import quote

from meche_tracker.adapters.base import (
    BaseAdapter,
    employer_date,
    records,
    required_text,
    workplace,
)
from meche_tracker.http import SourceClient, SourceHTTPError
from meche_tracker.models import FetchResult, InventoryItem, Job, Posting, SourceConfig
from meche_tracker.normalization import canonical_url, fingerprint, html_text, normalize_locations


class WorkdayAdapter(BaseAdapter):
    page_size = 20

    def fetch(
        self,
        http: SourceClient,
        source: SourceConfig,
        known: dict[str, Job],
        now: datetime,
        force_details: bool,
        result: FetchResult,
    ) -> None:
        tenant = quote(source.workday_tenant, safe="")
        site = quote(source.workday_site, safe="")
        api = f"{source.origin}/wday/cxs/{tenant}/{site}"
        seen: set[str] = set()
        observed: dict[str, InventoryItem] = {}
        ambiguous_ids: set[str] = set()
        offset = 0
        candidates = []
        for _ in range(self.max_pages):
            query = {
                "limit": self.page_size,
                "offset": offset,
                "searchText": "",
                "appliedFacets": {},
            }
            response = http.post_json(f"{api}/jobs", query)
            page = records(response, "jobPostings")
            if any(not self._has_public_fields(listing) for listing in page):
                # Workday occasionally returns an opaque row containing only
                # bulletFields. Re-read once to recover transient partial rows.
                # Never manufacture a public URL from an internal requisition ID.
                retry_response = http.post_json(f"{api}/jobs", query)
                retry_page = records(retry_response, "jobPostings")
                if len(retry_page) != len(page):
                    raise ValueError("Workday page changed while retrying incomplete records")
                response, page = retry_response, retry_page
            result.pages_fetched += 1
            total = response.get("total")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise ValueError("Workday response has no valid total")
            # Some tenants report the actual total only on the first page and use
            # zero on every later page, even when returning a full page of jobs.
            if result.expected_count is not None and total not in {0, result.expected_count}:
                raise ValueError("Workday total changed during pagination; inventory incomplete")
            if result.expected_count is None:
                result.expected_count = total
            total = result.expected_count
            for index, listing in enumerate(page):
                if not self._has_public_fields(listing):
                    result.errors.append(
                        f"Workday record at offset {offset + index} has no public title/URL; "
                        "inventory remains incomplete"
                    )
                    continue
                path = required_text(listing, "externalPath")
                if not path.startswith("/job/") or any(c in path for c in ("?", "#", "\\")):
                    raise ValueError("Workday returned an invalid externalPath")
                slug = path.rsplit("/", 1)[-1]
                # Preserve posting suffixes (-2, etc.); requisition ID is a separate field.
                job_id = slug.rsplit("_", 1)[-1] if "_" in slug else path
                title = required_text(listing, "title")
                stable = {key: value for key, value in listing.items() if key != "postedOn"}
                item = InventoryItem(
                    source_job_id=job_id,
                    title=title,
                    source_url=canonical_url(
                        f"{source.origin}/{quote(source.locale, safe='')}/{site}{path}"
                    ),
                    detail_path=path,
                    fingerprint=fingerprint(stable),
                )
                if item.source_job_id in observed:
                    previous = observed[item.source_job_id]
                    exact = item == previous
                    result.errors.append(
                        f"Duplicate Workday posting ID {item.source_job_id} "
                        f"({'exact row' if exact else 'changed row'}); inventory remains incomplete"
                    )
                    if item.source_url != previous.source_url:
                        ambiguous_ids.add(item.source_job_id)
                    continue
                self.append_item(result, item, seen)
                observed[item.source_job_id] = item
                if self.needs_details(item, source, known, now, force_details):
                    candidates.append((item, listing))
            received_count = offset + len(page)
            if received_count == total:
                result.complete = len(seen) == total
                break
            if not page or received_count > total:
                raise ValueError("Workday ended before its complete inventory was collected")
            offset += len(page)
        else:
            raise ValueError("Workday page budget exhausted; inventory is incomplete")
        for item, listing in candidates:
            if item.source_job_id in ambiguous_ids:
                result.detail_errors[item.source_job_id] = (
                    "Posting ID has multiple public URLs; details require a later stable inventory"
                )
                continue
            try:
                response = http.get_json(f"{api}{item.detail_path}")
                detail = response.get("jobPostingInfo") if isinstance(response, dict) else None
                if not isinstance(detail, dict) or not detail.get("title"):
                    raise ValueError("Missing Workday jobPostingInfo detail envelope")
                # Do not interpret startDate: its semantics are not consistent across sites.
                date_raw = detail.get("datePosted")
                date_fields = employer_date(date_raw, "date_posted")
                if date_raw is None:
                    date_fields["employer_date_raw"] = detail.get("postedOn") or listing.get(
                        "postedOn"
                    )
                country = detail.get("country")
                locations = normalize_locations(
                    detail.get("jobRequisitionLocation")
                    or detail.get("location")
                    or listing.get("locationsText"),
                    country_hint=country,
                )
                locations.extend(normalize_locations(detail.get("additionalLocations") or []))
                closed = detail.get("canApply") is False or detail.get("posted") is False
                result.postings.append(
                    Posting(
                        source_job_id=item.source_job_id,
                        title=required_text(detail, "title"),
                        source_url=item.source_url,
                        apply_url=item.source_url + "/apply",
                        requisition_id=str(detail["jobReqId"]) if detail.get("jobReqId") else None,
                        description=html_text(
                            detail.get("jobDescription") or detail.get("description")
                        ),
                        locations=locations,
                        employment_type=detail.get("timeType"),
                        workplace=workplace(detail.get("remoteType")),
                        fingerprint=item.fingerprint,
                        explicitly_closed=closed,
                        closure_reason="Employer says applications are unavailable"
                        if closed
                        else None,
                        **date_fields,
                    )
                )
            except (SourceHTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
                self.detail_failure(result, item, exc)
                if isinstance(exc, SourceHTTPError) and exc.status_code in {404, 410}:
                    result.postings.append(self.closed_posting(item, "Employer detail removed"))

    @staticmethod
    def _has_public_fields(listing: dict) -> bool:
        return all(
            isinstance(listing.get(key), str) and listing[key].strip()
            for key in ("externalPath", "title")
        )
