"""Network-free, deterministic Markdown publishing from the validated catalog."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from .classification import SPECIALTIES
from .models import Catalog, Employer, HealthDocument, Job, Location
from .query import REGIONS, Query, select

PAGE_SIZE = 200
EASTERN = ZoneInfo("America/New_York")


def escape(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    for character in ["\\", "|", "[", "]", "<", ">", "`", "*"]:
        value = value.replace(character, "\\" + character)
    return value


def link(label: str, url: str | None) -> str:
    if not url:
        return escape(label)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return escape(label)
    safe_url = quote(url, safe=":/?#[]@!$&'+,;=%~_-.")
    return f"[{escape(label)}](<{safe_url}>)"


def day(value: str | datetime | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone(EASTERN).date().isoformat()
    if "T" in value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo:
                return parsed.astimezone(EASTERN).date().isoformat()
        except ValueError:
            pass
    return value[:10]


def _location_label(location: Location) -> str:
    if location.city and location.state:
        label = f"{location.city}, {location.state}"
    else:
        label = location.raw
    # Full source locations remain in JSON; keep large campus labels out of the table.
    return label if len(label) <= 70 else label[:67].rstrip() + "…"


def table(jobs: list[Job]) -> str:
    lines = [
        "| Company | Role | Location | Term / type | Posted | Found | Checked |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for job in jobs:
        p, c = job.posting, job.classification
        locations = [_location_label(loc) for loc in p.locations if loc.country == "US"]
        locations = locations or [_location_label(loc) for loc in p.locations]
        location = "; ".join(locations[:2])
        if len(locations) > 2:
            location += f"; +{len(locations) - 2} more"
        terms = [
            f"{term.season.title()}{' ' + str(term.year) if term.year else ''}" for term in c.terms
        ]
        program = "; ".join(terms[:3]) or "Term unknown"
        program += " / " + ", ".join(c.opportunity_types)
        posted = day(p.employer_date)
        if p.employer_date_kind == "last_published" and p.employer_date:
            posted += " †"
        lines.append(
            "| "
            + " | ".join(
                [
                    escape(job.employer_name),
                    link(p.title, p.apply_url or p.source_url),
                    escape(location or "Unknown"),
                    escape(program),
                    posted,
                    day(job.first_seen_at),
                    day(job.last_verified_at),
                ]
            )
            + " |"
        )
    if not jobs:
        return "No matching opportunities in the current catalog.\n"
    return "\n".join(lines) + "\n"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def build_outputs(
    catalog: Catalog,
    health: HealthDocument,
    employers: list[Employer],
    now: datetime,
) -> dict[str, str]:
    outputs: dict[str, str] = {}
    links: list[tuple[str, str, int]] = []
    updated = (
        catalog.generated_at.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M %Z")
        if catalog.generated_at
        else "Not collected yet"
    )

    def view(name: str, title: str, query: Query, *, always: bool = False) -> None:
        jobs = select(catalog.jobs, query, now)
        if not jobs and not always:
            return
        pages = max(1, (len(jobs) + PAGE_SIZE - 1) // PAGE_SIZE)
        path = f"views/{name}.md"
        links.append((title, path, len(jobs)))
        for index in range(pages):
            filename = path if index == 0 else f"views/{name}-{index + 1}.md"
            navigation = []
            for page in range(pages):
                target = f"{name}.md" if page == 0 else f"{name}-{page + 1}.md"
                navigation.append(str(page + 1) if page == index else f"[{page + 1}]({target})")
            selected = jobs[index * PAGE_SIZE : (index + 1) * PAGE_SIZE]
            unknown_note = ""
            if query.sort == "posted":
                known = [job for job in selected if job.posting.employer_date]
                unknown = [job for job in selected if not job.posting.employer_date]
                body = table(known) if known else ""
                if unknown:
                    body += "\n## Employer posting date unknown\n\n" + table(unknown)
                unknown_note = "Known employer dates first; unknown dates are grouped last.\n\n"
            else:
                body = table(selected)
            outputs[filename] = (
                f"# {title}\n\n[Browse views](index.md) · [Main list](../README.md)\n\n"
                f"Updated **{updated}** · **{len(jobs)}** matching opportunities\n\n"
                + unknown_note
                + body
                + "\n"
                + ("Pages: " + " · ".join(navigation) + "\n\n" if pages > 1 else "")
                + "Posted = employer date; † = latest publication, potentially a repost. "
                "Found = first discovery. Checked = last successful verification. "
                "Dates shown in Eastern Time where timestamps are available.\n"
            )

    view("all", "All verified opportunities", Query(), always=True)
    view("posted", "Newest employer posting dates", Query(sort="posted"), always=True)
    view("new", "Discovered in the last seven days", Query(discovered_days=7), always=True)
    for kind in ["internship", "co-op"]:
        view(kind, kind.title(), Query(types=[kind]))
    for name in SPECIALTIES:
        view(f"specialty-{name}", name.replace("-", " / ").title(), Query(specialties=[name]))
    for name in REGIONS:
        view(f"region-{name}", name.title(), Query(regions=[name]))
    states = sorted(
        {
            loc.state
            for job in catalog.jobs
            for loc in job.posting.locations
            if loc.country == "US" and loc.state
        }
    )
    for state in states:
        view(f"state-{state.lower()}", f"State: {state}", Query(states=[state]))
    terms = sorted(
        {(term.season, term.year) for job in catalog.jobs for term in job.classification.terms},
        key=lambda x: (x[1] or 0, x[0]),
    )
    for season, year in terms:
        title = f"{season.title()} {year}" if year else f"{season.title()} — year unspecified"
        if year:
            view(f"term-{season}-{year}", title, Query(seasons=[season], years=[year]))
    for degree in ["bachelors", "masters", "phd", "unspecified"]:
        view(
            f"degree-{degree}", f"Degree: {degree}", Query(degrees=[degree], include_graduate=True)
        )
    for employer in employers:
        view(f"employer-{employer.id}", employer.name, Query(employers=[employer.id]))
    for industry in sorted({employer.industry for employer in employers}):
        view(f"industry-{_slug(industry)}", industry, Query(industries=[industry]))
    for name in ["housing", "relocation", "sponsorship"]:
        view(name, f"Explicitly stated {name}", Query(**{name: True}))
    view("pay", "Published compensation", Query(pay_available=True))
    for workplace in ["onsite", "hybrid", "remote"]:
        view(f"workplace-{workplace}", workplace.title(), Query(workplaces=[workplace]))
    view(
        "graduate",
        "Graduate-only opportunities",
        Query(include_graduate=True, graduate_only=True),
        always=True,
    )
    view(
        "early-interest",
        "Early interest and talent pools",
        Query(kinds=["early_interest", "talent_pool"], include_graduate=True),
        always=True,
    )
    view(
        "unverified",
        "Unverified and suspected closed",
        Query(
            statuses=[],
            unverified_only=True,
            kinds=[],
            include_stale=True,
            include_graduate=True,
        ),
        always=True,
    )
    view(
        "closed",
        "Closed opportunities",
        Query(statuses=["closed"], kinds=[], include_stale=True, include_graduate=True),
        always=True,
    )
    view(
        "review",
        "Candidates requiring review",
        Query(
            decisions=["review"], kinds=[], statuses=[], include_stale=True, include_graduate=True
        ),
        always=True,
    )

    enabled = {source.id for employer in employers for source in employer.sources if source.enabled}
    statuses = {source.source_id: source for source in health.sources}
    latest_complete = sum(
        bool(statuses.get(source) and statuses[source].complete) for source in enabled
    )
    main = select(catalog.jobs, Query(), now)
    bootstrap = ""
    if catalog.initial_inventory_at and catalog.generated_at == catalog.initial_inventory_at:
        bootstrap = "Initial inventory: these roles were first discovered during setup.\n\n"
    outputs["README.md"] = (
        "# Mechanical Engineering Internships & Co-ops\n\n"
        "U.S. opportunities for undergraduate mechanical engineering students, "
        "collected from employer career boards.\n\n"
        f"Updated **{updated}** · **{len(main)}** verified opportunities · "
        f"**{latest_complete}/{len(enabled)}** sources complete on their last check\n\n"
        "[Browse filters](views/index.md) · [All opportunities](views/all.md) · "
        "[Newest postings](views/posted.md) · [New this week](views/new.md) · "
        "[Source coverage](views/coverage.md)\n\n"
        + bootstrap
        + f"Showing {min(PAGE_SIZE, len(main))} of {len(main)}, "
        "newest discoveries first. Listings in this view were verified within 48 hours.\n\n"
        + table(main[:PAGE_SIZE])
        + "\nPosted = employer publication date; † = latest publication, potentially a repost. "
        "Found = first discovery; repeat checks do not move a role to the top. "
        "Checked = last successful verification. An em dash means unknown.\n\n"
        "Refresh target: every three hours. Scheduled operation starts after this repository "
        "is published to GitHub and Actions is enabled. "
        "[Methodology](docs/METHODOLOGY.md) · [Contribute](docs/CONTRIBUTING.md) · "
        "[Local development and manual refresh](docs/DEVELOPMENT.md)\n"
    )
    outputs["views/index.md"] = (
        "# Browse opportunities\n\n[Main list](../README.md) · "
        "[Employer coverage](coverage.md)\n\n"
        "These are preset views. Combine arbitrary filters with the local CLI. "
        "Unknown fields do not satisfy explicit eligibility or benefit filters.\n\n"
        + "\n".join(
            f"- [{escape(title)}]({path.removeprefix('views/')}) — {count}"
            for title, path, count in links
        )
        + "\n"
    )
    coverage = [
        "# Employer coverage\n",
        "[Main list](../README.md) · [Browse views](index.md)\n",
        f"{len(employers)} configured employers; {len(enabled)} enabled sources. "
        "Zero matching jobs does not mean an employer's source is broken.\n",
        "| Employer | Industry | ATS | Status | Inventory | Last successful feed check |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    coverage_errors = []
    for employer in employers:
        for source in employer.sources:
            status = statuses.get(source.id)
            state = (
                "Disabled"
                if not source.enabled
                else ("Complete" if status and status.complete else "Not checked / incomplete")
            )
            count = (
                str(status.last_complete_count)
                if status and (status.last_complete_count is not None)
                else "—"
            )
            coverage.append(
                "| "
                + " | ".join(
                    [
                        link(employer.name, source.board_url),
                        escape(employer.industry),
                        source.ats,
                        state,
                        count,
                        day(status.last_success_at if status else None),
                    ]
                )
                + " |"
            )
            if status and status.errors:
                coverage_errors.append(
                    f"- {escape(employer.name)}: "
                    + "; ".join(escape(error) for error in status.errors[:2])
                )
    if coverage_errors:
        coverage.extend(["\n## Collection diagnostics\n", *coverage_errors])
    outputs["views/coverage.md"] = "\n".join(coverage) + "\n"
    return outputs
