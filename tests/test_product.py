from datetime import UTC, datetime, timedelta

import pytest

from meche_tracker.classification import classify, extract_pay
from meche_tracker.cli import parser
from meche_tracker.models import (
    Catalog,
    Employer,
    HealthDocument,
    Job,
    Location,
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
    candidate.posting.apply_url = "javascript:alert(1)"
    output = table([candidate])
    assert "javascript:" not in output
    assert r"\|" in output and r"\[click\]" in output


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
