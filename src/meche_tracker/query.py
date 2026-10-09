"""The same composable filters power local queries and all generated views."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import Job

REGIONS = {
    "northeast": "CT ME MA NH RI VT NJ NY PA".split(),
    "midwest": "IN IL MI OH WI IA KS MN MO NE ND SD".split(),
    "south": "DE DC FL GA MD NC SC VA WV AL KY MS TN AR LA OK TX".split(),
    "west": "AZ CO ID MT NV NM UT WY AK CA HI OR WA".split(),
}


@dataclass
class Query:
    employers: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    workplaces: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    seasons: list[str] = field(default_factory=list)
    years: list[int] = field(default_factory=list)
    specialties: list[str] = field(default_factory=list)
    degrees: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=lambda: ["role"])
    statuses: list[str] = field(default_factory=lambda: ["open"])
    decisions: list[str] = field(default_factory=lambda: ["include"])
    include_graduate: bool = False
    graduate_only: bool = False
    include_stale: bool = False
    unverified_only: bool = False
    discovered_days: int | None = None
    verified_hours: float = 48
    pay_available: bool | None = None
    minimum_pay: float | None = None
    pay_unit: str | None = None
    pay_currency: str | None = None
    sponsorship: bool | None = None
    housing: bool | None = None
    relocation: bool | None = None
    start_after: str | None = None
    end_before: str | None = None
    minimum_weeks: float | None = None
    maximum_weeks: float | None = None
    sort: str = "discovered"


def is_fresh(job: Job, now: datetime, hours: float = 48) -> bool:
    return job.last_verified_at is not None and (
        timedelta(0) <= now - job.last_verified_at <= timedelta(hours=hours)
    )


def matches(job: Job, query: Query, now: datetime) -> bool:
    p, c = job.posting, job.classification
    if query.statuses and job.availability not in query.statuses:
        return False
    if query.decisions and c.decision not in query.decisions:
        return False
    if query.unverified_only and not (
        job.availability in {"unverified", "suspected_closed"}
        or (job.availability == "open" and not is_fresh(job, now, query.verified_hours))
    ):
        return False
    if not p.listed:
        return False
    if not query.include_stale and not is_fresh(job, now, query.verified_hours):
        return False
    if query.graduate_only and not c.graduate_only:
        return False
    if not query.include_graduate and c.graduate_only:
        return False
    if query.kinds and c.kind not in query.kinds:
        return False
    if query.employers and job.employer_id not in query.employers:
        return False
    if query.industries and job.industry not in query.industries:
        return False
    allowed_states = set(query.states)
    if query.regions:
        region_states = {state for region in query.regions for state in REGIONS[region]}
        if not any(loc.country == "US" and loc.state in region_states for loc in p.locations):
            return False
    if allowed_states and not any(
        loc.country == "US" and loc.state in allowed_states for loc in p.locations
    ):
        return False
    if query.workplaces and p.workplace not in query.workplaces:
        return False
    if query.types and not set(query.types).intersection(c.opportunity_types):
        return False
    if query.seasons or query.years:
        if not any(
            (not query.seasons or term.season in query.seasons)
            and (not query.years or term.year in query.years)
            for term in c.terms
        ):
            return False
    if query.specialties and not set(query.specialties).intersection(c.specialties):
        return False
    if query.degrees and not (
        set(query.degrees).intersection(c.degrees)
        or ("unspecified" in query.degrees and not c.degrees)
    ):
        return False
    if query.discovered_days is not None and job.first_seen_at < now - timedelta(
        days=query.discovered_days
    ):
        return False
    for field_name in ["sponsorship", "housing", "relocation"]:
        expected = getattr(query, field_name)
        if expected is not None and getattr(c, field_name) is not expected:
            return False
    known_pay = p.pay is not None and (p.pay.minimum is not None or p.pay.maximum is not None)
    if query.pay_available is not None and known_pay is not query.pay_available:
        return False
    if query.minimum_pay is not None:
        if query.pay_unit is None or query.pay_currency is None:
            raise ValueError("minimum pay requires both pay unit and currency")
        if p.pay is None or p.pay.unit != query.pay_unit or p.pay.currency != query.pay_currency:
            return False
        if p.pay.minimum is None or p.pay.minimum < query.minimum_pay:
            return False
    if query.start_after and (c.start_date is None or c.start_date.isoformat() < query.start_after):
        return False
    if query.end_before and (c.end_date is None or c.end_date.isoformat() > query.end_before):
        return False
    if query.minimum_weeks is not None and (
        c.duration_weeks is None or c.duration_weeks < query.minimum_weeks
    ):
        return False
    if query.maximum_weeks is not None and (
        c.duration_weeks is None or c.duration_weeks > query.maximum_weeks
    ):
        return False
    return True


def select(jobs: list[Job], query: Query, now: datetime) -> list[Job]:
    if query.minimum_pay is not None and (not query.pay_unit or not query.pay_currency):
        raise ValueError("minimum pay requires --pay-unit and --pay-currency")
    matches_list = [job for job in jobs if matches(job, query, now)]
    # Stable secondary keys avoid reordering equal timestamps between fetches.
    matches_list.sort(key=lambda j: (j.employer_name.casefold(), j.posting.title.casefold(), j.id))
    matches_list.sort(key=lambda j: j.posting.employer_date or "", reverse=True)
    if query.sort == "discovered":
        matches_list.sort(key=lambda j: j.first_seen_at, reverse=True)
    elif query.sort != "posted":
        raise ValueError(f"unsupported sort: {query.sort}")
    return matches_list
