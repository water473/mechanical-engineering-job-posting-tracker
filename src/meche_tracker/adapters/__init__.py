"""Adapter registry. All collectors share the same normalized result contract."""

from datetime import datetime

import httpx

from meche_tracker.adapters.ashby import AshbyAdapter
from meche_tracker.adapters.greenhouse import GreenhouseAdapter
from meche_tracker.adapters.lever import LeverAdapter
from meche_tracker.adapters.workday import WorkdayAdapter
from meche_tracker.models import Employer, FetchResult, Job, SourceConfig

ADAPTERS = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
    "workday": WorkdayAdapter,
}


def collect_source(
    employer: Employer,
    source: SourceConfig,
    existing: list[Job],
    now: datetime,
    force_details: bool = False,
    *,
    client: httpx.Client | None = None,
) -> FetchResult:
    return ADAPTERS[source.ats](client).collect(employer, source, existing, now, force_details)


__all__ = [
    "ADAPTERS",
    "AshbyAdapter",
    "GreenhouseAdapter",
    "LeverAdapter",
    "WorkdayAdapter",
    "collect_source",
]
