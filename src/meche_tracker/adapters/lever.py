"""Lever published postings, including global and EU boards."""

from datetime import datetime
from urllib.parse import quote, urlsplit

from meche_tracker.adapters.base import BaseAdapter, pay_range, records, required_text, workplace
from meche_tracker.http import SourceClient
from meche_tracker.models import FetchResult, InventoryItem, Job, Posting, SourceConfig
from meche_tracker.normalization import canonical_url, fingerprint, html_text, normalize_locations


class LeverAdapter(BaseAdapter):
    page_size = 100

    def fetch(
        self,
        http: SourceClient,
        source: SourceConfig,
        known: dict[str, Job],
        now: datetime,
        force_details: bool,
        result: FetchResult,
    ) -> None:
        eu = (urlsplit(source.board_url).hostname or "") == "jobs.eu.lever.co"
        domain = "api.eu.lever.co" if eu else "api.lever.co"
        base = f"https://{domain}/v0/postings/{quote(source.token, safe='')}"
        seen: set[str] = set()
        offset = 0
        for _ in range(self.max_pages):
            page = records(
                http.get_json(
                    base,
                    params={
                        "mode": "json",
                        "skip": offset,
                        "limit": self.page_size,
                    },
                )
            )
            result.pages_fetched += 1
            for job in page:
                item = InventoryItem(
                    source_job_id=required_text(job, "id"),
                    title=required_text(job, "text"),
                    source_url=canonical_url(required_text(job, "hostedUrl")),
                    fingerprint=fingerprint(job),
                )
                self.append_item(result, item, seen)
                if not self.needs_details(item, source, known, now, force_details):
                    continue
                try:
                    categories = job.get("categories") or {}
                    fragments = [job.get("descriptionPlain") or html_text(job.get("description"))]
                    for section in job.get("lists") or []:
                        fragments.extend([section.get("text"), html_text(section.get("content"))])
                    fragments.append(job.get("additionalPlain") or html_text(job.get("additional")))
                    primary_location = categories.get("location")
                    location_values = categories.get("allLocations") or [primary_location]
                    locations = []
                    for location in location_values:
                        locations.extend(
                            normalize_locations(
                                location,
                                country_hint=job.get("country")
                                if location == primary_location
                                else None,
                            )
                        )
                    result.postings.append(
                        Posting(
                            source_job_id=item.source_job_id,
                            title=item.title,
                            source_url=item.source_url,
                            apply_url=canonical_url(job.get("applyUrl") or item.source_url),
                            description="\n\n".join(x for x in fragments if x),
                            locations=locations,
                            employment_type=categories.get("commitment"),
                            workplace=workplace(job.get("workplaceType")),
                            pay=pay_range(job.get("salaryRange")),
                            fingerprint=item.fingerprint,
                        )
                    )
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    self.detail_failure(result, item, exc)
            if len(page) < self.page_size:
                result.expected_count = len(seen)
                result.complete = True
                return
            offset += len(page)
        raise ValueError("Lever page budget exhausted; inventory is incomplete")
