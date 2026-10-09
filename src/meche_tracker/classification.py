"""Explainable mechanical/student classification independent of all ATS adapters."""

from __future__ import annotations

import re
from datetime import date

from .models import Classification, Pay, Posting, Term

SPECIALTIES = {
    "design-product": r"mechanical|product design|product development|CAD|SolidWorks|GD&T",
    "manufacturing-process": r"manufactur|process engineer|tooling|machining|production engineer",
    "test-validation": r"test engineer|validation|test fixture|environmental test|instrumentation",
    "thermal-fluids": r"thermal|\bfluid|heat transfer|thermodynamic|aerodynamic|\bCFD\b",
    "structures-materials": r"structur|\bmaterials?\b|finite element|\bFEA\b|stress analysis",
    "robotics-mechatronics": r"robotic|mechatronic|electromechanical|motion control",
    "quality-reliability": r"quality engineer|reliability|failure analysis|root cause",
    "hvac-building": r"\bHVAC\b|building systems|building mechanical|MEP engineer",
}
STUDENT_TITLE = re.compile(
    r"\bintern(?:ship)?s?\b|\bco[-\s]?op\b|\b(?:engineering )?student\b|"
    r"\bplacement\b|\b(?:summer|spring|fall|winter)\b.*\b(?:engineer|engineering|program)\b",
    re.I,
)
NOT_STUDENT = re.compile(
    r"\b(?:senior|principal|staff|director|manager)\b|new graduate|new grad|"
    r"for .{0,25}interns? only|intern.{0,15}conversion|return offer",
    re.I,
)
ME_TITLE = re.compile(
    r"mechanical|manufacturing|thermal|fluids?|aerodynamic|structur|product design|tooling|"
    r"mechatronic|acoustic|HVAC|propulsion|materials|quality engineer|reliability engineer|"
    r"industrial engineer|process engineer|test engineer|integration (?:&|and) test",
    re.I,
)
ME_MAJOR = re.compile(
    r"(?:pursuing|enrolled|major|degree|studying|education|qualifications|engineering disciplines)"
    r"[^.!?;:]{0,240}\b(?:mechanical(?: engineering)?|manufacturing engineering)\b",
    re.I,
)
PHYSICAL_WORK = re.compile(
    r"mechanical design|test fixtures?|thermal analysis|structural analysis|"
    r"heat transfer|CAD models?|SolidWorks|GD&T|machining|"
    r"(?:design|develop|improve).{0,40}manufacturing process|"
    r"finite element|electromechanical|hands.on hardware|spacecraft design|Siemens NX|"
    r"mechanical reasoning|engineering teams.{0,60}design, manufacturing",
    re.I,
)
SOFTWARE_TITLE = re.compile(
    r"software|firmware|front.?end|back.?end|data scien|machine learning", re.I
)
SOFTWARE_WORK = re.compile(r"full.stack|embedded software|data pipelines|TMS|WMS|MRP", re.I)
SEASON = re.compile(r"\b(winter|spring|summer|fall|autumn)\b", re.I)
DATE_PATTERN = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def _statements(text: str, pattern: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [s.strip()[:600] for s in sentences if re.search(pattern, s, re.I)][:8]


def _benefit(text: str, positive: str, negative: str) -> bool | None:
    # A negative statement about one benefit must not cross into another sentence.
    positive = positive.replace(".{", "[^.;]{")
    negative = negative.replace(".{", "[^.;]{")
    if re.search(negative, text, re.I):
        return False
    if re.search(positive, text, re.I):
        return True
    return None


def _terms(title: str, text: str) -> list[Term]:
    terms = []
    for fragment in [title, text]:
        for match in SEASON.finditer(fragment):
            before = fragment[max(0, match.start() - 100) : match.start()]
            after = fragment[match.end() : match.end() + 35]
            if fragment != title:
                # Enrollment/return-to-school seasons are eligibility, not program terms.
                context = re.split(r"[.\n]", before)[-1]
                if re.search(r"enroll|return.{0,30}(?:school|university)|graduat", context, re.I):
                    continue
                if not re.search(
                    r"intern|co[- ]?op|program|term|during|work|begin|start|commit|available",
                    before,
                    re.I,
                ):
                    continue
            # Bind each season to its own adjacent year, never the last year in a window.
            following = re.match(r"\s*[,/-]?\s*(20\d{2})\b", after)
            preceding = re.search(r"\b(20\d{2})\s*$", before)
            joined_after = re.match(
                r"(?:\s*(?:and|/|-|&)\s*(?:winter|spring|summer|fall|autumn)){1,3}"
                r"\s+(20\d{2})\b",
                after,
                re.I,
            )
            joined_before = re.search(
                r"\b(20\d{2})\s+(?:(?:winter|spring|summer|fall|autumn)"
                r"\s*(?:and|/|-|&)\s*){1,3}$",
                before,
                re.I,
            )
            year = following or preceding or joined_after or joined_before
            season = "fall" if match.group(1).lower() == "autumn" else match.group(1).lower()
            term = Term(season=season, year=int(year.group(1)) if year else None)
            if term not in terms:
                terms.append(term)
    known = {term.season for term in terms if term.year is not None}
    return [term for term in terms if term.year is not None or term.season not in known]


def _education(text: str, title: str) -> tuple[list[str], bool]:
    # Salary bands and employee benefits are not evidence of applicant degree eligibility.
    cutoff = re.search(
        r"(?im)^\s*(?:compensation(?: and (?:other )?benefits)?|intern pay range|"
        r"pay range|salary range|benefits)\s*:?",
        text,
    )
    education = text[: cutoff.start()] if cutoff else text
    education = re.sub(r"\s+", " ", education)
    tokens = {
        "bachelors": r"bachelor|undergrad(?:uate)?|\bB\.?S\.?\b|\bB\.?Eng\b",
        "masters": r"\bmaster(?:[’']s|s)\b|\bmaster (?:degree|program)\b|\bM\.?S\.?\b",
        "phd": r"\bPh\.?D\.?\b|doctoral|doctorate",
    }
    degrees = []
    for degree, pattern in tokens.items():
        if re.search(pattern, title, re.I):
            degrees.append(degree)
            continue
        for match in re.finditer(pattern, education, re.I):
            context = education[max(0, match.start() - 140) : match.end() + 140]
            if re.search(
                r"pursu|enroll|degree|student|studying|major|program|hold|completed", context, re.I
            ):
                degrees.append(degree)
                break
    # A preferred advanced degree cannot override explicitly eligible undergraduates.
    mandatory = re.sub(
        r"(?:preferred (?:skills(?: and experience)?|qualifications)|"
        r"bonus (?:skills|qualifications))"
        r".*?(?=additional requirements|compensation|benefits|$)",
        " ",
        education,
        flags=re.I,
    )
    graduate_only = bool(
        re.search(
            r"\bPh\.?D|doctoral|graduate (?:student|engineer)|\bmaster(?:[’']s|s)\b",
            title,
            re.I,
        )
    )
    for match in re.finditer(
        r"(?:pursuing|enrolled in|must|requires?|required)[^.!?;]{0,150}"
        r"(?:master(?:[’']s|s)\b|\bPh\.?D\.?\b|doctoral|graduate program)",
        mandatory,
        re.I,
    ):
        context = mandatory[max(0, match.start() - 180) : match.end()]
        context = re.split(r"[.!?;]\s+", context)[-1]
        alternative = re.search(tokens["bachelors"], context, re.I) and not re.search(
            r"(?:holds?|completed|earned).{0,40}(?:bachelor|\bB\.?S\b)", context, re.I
        )
        if not alternative:
            graduate_only = True
    return degrees, graduate_only


def _duration(text: str) -> float | None:
    for match in re.finditer(r"\b(\d+(?:\.\d+)?)\s*[- ]?(?:consecutive\s+)?weeks?\b", text, re.I):
        before = text[max(0, match.start() - 100) : match.start()]
        after = text[match.end() : match.end() + 55]
        if re.search(r"\d+\s*(?:to|[-–—])\s*$", before):
            continue  # A range is not an exact duration.
        context = before + match.group(0) + after
        if re.search(r"parental|maternity|paternity|vacation|paid leave", context, re.I):
            continue
        if re.search(
            r"internship|co[- ]?op|program|assignment|commit|work full.time|duration", context, re.I
        ):
            return float(match.group(1))
    return None


def _program_date(text: str, prefix: str) -> date | None:
    match = re.search(prefix + r"[^.\n]{0,40}?(20\d{2}-\d{2}-\d{2})", text, re.I)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            return None
    return None


def classify(posting: Posting, *, today: date | None = None) -> Classification:
    today = today or date.today()
    original = posting.description or ""
    text = re.sub(r"\s+", " ", original)
    title = posting.title
    combined = f"{title}\n{text}"
    result = Classification()
    if re.search(
        r"early[- ]interest|expression of interest|not yet.{0,35}(?:recruit|interview)",
        combined,
        re.I,
    ):
        result.kind = "early_interest"
    elif re.search(r"talent (?:pool|community)|future opportunities|join our talent", title, re.I):
        result.kind = "talent_pool"

    coop = bool(
        re.search(r"\bco[-\s]?op\b", f"{title} {posting.employment_type or ''}", re.I)
    ) or bool(
        re.search(
            r"(?:this|the) (?:position|role|opportunity|program).{0,45}\bco[-\s]?op\b", text, re.I
        )
    )
    internship = (
        bool(re.search(r"\bintern(?:ship)?s?\b", title, re.I))
        or bool(re.search(r"intern", posting.employment_type or "", re.I))
        or bool(re.search(r"(?:this|the|our|paid|summer) internship\b", text, re.I))
    )
    if internship:
        result.opportunity_types.append("internship")
    if coop:
        result.opportunity_types.append("co-op")
    if not result.opportunity_types and STUDENT_TITLE.search(title):
        result.opportunity_types.append("internship")

    result.degrees, result.graduate_only = _education(original, title)
    result.terms = _terms(title, original)
    result.start_date = _program_date(text, r"(?:start(?:s|ing| date)?|begin(?:s|ning)?)")
    result.end_date = _program_date(text, r"(?:end(?:s|ing| date)?|through|until)")
    result.duration_weeks = _duration(text)
    result.specialties = [
        name for name, pattern in SPECIALTIES.items() if re.search(pattern, combined, re.I)
    ]
    result.sponsorship = _benefit(
        text,
        r"(?:visa|immigration|work authorization) sponsorship (?:is )?(?:available|provided)|"
        r"(?:provide|offer|eligible for) (?:visa |immigration )?sponsorship",
        r"(?:no|without|not.{0,25}|unable.{0,25}) (?:visa |immigration )?sponsorship|"
        r"(?:do|does|will) not (?:provide|offer) (?:visa |immigration )?sponsorship|"
        r"(?:will|can|do|does) not sponsor|cannot sponsor",
    )
    result.housing = _benefit(
        text,
        r"housing (?:is )?(?:provided|available|assistance|stipend)|"
        r"(?:provide|offer).{0,20}housing|housing and relocation assistance",
        r"(?:no|not.{0,30}|without).{0,20}(?:housing assistance|housing provided)|"
        r"housing.{0,20}(?:not provided|unavailable)",
    )
    result.relocation = _benefit(
        text,
        r"relocation (?:is )?(?:provided|available|assistance|stipend)|"
        r"(?:provide|offer).{0,20}relocation",
        r"(?:no|not.{0,30}|without).{0,20}relocation|relocation.{0,20}(?:not provided|unavailable)",
    )
    result.eligibility_statements = _statements(
        text,
        r"citizen|U\.?S\.? person|export control|clearance|work authoriz|sponsor|"
        r"return.{0,20}(?:school|university)|currently enrolled",
    )

    explicitly_not_internship = re.search(r"this is not an internship\b", text, re.I)
    if (
        NOT_STUDENT.search(title)
        or not result.opportunity_types
        or (explicitly_not_internship and not coop)
    ):
        result.decision = "exclude"
        result.reasons.append("not a current-student internship or co-op")
    elif result.end_date is not None and result.end_date < today:
        result.decision = "exclude"
        result.reasons.append("explicit program end date has passed")
    elif not any(location.country == "US" for location in posting.locations):
        known_countries = [loc.country for loc in posting.locations if loc.country]
        result.decision = "exclude" if known_countries else "review"
        result.reasons.append("no explicit U.S. location")
    elif SOFTWARE_TITLE.search(title):
        result.decision = "exclude"
        result.reasons.append("software-focused responsibilities are outside mechanical coverage")
    elif (
        re.search(r"tooling", title, re.I)
        and SOFTWARE_WORK.search(text)
        and not ME_MAJOR.search(text)
    ):
        result.decision = "exclude"
        result.reasons.append("digital tooling without mechanical engineering responsibilities")
    elif ME_TITLE.search(title) or ME_MAJOR.search(text) or PHYSICAL_WORK.search(text):
        result.decision = "include"
        result.reasons.append(
            "mechanical title, eligible major, or physical engineering responsibilities"
        )
    else:
        potential = re.search(
            r"engineer|hardware|research|technical|design|diagnostic|navigation|controls|GNC",
            title,
            re.I,
        )
        result.decision = "review" if potential else "exclude"
        result.reasons.append(
            "mechanical relevance requires review"
            if potential
            else "no mechanical engineering evidence"
        )
    return result


def extract_pay(description: str) -> Pay | None:
    """Use explicitly stated dollar amounts and intervals; never annualize a wage."""
    match = re.search(
        r"\$\s*(\d[\d,.]*(?:[kK])?)\s*"
        r"(?:[-–—]|to)?\s*\$?\s*(\d[\d,.]*(?:[kK])?)?\s*"
        r"(?:USD\s*)?(?:/|per\s+|an?\s+)?"
        r"(hour(?:ly)?|hr\b|year|annual(?:ly)?|annum|month|week)",
        description,
        re.I,
    )
    if not match:
        return None

    def amount(value: str) -> float:
        return float(value.rstrip("kK").replace(",", "")) * (1000 if value[-1] in "kK" else 1)

    unit = match.group(3).lower()
    interval = (
        "hour"
        if unit.startswith(("hour", "hr"))
        else ("year" if unit.startswith(("year", "annual", "annum")) else unit)
    )
    return Pay(
        minimum=amount(match.group(1)),
        maximum=amount(match.group(2) or match.group(1)),
        currency="USD",
        unit=interval,
        raw=match.group(0),
    )
