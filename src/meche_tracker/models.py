"""Validated interfaces shared by adapters, reconciliation, and presentation."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ATS = Literal["greenhouse", "lever", "ashby", "workday"]
ATS_LABELS: dict[ATS, str] = {
    "workday": "Workday",
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
}
Availability = Literal["open", "suspected_closed", "closed", "unverified"]
Decision = Literal["include", "review", "exclude"]
Workplace = Literal["onsite", "hybrid", "remote", "unknown"]
Degree = Literal["bachelors", "masters", "phd"]
Opportunity = Literal["internship", "co-op"]
Kind = Literal["role", "early_interest", "talent_pool"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("*")
    @classmethod
    def timezone_aware_dates(cls, value):
        if isinstance(value, datetime) and value.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        return value


class SourceConfig(Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    ats: ATS
    board_url: str
    enabled: bool = False
    board_token: str | None = None
    tenant: str | None = None
    site: str | None = None
    locale: str = "en-US"
    student_only: bool = False
    candidate_title_patterns: list[str] = Field(default_factory=list)
    verified_at: datetime | None = None
    verification_note: str | None = None
    aliases: list[str] = Field(default_factory=list)

    @field_validator("board_url")
    @classmethod
    def public_board(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("board_url must be an HTTPS URL without credentials")
        return value.rstrip("/")

    @field_validator("verified_at")
    @classmethod
    def aware_verification(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("verification timestamp must include a timezone")
        return value

    @field_validator("candidate_title_patterns")
    @classmethod
    def valid_patterns(cls, values: list[str]) -> list[str]:
        for value in values:
            re.compile(value, re.IGNORECASE)
        return values

    @model_validator(mode="after")
    def validate_board(self) -> SourceConfig:
        host = urlsplit(self.board_url).hostname or ""
        allowed = {
            "greenhouse": ("greenhouse.io",),
            "lever": ("lever.co",),
            "ashby": ("ashbyhq.com",),
            "workday": ("myworkdayjobs.com",),
        }[self.ats]
        if not any(host == suffix or host.endswith("." + suffix) for suffix in allowed):
            raise ValueError(f"{self.ats} board_url must use its public ATS domain")
        if self.ats != "workday" and not self.token:
            raise ValueError("board token is required")
        if self.ats == "workday" and not self.workday_site:
            raise ValueError("Workday site is required")
        if self.enabled and self.verified_at is None:
            raise ValueError("enabled sources require a verified_at timestamp")
        return self

    @property
    def token(self) -> str:
        return self.board_token or urlsplit(self.board_url).path.strip("/").split("/")[0]

    @property
    def origin(self) -> str:
        parsed = urlsplit(self.board_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def workday_tenant(self) -> str:
        return self.tenant or (urlsplit(self.board_url).hostname or "").split(".")[0]

    @property
    def workday_site(self) -> str:
        parts = urlsplit(self.board_url).path.strip("/").split("/")
        if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
            parts = parts[1:]
        return self.site or (parts[0] if parts else "")


class Employer(Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str
    website: str
    industry: str
    sources: list[SourceConfig] = Field(min_length=1)


class Location(Model):
    raw: str
    city: str | None = None
    state: str | None = None
    country: str | None = None


class Term(Model):
    season: Literal["winter", "spring", "summer", "fall"]
    year: int | None = None


class Pay(Model):
    minimum: float | None = None
    maximum: float | None = None
    currency: str | None = None
    unit: Literal["hour", "year", "month", "week"] | None = None
    raw: str | None = None


class Posting(Model):
    """Source facts. Adapters never decide whether a role belongs in the tracker."""

    source_job_id: str
    title: str
    source_url: str
    apply_url: str | None = None
    requisition_id: str | None = None
    description: str | None = None
    content_hash: str = ""
    locations: list[Location] = Field(default_factory=list)
    workplace: Workplace = "unknown"
    employment_type: str | None = None
    employer_date: str | None = None
    employer_date_kind: Literal["first_published", "last_published", "date_posted"] | None = None
    employer_date_precision: Literal["timestamp", "date", "unknown"] = "unknown"
    employer_date_raw: str | None = None
    source_updated_at: str | None = None
    pay: Pay | None = None
    fingerprint: str = ""
    listed: bool = True
    explicitly_closed: bool = False
    closure_reason: str | None = None


class InventoryItem(Model):
    source_job_id: str
    title: str
    source_url: str
    fingerprint: str = ""
    detail_path: str | None = None
    listed: bool = True


class FetchResult(Model):
    employer_id: str
    source_id: str
    items: list[InventoryItem] = Field(default_factory=list)
    postings: list[Posting] = Field(default_factory=list)
    complete: bool = False
    expected_count: int | None = None
    pages_fetched: int = 0
    errors: list[str] = Field(default_factory=list)
    detail_errors: dict[str, str] = Field(default_factory=dict)


class Classification(Model):
    decision: Decision = "review"
    reasons: list[str] = Field(default_factory=list)
    specialties: list[str] = Field(default_factory=list)
    opportunity_types: list[Opportunity] = Field(default_factory=list)
    terms: list[Term] = Field(default_factory=list)
    degrees: list[Degree] = Field(default_factory=list)
    graduate_only: bool = False
    kind: Kind = "role"
    start_date: date | None = None
    end_date: date | None = None
    duration_weeks: float | None = None
    sponsorship: bool | None = None
    housing: bool | None = None
    relocation: bool | None = None
    eligibility_statements: list[str] = Field(default_factory=list)


class Job(Model):
    id: str
    employer_id: str
    employer_name: str
    industry: str
    source_id: str
    ats: ATS
    posting: Posting
    classification: Classification = Field(default_factory=Classification)
    aliases: list[str] = Field(default_factory=list)
    first_seen_at: datetime
    last_seen_at: datetime
    details_checked_at: datetime | None = None
    last_verified_at: datetime | None = None
    reopened_at: datetime | None = None
    closed_at: datetime | None = None
    availability: Availability = "unverified"
    status_reason: str | None = None
    missing_since: datetime | None = None
    missing_count: int = 0

    @field_validator(
        "first_seen_at",
        "last_seen_at",
        "details_checked_at",
        "last_verified_at",
        "reopened_at",
        "closed_at",
        "missing_since",
    )
    @classmethod
    def aware_times(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("job timestamps must include a timezone")
        return value


class Catalog(Model):
    schema_version: Literal[1] = 1
    generated_at: datetime | None = None
    initial_inventory_at: datetime | None = None
    jobs: list[Job] = Field(default_factory=list)


class SourceHealth(Model):
    employer_id: str
    source_id: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_complete_count: int | None = None
    pages_fetched: int = 0
    complete: bool = False
    errors: list[str] = Field(default_factory=list)
    detail_error_count: int = 0
    consecutive_failures: int = 0
    quarantined_count: int | None = None
    quarantined_at: datetime | None = None


class HealthDocument(Model):
    schema_version: Literal[1] = 1
    generated_at: datetime | None = None
    sources: list[SourceHealth] = Field(default_factory=list)


class Override(Model):
    job_id: str
    reason: str = Field(min_length=1)
    reviewed_at: datetime
    posting_fields: dict = Field(default_factory=dict)
    classification_fields: dict = Field(default_factory=dict)


class OverrideDocument(Model):
    schema_version: Literal[1] = 1
    overrides: list[Override] = Field(default_factory=list)


class RunReport(Model):
    started_at: datetime
    employers_checked: int = 0
    sources_checked: int = 0
    sources_complete: int = 0
    source_errors: int = 0
    discovered: int = 0
    reopened: int = 0
    closed: int = 0
    reviewed: int = 0
    errors: list[str] = Field(default_factory=list)
