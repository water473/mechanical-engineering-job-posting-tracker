"""ATS fixtures exercise failure isolation, exhaustive pagination and source semantics."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from meche_tracker.adapters import (
    AshbyAdapter,
    GreenhouseAdapter,
    LeverAdapter,
    WorkdayAdapter,
    collect_source,
)
from meche_tracker.http import SourceClient, SourceHTTPError
from meche_tracker.models import Employer, Job, SourceConfig
from meche_tracker.normalization import (
    canonical_url,
    description_locations,
    html_text,
    is_student_candidate,
    normalize_locations,
    parse_employer_date,
)

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


def configuration(ats):
    url = {
        "greenhouse": "https://job-boards.greenhouse.io/example",
        "lever": "https://jobs.lever.co/example",
        "ashby": "https://jobs.ashbyhq.com/example",
        "workday": "https://example.wd1.myworkdayjobs.com/en-US/Careers",
    }[ats]
    source = SourceConfig(id="careers", ats=ats, board_url=url)
    employer = Employer(
        id="example",
        name="Example",
        website="https://example.com",
        industry="Manufacturing",
        sources=[source],
    )
    return employer, source


def prior(posting, ats="workday", checked=None):
    return Job(
        id=f"example:careers:{posting.source_job_id}",
        employer_id="example",
        employer_name="Example",
        industry="Manufacturing",
        source_id="careers",
        ats=ats,
        posting=posting,
        first_seen_at=NOW - timedelta(days=2),
        last_seen_at=NOW,
        details_checked_at=checked or NOW - timedelta(hours=1),
        availability="open",
    )


def transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def gh_job(job_id=123):
    return {
        "id": job_id,
        "title": "Mechanical Engineering Intern Summer 2027",
        "updated_at": "2026-10-08T08:00:00Z",
        "location": {"name": "Boston, MA"},
        "absolute_url": f"https://job-boards.greenhouse.io/example/jobs/{job_id}?utm_source=x",
        "content": "&lt;p&gt;Pursuing a mechanical engineering degree.&lt;/p&gt;",
    }


def lever_job(job_id="a"):
    return {
        "id": job_id,
        "text": "Mechanical Engineering Intern",
        "categories": {
            "location": "Boston, MA",
            "allLocations": ["Boston, MA", "London, UK"],
            "commitment": "Intern",
            "team": "Hardware",
        },
        "country": "US",
        "descriptionPlain": "Design hardware.",
        "lists": [{"text": "Requirements", "content": "<li>Mechanical degree</li>"}],
        "additionalPlain": "Return to school afterwards.",
        "hostedUrl": f"https://jobs.lever.co/example/{job_id}",
        "applyUrl": f"https://jobs.lever.co/example/{job_id}/apply",
        "workplaceType": "on-site",
        "salaryRange": {"min": 25, "max": 35, "currency": "USD", "interval": "hour"},
    }


def ashby_job(listed=True):
    return {
        "id": "ashby-1",
        "title": "Mechanical Engineering Intern",
        "isListed": listed,
        "location": "Boston",
        "address": {
            "postalAddress": {
                "addressLocality": "Boston",
                "addressRegion": "Massachusetts",
                "addressCountry": "USA",
            }
        },
        "secondaryLocations": [{"location": "London", "address": {"addressCountry": "GB"}}],
        "employmentType": "Intern",
        "publishedAt": "2026-10-01T08:00:00Z",
        "descriptionPlain": "Build hardware.",
        "workplaceType": "OnSite",
        "jobUrl": "https://jobs.ashbyhq.com/example/ashby-1",
        "applyUrl": "https://jobs.ashbyhq.com/example/ashby-1/application",
    }


def wd_listing(number=1, title="Mechanical Engineering Co-op", posted="Posted Today"):
    return {
        "title": title,
        "externalPath": f"/job/Boston/Mechanical-Co-op_JR{number}-2",
        "locationsText": "Boston, MA",
        "postedOn": posted,
        "bulletFields": [f"JR{number}"],
    }


def wd_detail(can_apply=True):
    return {
        "jobPostingInfo": {
            "title": "Mechanical Engineering Co-op",
            "canApply": can_apply,
            "posted": True,
            "jobReqId": "JR1",
            "jobDescription": "<p>Mechanical engineering student.</p>",
            "location": "Boston, MA",
            "country": {"descriptor": "United States of America"},
            "additionalLocations": ["Toronto, ON, Canada"],
            "timeType": "Full time",
            "startDate": "2026-09-01",
            "postedOn": "Posted 30+ Days Ago",
            "remoteType": "Onsite",
        }
    }


def test_greenhouse_identity_details_and_first_publication():
    employer, source = configuration("greenhouse")
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/123"):
            return httpx.Response(200, json={**gh_job(), "first_published": "2026-09-01T12:00:00Z"})
        assert request.url.params["content"] == "true"
        return httpx.Response(200, json={"jobs": [gh_job()], "meta": {"total": 1}})

    result = collect_source(employer, source, [], NOW, client=transport(handler))
    assert result.complete and result.pages_fetched == 1 and len(calls) == 2
    posting = result.postings[0]
    assert posting.employer_date == "2026-09-01T12:00:00+00:00"
    assert posting.employer_date_kind == "first_published"
    assert posting.source_updated_at == "2026-10-08T08:00:00Z"
    assert posting.description == "Pursuing a mechanical engineering degree."
    assert posting.locations[0].country == "US"
    assert "utm" not in posting.source_url


def test_greenhouse_detail_failure_preserves_complete_inventory():
    employer, source = configuration("greenhouse")

    def handler(request):
        if request.url.path.endswith("/123"):
            return httpx.Response(403)
        return httpx.Response(200, json={"jobs": [gh_job()], "meta": {"total": 1}})

    result = GreenhouseAdapter(transport(handler)).collect(employer, source, [], NOW)
    assert result.complete and len(result.items) == 1
    assert result.postings == [] and "123" in result.detail_errors


@pytest.mark.parametrize(
    "payload",
    [
        {"jobs": [gh_job()], "meta": {"total": 2}},
        {"jobs": [gh_job(), gh_job()], "meta": {"total": 2}},
        {"error": "unavailable"},
    ],
)
def test_greenhouse_incomplete_inventory_is_not_authoritative(payload):
    employer, source = configuration("greenhouse")
    result = GreenhouseAdapter(transport(lambda _: httpx.Response(200, json=payload))).collect(
        employer,
        source,
        [],
        NOW,
    )
    assert not result.complete and result.errors


def test_lever_exact_page_boundary_requests_terminal_page_and_retains_all_content():
    employer, source = configuration("lever")
    offsets = []

    def handler(request):
        offsets.append(int(request.url.params["skip"]))
        return httpx.Response(
            200, json=[lever_job("a"), lever_job("b")] if len(offsets) == 1 else []
        )

    adapter = LeverAdapter(transport(handler))
    adapter.page_size = 2
    result = adapter.collect(employer, source, [], NOW)
    assert offsets == [0, 2] and result.complete and result.expected_count == 2
    posting = result.postings[0]
    assert posting.employer_date is None
    assert "Mechanical degree" in posting.description and "Return to school" in posting.description
    assert posting.pay.minimum == 25 and posting.workplace == "onsite"
    assert [location.country for location in posting.locations] == ["US", "GB"]


def test_lever_repeated_page_and_cap_are_incomplete():
    employer, source = configuration("lever")
    adapter = LeverAdapter(transport(lambda _: httpx.Response(200, json=[lever_job()])))
    adapter.page_size = 1
    result = adapter.collect(employer, source, [], NOW)
    assert not result.complete and "Duplicate" in result.errors[0]
    adapter.max_pages = 1
    result = adapter.collect(employer, source, [], NOW)
    assert not result.complete and "budget" in result.errors[0]


def test_lever_eu_board_selects_eu_api():
    employer, source = configuration("lever")
    source.board_url = "https://jobs.eu.lever.co/example"

    def handler(request):
        assert request.url.host == "api.eu.lever.co"
        return httpx.Response(200, json=[])

    assert LeverAdapter(transport(handler)).collect(employer, source, [], NOW).complete


@pytest.mark.parametrize("listed", [True, False])
def test_ashby_preserves_listing_visibility_and_last_publication(listed):
    employer, source = configuration("ashby")
    response = {"jobs": [ashby_job(listed)]}
    result = AshbyAdapter(transport(lambda _: httpx.Response(200, json=response))).collect(
        employer,
        source,
        [],
        NOW,
    )
    assert result.complete and result.postings[0].listed is listed
    assert result.items[0].listed is listed
    posting = result.postings[0]
    assert posting.employer_date_kind == "last_published"
    assert [location.country for location in posting.locations] == ["US", "GB"]


def test_workday_full_inventory_selective_details_and_cautious_dates():
    employer, source = configuration("workday")
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["searchText"] == "" and body["appliedFacets"] == {}
            page = (
                [wd_listing(), wd_listing(2, "Senior Mechanical Engineer")]
                if body["offset"] == 0
                else [wd_listing(3, "Accountant")]
            )
            return httpx.Response(200, json={"total": 3, "jobPostings": page})
        assert request.url.path.endswith("/job/Boston/Mechanical-Co-op_JR1-2")
        return httpx.Response(200, json=wd_detail())

    adapter = WorkdayAdapter(transport(handler))
    adapter.page_size = 2
    result = adapter.collect(employer, source, [], NOW)
    assert result.complete and len(result.items) == 3 and len(result.postings) == 1
    assert len(requests) == 3
    posting = result.postings[0]
    assert posting.source_job_id == "JR1-2" and posting.requisition_id == "JR1"
    assert posting.employer_date is None and posting.employer_date_raw == "Posted 30+ Days Ago"
    assert [location.country for location in posting.locations] == ["US", "CA"]


@pytest.mark.parametrize(
    "second",
    [
        {"total": 2, "jobPostings": []},
        {"total": 3, "jobPostings": [wd_listing(2)]},
        {"total": 2, "jobPostings": [wd_listing()]},
    ],
)
def test_workday_partial_changed_total_and_duplicate_inventory(second):
    employer, source = configuration("workday")
    calls = []

    def handler(request):
        calls.append(request)
        payload = {"total": 2, "jobPostings": [wd_listing()]} if len(calls) == 1 else second
        return httpx.Response(200, json=payload)

    result = WorkdayAdapter(transport(handler)).collect(employer, source, [], NOW)
    assert not result.complete and result.errors and result.postings == []


def test_workday_inventory_survives_detail_failure_and_explicit_closure():
    employer, source = configuration("workday")

    def handler(request):
        if request.method == "POST":
            return httpx.Response(
                200, json={"total": 2, "jobPostings": [wd_listing(), wd_listing(2)]}
            )
        return (
            httpx.Response(403)
            if "JR1-2" in request.url.path
            else httpx.Response(200, json=wd_detail(False))
        )

    result = WorkdayAdapter(transport(handler)).collect(employer, source, [], NOW)
    assert result.complete and "JR1-2" in result.detail_errors
    assert result.postings[0].explicitly_closed


def test_workday_caches_details_but_refreshes_after_24_hours_or_force():
    employer, source = configuration("workday")
    detail_calls = []

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"total": 1, "jobPostings": [wd_listing()]})
        detail_calls.append(request)
        return httpx.Response(200, json=wd_detail())

    adapter = WorkdayAdapter(transport(handler))
    first = adapter.collect(employer, source, [], NOW)
    existing = [prior(first.postings[0])]
    second = adapter.collect(employer, source, existing, NOW)
    assert second.complete and not second.postings and len(detail_calls) == 1
    adapter.collect(employer, source, existing, NOW, force_details=True)
    assert len(detail_calls) == 2
    adapter.collect(employer, source, existing, NOW + timedelta(hours=25))
    assert len(detail_calls) == 3


def test_workday_relative_posted_label_does_not_change_fingerprint():
    employer, source = configuration("workday")

    def result_for(label):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(
                    200, json={"total": 1, "jobPostings": [wd_listing(posted=label)]}
                )
            return httpx.Response(200, json=wd_detail())

        return WorkdayAdapter(transport(handler)).collect(employer, source, [], NOW)

    assert (
        result_for("Posted Today").items[0].fingerprint
        == result_for("Posted Yesterday").items[0].fingerprint
    )


def test_workday_subsequent_zero_totals_retain_first_page_total():
    employer, source = configuration("workday")

    def handler(request):
        offset = json.loads(request.content)["offset"]
        return httpx.Response(
            200,
            json={
                "total": 2 if offset == 0 else 0,
                "jobPostings": [wd_listing(offset + 1, title="Senior Engineer")],
            },
        )

    result = WorkdayAdapter(transport(handler)).collect(employer, source, [], NOW)
    assert result.complete and result.expected_count == 2 and result.pages_fetched == 2
    assert len(result.items) == 2


def test_workday_opaque_record_keeps_valid_details_but_never_claims_complete_inventory():
    employer, source = configuration("workday")
    listing_calls = []

    def handler(request):
        if request.method == "POST":
            listing_calls.append(request)
            return httpx.Response(
                200,
                json={
                    "total": 2,
                    "jobPostings": [wd_listing(), {"bulletFields": ["01868837"]}],
                },
            )
        return httpx.Response(200, json=wd_detail())

    result = WorkdayAdapter(transport(handler)).collect(employer, source, [], NOW)
    assert not result.complete and result.expected_count == 2 and len(result.items) == 1
    assert len(result.postings) == 1 and len(listing_calls) == 2
    assert "no public title/URL" in result.errors[0]


def test_workday_retries_transient_opaque_record_before_accepting_inventory():
    employer, source = configuration("workday")
    listing_calls = []

    def handler(request):
        if request.method == "POST":
            listing_calls.append(request)
            jobs = [{"bulletFields": ["JR1"]}] if len(listing_calls) == 1 else [wd_listing()]
            return httpx.Response(200, json={"total": 1, "jobPostings": jobs})
        return httpx.Response(200, json=wd_detail())

    result = WorkdayAdapter(transport(handler)).collect(employer, source, [], NOW)
    assert result.complete and len(result.items) == 1 and len(result.postings) == 1
    assert len(listing_calls) == 2 and result.errors == []


def test_http_retries_transient_responses_honors_retry_after_and_retains_injected_client():
    attempts = []
    elapsed = [0.0]

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"jobs": []})

    client = transport(handler)
    http = SourceClient(
        client, clock=lambda: elapsed[0], sleep=lambda x: elapsed.__setitem__(0, elapsed[0] + x)
    )
    assert http.get_json("https://example.com/jobs") == {"jobs": []}
    assert len(attempts) == 2 and elapsed[0] == 3
    http.close()
    assert not client.is_closed


def test_http_failure_limits_invalid_json_and_deadline():
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503)

    http = SourceClient(transport(handler), sleep=lambda _: None)
    with pytest.raises(SourceHTTPError, match="3 attempts"):
        http.get_json("https://example.com/jobs")
    assert len(attempts) == 3
    http = SourceClient(transport(lambda _: httpx.Response(200, text="<html>challenge</html>")))
    with pytest.raises(SourceHTTPError, match="Expected JSON"):
        http.get_json("https://example.com/jobs")
    http = SourceClient(transport(lambda _: httpx.Response(429, headers={"Retry-After": "600"})))
    with pytest.raises(SourceHTTPError, match="deadline"):
        http.get_json("https://example.com/jobs")


def test_normalization_keeps_ambiguous_locations_dates_and_job_identifiers():
    assert (
        canonical_url("https://example.com/job?gh_jid=12&utm_campaign=x#apply")
        == "https://example.com/job?gh_jid=12"
    )
    assert normalize_locations("Boston")[0].country is None
    assert normalize_locations("Cambridge, United Kingdom")[0].country == "GB"
    assert normalize_locations("San Francisco, CA")[0].country == "US"
    assert normalize_locations("Toronto, ON")[0].country == "CA"
    assert parse_employer_date("2026-10-01") == ("2026-10-01", "date")
    assert parse_employer_date("Posted 30+ Days Ago") == (None, "unknown")
    assert parse_employer_date("2026-10-01T08:00:00") == (None, "unknown")
    assert html_text("<p>Hello</p><script>bad()</script><p>World</p>") == "Hello\nWorld"
    assert is_student_candidate("Audio Systems Engineering Co-op")
    assert not is_student_candidate("Senior Mechanical Engineer")


def test_location_country_hint_and_structured_country_outrank_state_guessing():
    assert normalize_locations("Ontario, CA", country_hint="CA")[0].country == "CA"
    assert normalize_locations({"name": "CA", "country": {"alpha2Code": "CA"}})[0].country == "CA"
    assert (
        normalize_locations({"address": {"postalAddress": {"addressCountry": "US"}}})[0].raw == "US"
    )
    assert normalize_locations({"location": None, "address": {}}) == []
    assert normalize_locations("London", country_hint="GB")[0].country == "GB"
    assert normalize_locations("Washington, District of Columbia, United States")[0].state == "DC"
    assert normalize_locations("Washington, DC")[0].state == "DC"
    assert normalize_locations("Seattle, Washington, United States")[0].state == "WA"
    assert normalize_locations("Toronto, CAN")[0].country == "CA"
    assert normalize_locations("Auckland, NZL")[0].country == "NZ"
    assert normalize_locations("Auckland, NZ")[0].country == "NZ"
    campus = normalize_locations("US-MA-MARLBOROUGH-MA4 ~ 1001 Boston Post Rd")[0]
    assert campus.country == "US" and campus.state == "MA"


def test_source_alias_reuses_previous_details():
    employer, source = configuration("workday")
    detail_calls = []

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"total": 1, "jobPostings": [wd_listing()]})
        detail_calls.append(request)
        return httpx.Response(200, json=wd_detail())

    adapter = WorkdayAdapter(transport(handler))
    initial = adapter.collect(employer, source, [], NOW)
    source.aliases = ["old-careers"]
    job = prior(initial.postings[0])
    job.source_id = "old-careers"
    assert adapter.collect(employer, source, [job], NOW).postings == []
    assert len(detail_calls) == 1


def test_malformed_optional_detail_does_not_invalidate_good_inventory():
    employer, source = configuration("lever")
    job = lever_job()
    job["categories"] = "unexpected shape"
    result = LeverAdapter(transport(lambda _: httpx.Response(200, json=[job]))).collect(
        employer,
        source,
        [],
        NOW,
    )
    assert result.complete and len(result.items) == 1 and result.postings == []
    assert "a" in result.detail_errors


def test_http_host_capacity_is_shared_across_source_clients():
    lock = threading.Lock()
    active = [0]
    peak = [0]

    def handler(request):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.01)
        with lock:
            active[0] -= 1
        return httpx.Response(200, json={})

    client = transport(handler)

    def fetch(_):
        return SourceClient(client).get_json("https://shared-host.example/jobs")

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(fetch, range(12))) == [{}] * 12
    assert peak[0] <= 2


def test_workday_exact_duplicates_are_deduplicated_without_false_completeness():
    employer, source = configuration("workday")

    def handler(request):
        if request.method == "POST":
            return httpx.Response(
                200, json={"total": 2, "jobPostings": [wd_listing(), wd_listing()]}
            )
        return httpx.Response(200, json=wd_detail())

    result = WorkdayAdapter(transport(handler)).collect(employer, source, [], NOW)
    assert not result.complete and len(result.items) == 1 and len(result.postings) == 1
    assert result.expected_count == 2 and "exact row" in result.errors[0]


def test_workday_same_id_at_different_urls_does_not_guess_a_detail_record():
    employer, source = configuration("workday")
    second = {**wd_listing(), "externalPath": "/job/Austin/Mechanical-Co-op_JR1-2"}
    response = {"total": 2, "jobPostings": [wd_listing(), second]}
    result = WorkdayAdapter(transport(lambda _: httpx.Response(200, json=response))).collect(
        employer,
        source,
        [],
        NOW,
    )
    assert not result.complete and result.postings == []
    assert "JR1-2" in result.detail_errors


def test_description_location_fallback_uses_explicit_role_availability_only():
    text = (
        "Internships are available with a variety of engineering teams across the company "
        "including design, manufacturing and operations at Bastrop, TX; Bloomfield, CT, "
        "Brownsville, TX; Cape Canaveral, FL; Hawthorne (Los Angeles), CA; Irvine, CA; "
        "Long Beach, CA, McGregor (Waco), TX; Palo Alto, CA; Redmond (Seattle), WA; "
        "Vandenberg, CA; and Woodinville, WA."
    )
    locations = description_locations(text)
    assert len(locations) == 12
    assert locations[0].city == "Bastrop" and locations[-1].city == "Woodinville"
    assert all(location.country == "US" for location in locations)
    assert (
        description_locations("Our headquarters and offices are in Austin, TX; Boston, MA.") == []
    )
    assert description_locations("Interns may travel to Boston, MA for training.") == []
    assert description_locations("This role is based in Toronto, ON.") == []
    only_role = description_locations(
        "This role is based in Austin, TX. We also have offices internationally; Toronto, CA."
    )
    assert len(only_role) == 1 and only_role[0].city == "Austin"
