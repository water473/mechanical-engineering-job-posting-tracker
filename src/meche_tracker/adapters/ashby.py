"""Ashby's public board feed; direct-link-only postings remain unlisted."""

from datetime import datetime
from urllib.parse import quote, urlsplit

from meche_tracker.adapters.base import (
    BaseAdapter,
    employer_date,
    pay_range,
    records,
    required_text,
    workplace,
)
from meche_tracker.http import SourceClient
from meche_tracker.models import FetchResult, InventoryItem, Job, Posting, SourceConfig
from meche_tracker.normalization import canonical_url, fingerprint, html_text, normalize_locations


class AshbyAdapter(BaseAdapter):
    def fetch(
        self,
        http: SourceClient,
        source: SourceConfig,
        known: dict[str, Job],
        now: datetime,
        force_details: bool,
        result: FetchResult,
    ) -> None:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{quote(source.token, safe='')}"
        payload = http.get_json(url, params={"includeCompensation": "true"})
        jobs = records(payload, "jobs")
        result.pages_fetched = 1
        result.expected_count = len(jobs)
        seen: set[str] = set()
        for job in jobs:
            url = canonical_url(required_text(job, "jobUrl"))
            job_id = str(job.get("id") or urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
            if not isinstance(job.get("isListed"), bool):
                raise ValueError("Ashby isListed must be explicit")
            item = InventoryItem(
                source_job_id=job_id,
                title=required_text(job, "title"),
                source_url=url,
                fingerprint=fingerprint(job),
                listed=job["isListed"],
            )
            self.append_item(result, item, seen)
            if not self.needs_details(item, source, known, now, force_details):
                continue
            try:
                locations = [{"location": job.get("location"), "address": job.get("address")}]
                locations.extend(job.get("secondaryLocations") or [])
                compensation = job.get("compensation") or {}
                salary = next(
                    (
                        component
                        for component in compensation.get("summaryComponents", [])
                        if component.get("compensationType") == "Salary"
                    ),
                    None,
                )
                result.postings.append(
                    Posting(
                        source_job_id=job_id,
                        title=item.title,
                        source_url=url,
                        apply_url=canonical_url(job.get("applyUrl") or url),
                        description=job.get("descriptionPlain")
                        or html_text(job.get("descriptionHtml")),
                        locations=normalize_locations(locations),
                        workplace=workplace(
                            job.get("workplaceType")
                            or ("remote" if job.get("isRemote") is True else None)
                        ),
                        employment_type=job.get("employmentType"),
                        pay=pay_range(salary),
                        fingerprint=item.fingerprint,
                        listed=item.listed,
                        **employer_date(job.get("publishedAt"), "last_published"),
                    )
                )
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                self.detail_failure(result, item, exc)
        result.complete = True
