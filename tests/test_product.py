import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from meche_tracker.classification import classify, extract_pay
from meche_tracker.cli import main, parser
from meche_tracker.models import (
    Catalog,
    Employer,
    HealthDocument,
    Job,
    Location,
    OverrideDocument,
    Pay,
    Posting,
    SourceConfig,
)
from meche_tracker.query import Query, select
from meche_tracker.render import build_outputs, day, table

NOW = datetime(2026, 10, 8, 16, tzinfo=UTC)


def posting(title="Mechanical Engineering Intern - Summer 2027", description="", **values):
    return Posting(
        source_job_id="123",
        title=title,
        source_url="https://example.com/job/123",
        description=description,
        locations=[Location(raw="Boston, MA", state="MA", country="US")],
        **values,
    )


def job(id="123", found=NOW, **values):
    source = posting(**values)
    return Job(
        id=id,
        employer_id="test",
        employer_name="Test Co",
        industry="Industrial",
        source_id="test-main",
        ats="greenhouse",
        posting=source,
        classification=classify(source, today=NOW.date()),
        first_seen_at=found,
        last_seen_at=NOW,
        details_checked_at=NOW,
        last_verified_at=NOW,
        availability="open",
    )


def test_body_eligible_me_and_mixed_program():
    result = classify(
        posting(
            "Audio Systems Engineering Co-op",
            "Currently pursuing a bachelor's "
            "degree in mechanical engineering. This internship includes CAD "
            "models. Spring 2027. 16-week program.",
        )
    )
    assert result.decision == "include"
    assert result.opportunity_types == ["internship", "co-op"]
    assert not result.graduate_only
    assert result.duration_weeks == 16
    assert any(term.season == "spring" and term.year == 2027 for term in result.terms)


@pytest.mark.parametrize(
    "title",
    [
        "Senior Mechanical Engineer",
        "International Sales Manager",
        "Engineer I (For 2026 Interns Only)",
        "Software Engineer Intern",
    ],
)
def test_non_student_and_software_exclusions(title):
    assert classify(posting(title)).decision == "exclude"


def test_non_me_student_excluded_and_generic_engineering_reviewed():
    assert classify(posting("Accounting Intern")).decision == "exclude"
    assert classify(posting("Engineering Intern")).decision == "review"


def test_graduate_and_early_interest_separate():
    graduate = job(title="PhD Thermal Engineering Intern")
    early = job(title="Mechanical Engineering Internship — Early Interest")
    assert select([graduate, early], Query(), NOW) == []
    assert select([graduate], Query(include_graduate=True, graduate_only=True), NOW) == [graduate]
    assert select([early], Query(kinds=["early_interest"]), NOW) == [early]


def test_exact_us_location_required():
    p = posting()
    p.locations = [Location(raw="Cambridge")]
    assert classify(p).decision == "review"
    p.locations = [Location(raw="Toronto, ON", country="CA")]
    assert classify(p).decision == "exclude"
    p.locations.append(Location(raw="Boston, MA", country="US", state="MA"))
    assert classify(p).decision == "include"


def test_benefits_do_not_invent_or_cross_sentences():
    p = posting(
        description="We do not provide relocation. Visa sponsorship is available. "
        "Housing is not provided."
    )
    c = classify(p)
    assert c.sponsorship is True
    assert c.relocation is False
    assert c.housing is False
    assert classify(posting()).sponsorship is None


def test_explicit_program_end_excluded():
    result = classify(posting(description="Program ends 2026-07-31."), today=NOW.date())
    assert result.decision == "exclude"


def test_pay_keeps_units_and_unknowns():
    pay = extract_pay("Pay: $25.50 - $35.00 per hour")
    assert pay.minimum == 25.50 and pay.maximum == 35 and pay.unit == "hour"
    assert extract_pay("Expected salary $75k per year").minimum == 75000
    assert extract_pay("Summer 2027, range depending on experience") is None


def test_discovery_order_stable_and_posted_view_unknown_last():
    assert day("2026-10-09T00:00:00Z") == "2026-10-08"
    assert day("2026-10-09") == "2026-10-09"
    old = job("old", found=NOW - timedelta(days=2), employer_date="2026-10-08")
    new = job("new", found=NOW, employer_date="2026-09-10")
    unknown = job("unknown", found=NOW + timedelta(seconds=1))
    assert [j.id for j in select([old, unknown, new], Query(), NOW)] == ["unknown", "new", "old"]
    assert [j.id for j in select([unknown, new, old], Query(sort="posted"), NOW)] == [
        "old",
        "new",
        "unknown",
    ]


def test_query_or_inside_facets_and_across_facets():
    candidate = job()
    assert select([candidate], Query(states=["MA", "NY"], types=["co-op", "internship"]), NOW)
    assert not select([candidate], Query(states=["NY"], types=["internship"]), NOW)
    assert not select([candidate], Query(states=["MA"], housing=True), NOW)


def test_platform_filters_use_or_within_platforms_and_and_across_facets():
    candidates = []
    for platform in ["greenhouse", "lever", "workday", "ashby"]:
        candidate = job(platform)
        candidate.ats = platform
        candidates.append(candidate)
    candidates[1].posting.locations = [Location(raw="New York, NY", state="NY", country="US")]

    assert {j.id for j in select(candidates, Query(), NOW)} == {
        "greenhouse",
        "lever",
        "workday",
        "ashby",
    }
    assert {j.id for j in select(candidates, Query(platforms=["greenhouse", "lever"]), NOW)} == {
        "greenhouse",
        "lever",
    }
    assert [
        j.id
        for j in select(candidates, Query(platforms=["greenhouse", "lever"], states=["MA"]), NOW)
    ] == ["greenhouse"]


def test_platform_cli_aliases_reach_query_selection(tmp_path, monkeypatch, capsys):
    candidates = []
    for platform in ["greenhouse", "lever", "workday"]:
        candidate = job(platform)
        candidate.ats = platform
        candidates.append(candidate)
    data = tmp_path / "data"
    data.mkdir()
    for filename, document in [
        ("jobs.json", Catalog(jobs=candidates)),
        ("source_health.json", HealthDocument()),
        ("overrides.json", OverrideDocument()),
    ]:
        (data / filename).write_text(document.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("meche_tracker.cli.utc_now", lambda: NOW)

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "query",
                "--platform",
                "greenhouse",
                "--ats",
                "lever",
                "--state",
                "MA",
            ]
        )
        == 0
    )
    assert {j["id"] for j in json.loads(capsys.readouterr().out)} == {"greenhouse", "lever"}

    with pytest.raises(SystemExit) as error:
        parser().parse_args(["query", "--platform", "unsupported"])
    assert error.value.code == 2


def test_pay_filter_requires_comparable_units():
    annual = job(pay=Pay(minimum=75000, currency="USD", unit="year"))
    hourly = job("hourly", pay=Pay(minimum=35, currency="USD", unit="hour"))
    assert select(
        [annual, hourly], Query(minimum_pay=30, pay_unit="hour", pay_currency="USD"), NOW
    ) == [hourly]
    with pytest.raises(ValueError, match="pay-unit"):
        select([hourly], Query(minimum_pay=30), NOW)


def test_stale_verification_excluded_even_with_recent_feed_presence():
    candidate = job()
    candidate.last_verified_at = NOW - timedelta(hours=49)
    assert not select([candidate], Query(), NOW)
    assert select([candidate], Query(include_stale=True), NOW)
    assert select([candidate], Query(statuses=[], include_stale=True, unverified_only=True), NOW)
    fresh = job("fresh")
    suspected = job("suspected")
    suspected.availability = "suspected_closed"
    assert select(
        [fresh, suspected], Query(statuses=[], include_stale=True, unverified_only=True), NOW
    ) == [suspected]


def test_boolean_cli_flags():
    args = parser().parse_args(["query", "--housing", "yes", "--sponsorship", "no"])
    assert args.housing is True and args.sponsorship is False


def test_render_escapes_sources_and_rejects_unsafe_links():
    candidate = job(title="Mechanical Intern | [click] <script>")
    candidate.posting.source_url = "javascript:alert(1)"
    output = table([candidate])
    assert "javascript:" not in output
    assert r"\|" in output and r"\[click\]" in output


@pytest.mark.parametrize(
    ("platform", "label"),
    [("workday", "Workday"), ("greenhouse", "Greenhouse"), ("lever", "Lever"), ("ashby", "Ashby")],
)
def test_job_table_displays_readable_ats_between_company_and_role(platform, label):
    candidate = job()
    candidate.ats = platform
    output = table([candidate])
    assert output.splitlines()[0].startswith("| Company | ATS | Role |")
    assert f"| Test Co | {label} | [Mechanical Engineering Intern" in output


def test_platform_views_and_counts_use_full_eligible_catalog():
    platforms = {
        "workday": ("Workday", "https://example.myworkdayjobs.com/Careers"),
        "greenhouse": ("Greenhouse", "https://job-boards.greenhouse.io/example"),
        "lever": ("Lever", "https://jobs.lever.co/example"),
        "ashby": ("Ashby", "https://jobs.ashbyhq.com/example"),
    }
    employers = [
        Employer(
            id=platform,
            name=f"{label} Employer",
            website="https://example.com",
            industry="Industrial",
            sources=[
                SourceConfig(
                    id=f"{platform}-main",
                    ats=platform,
                    board_url=url,
                    enabled=True,
                    verified_at=NOW,
                )
            ],
        )
        for platform, (label, url) in platforms.items()
    ]

    def candidate(platform, id, **values):
        result = job(id, **values)
        result.ats = platform
        result.employer_id = platform
        result.employer_name = f"{platforms[platform][0]} Employer"
        result.source_id = f"{platform}-main"
        result.posting.source_url = f"https://example.com/job/{id}"
        return result

    jobs = [candidate("workday", f"wd-{index}") for index in range(201)]
    jobs.extend(
        [
            candidate("greenhouse", "gh-1", found=NOW - timedelta(days=1)),
            candidate("greenhouse", "gh-2", found=NOW - timedelta(days=1)),
            candidate("lever", "lever-1", found=NOW - timedelta(days=1)),
        ]
    )
    stale = candidate("greenhouse", "stale")
    stale.last_verified_at = NOW - timedelta(hours=49)
    excluded = candidate("lever", "excluded", title="Software Engineer Intern")
    graduate = candidate("ashby", "graduate", title="PhD Thermal Engineering Intern")
    assert excluded.classification.decision == "exclude"
    assert graduate.classification.graduate_only
    outputs = build_outputs(
        Catalog(jobs=[*jobs, stale, excluded, graduate], generated_at=NOW),
        HealthDocument(),
        employers,
        NOW,
    )

    expected = {"workday": 201, "greenhouse": 2, "lever": 1, "ashby": 0}
    for path in ["README.md", "views/index.md", "views/coverage.md"]:
        for platform, count in expected.items():
            label = platforms[platform][0]
            assert re.search(
                rf"\[{label}\]\((?:views/)?platform-{platform}\.md\)\s*\|\s*{count}\s*\|",
                outputs[path],
            ), (path, platform)
    assert "Showing 200 of 204" in outputs["README.md"]
    assert "| [Workday](views/platform-workday.md) | 201 | 200 |" in outputs["README.md"]
    assert "| [Greenhouse](views/platform-greenhouse.md) | 2 | 0 |" in outputs["README.md"]

    for platform, count in expected.items():
        body = outputs[f"views/platform-{platform}.md"]
        assert f"**{count}** matching opportunities" in body
        assert f"{platforms[platform][0]}: {count}" in body
    assert "No matching opportunities" in outputs["views/platform-ashby.md"]
    for path in ["views/platform-workday.md", "views/platform-workday-2.md"]:
        assert "Workday: 201" in outputs[path]
        assert "Greenhouse: 0" in outputs[path]
        assert "**201** matching opportunities" in outputs[path]
    for path in ["views/all.md", "views/all-2.md"]:
        for platform, count in expected.items():
            assert f"{platforms[platform][0]}: {count}" in outputs[path]
        assert "**204** matching opportunities" in outputs[path]
    assert outputs["views/platform-workday.md"].count("https://example.com/job/") == 200
    assert outputs["views/platform-workday-2.md"].count("https://example.com/job/") == 1
    assert outputs["views/all-2.md"].count("https://example.com/job/") == 4
    for path, body in outputs.items():
        for line in body.splitlines():
            if line.startswith("| Company |"):
                assert line.startswith("| Company | ATS | Role |"), path


def test_generated_pages_paginate_and_preserve_unknown_date_group():
    employers = [
        Employer(
            id="test",
            name="Test Co",
            website="https://example.com",
            industry="Industrial",
            sources=[
                SourceConfig(
                    id="test-main",
                    ats="greenhouse",
                    board_url="https://job-boards.greenhouse.io/test",
                    enabled=True,
                    verified_at=NOW,
                )
            ],
        )
    ]
    jobs = [job(str(i), employer_date="2026-10-01" if i else None) for i in range(201)]
    outputs = build_outputs(
        Catalog(jobs=jobs, generated_at=NOW, initial_inventory_at=NOW),
        HealthDocument(),
        employers,
        NOW,
    )
    assert "Showing 200 of 201" in outputs["README.md"]
    assert "views/all-2.md" in outputs
    assert "Employer posting date unknown" in outputs["views/posted-2.md"]
    assert "Initial inventory" in outputs["README.md"]


def test_timestamps_cannot_be_naive():
    with pytest.raises(ValueError, match="timezone"):
        Catalog(generated_at=datetime(2026, 10, 8))


def test_software_eligibility_and_digital_tooling_do_not_imply_mechanical_work():
    software = posting(
        "Software Engineer Intern",
        "Pursuing a degree in mechanical engineering. Build full-stack software.",
    )
    firmware = posting(
        "Firmware Thermal Intern", "Design embedded software. Degree in computer engineering."
    )
    tooling = posting(
        "Supply Chain Tooling Engineer Intern",
        "Build TMS/WMS data pipelines. Pursuing Computer Science.",
    )
    assert all(classify(p).decision == "exclude" for p in [software, firmware, tooling])
    assert (
        classify(
            posting(
                "Supply Chain Intern", "Knowledge of manufacturing processes. Degree in Business."
            )
        ).decision
        == "exclude"
    )
    assert (
        classify(
            posting(
                "Supply Chain Intern",
                "Pursuing a degree in mechanical engineering. Supplier quality inspections.",
            )
        ).decision
        == "include"
    )


def test_required_advanced_degree_wins_over_completed_bachelors():
    c = classify(
        posting(
            "Graduate Engineer Intern",
            "Enrolled in a graduate program and holds a bachelor's degree.",
        )
    )
    assert c.graduate_only
    c = classify(
        posting(
            description="Undergraduate or graduate students welcome.\nBasic Qualifications\n"
            "Currently pursuing a Master's degree.\nPreferred Skills\nCAD experience."
        )
    )
    assert c.graduate_only
    c = classify(
        posting(
            description="Undergraduate or graduate students welcome.\nBasic Qualifications\n"
            "Fluid mechanics coursework.\nPreferred Skills And Experience\n"
            "Currently pursuing a Master's degree.\nCompensation\n$33/hr"
        )
    )
    assert not c.graduate_only
    c = classify(
        posting(
            description="Must be enrolled in a STEM program (undergraduate student in your third "
            "or fourth year of study, or enrolled in a Master's or Ph.D. program)."
        )
    )
    assert not c.graduate_only
    assert not classify(
        posting(description="Must be enrolled in a bachelor's degree or graduate program.")
    ).graduate_only
    assert not classify(
        posting(
            description="Must be enrolled in a bachelor's, master's or doctorate degree "
            "program in engineering."
        )
    ).graduate_only
    assert not classify(
        posting("Supply Chain Intern", "Maintain master data. Pursuing a degree in Business.")
    ).graduate_only


def test_salary_degree_bands_do_not_create_degree_requirements():
    c = classify(
        posting(
            description="Design CAD parts.\nCompensation: $41/hr Undergrad; $58/hr PhD students."
        )
    )
    assert c.degrees == [] and not c.graduate_only


def test_each_season_has_its_own_year_and_enrollment_is_not_a_term():
    c = classify(
        posting("Mechanical Intern Fall 2026 / Winter 2027", "Our summer internship program.")
    )
    assert [(t.season, t.year) for t in c.terms][:2] == [("fall", 2026), ("winter", 2027)]
    c = classify(
        posting(
            "Materials Engineer Intern",
            "Must be enrolled full time in Fall 2027.\n"
            "Be available to work for ten weeks during summer 2027.",
        )
    )
    assert [(t.season, t.year) for t in c.terms] == [("summer", 2027)]
    c = classify(posting("Mechanical Co-op Summer and Fall 2027"))
    assert [(t.season, t.year) for t in c.terms] == [("summer", 2027), ("fall", 2027)]
    c = classify(posting("2027 Spring-Summer Co-op Mechanical Engineering"))
    assert [(t.season, t.year) for t in c.terms] == [("spring", 2027), ("summer", 2027)]


def test_benefit_duration_and_unsupported_ranges_are_not_program_lengths():
    assert (
        classify(posting(description="Benefits include 12 weeks of parental leave.")).duration_weeks
        is None
    )
    assert (
        classify(posting(description="This internship requires 16 to 24 weeks.")).duration_weeks
        is None
    )
    assert (
        classify(
            posting(description="Able to work full-time for 12 consecutive weeks.")
        ).duration_weeks
        == 12
    )


def test_explicit_non_internship_and_hardware_integration():
    assert (
        classify(posting("Contract Student Worker", "This is not an internship.")).decision
        == "exclude"
    )
    c = classify(
        posting(
            "Mechanical Intern",
            "At our company, internships, co-ops and full-time careers provide experience.",
        )
    )
    assert c.opportunity_types == ["internship"]


def test_non_engineering_qualifications_do_not_consume_later_manufacturing_context():
    c = classify(
        posting(
            "Environmental Health Safety Intern",
            "Required qualifications: Pursuing a degree in Occupational Health and Safety "
            "or Public Health. Experience in manufacturing environments. "
            "Support safety inspections.",
        )
    )
    assert c.decision == "exclude"
    assert (
        classify(
            posting(
                "Integration & Test Intern",
                "Hands-on hardware or test equipment experience. Spacecraft design.",
            )
        ).decision
        == "include"
    )
    assert (
        classify(
            posting(
                "Plasma Diagnostics Intern",
                "Bench test optical probes. Currently pursuing a PhD in Physics.",
            )
        ).decision
        == "review"
    )
