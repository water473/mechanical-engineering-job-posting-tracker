"""Conservative source normalization: retain unknowns rather than invent facts."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import UTC, date, datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from meche_tracker.models import Location, SourceConfig

US_STATES = dict(
    zip(
        "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
        "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split(),
        "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|"
        "Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|"
        "Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|"
        "New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|"
        "Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|"
        "Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|"
        "District of Columbia".split("|"),
        strict=True,
    )
)
_COUNTRY_NAMES = {
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "us": "US",
    "u.s.": "US",
    "canada": "CA",
    "ca": "CA",
    "united kingdom": "GB",
    "uk": "GB",
    "great britain": "GB",
    "germany": "DE",
    "france": "FR",
    "mexico": "MX",
    "india": "IN",
    "australia": "AU",
    "new zealand": "NZ",
    "singapore": "SG",
    "china": "CN",
    "japan": "JP",
    "netherlands": "NL",
    "ireland": "IE",
    "switzerland": "CH",
    "poland": "PL",
    "spain": "ES",
    "italy": "IT",
}
_ISO3_COUNTRIES = {
    "USA": "US",
    "CAN": "CA",
    "NZL": "NZ",
    "AUS": "AU",
    "GBR": "GB",
    "DEU": "DE",
    "FRA": "FR",
    "MEX": "MX",
    "IND": "IN",
    "SGP": "SG",
    "CHN": "CN",
    "JPN": "JP",
    "NLD": "NL",
    "IRL": "IE",
    "CHE": "CH",
    "POL": "PL",
    "ESP": "ES",
    "ITA": "IT",
}
_STUDENT = re.compile(
    r"\b(intern(?:ship)?s?|co[\s‐‑–-]?ops?|cooperative(?:\s+education)?|students?|"
    r"undergraduates?|placements?|summer|spring|winter|fall)\b",
    re.I,
)


def canonical_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Expected an absolute HTTP(S) source URL")
    if parsed.username or parsed.password:
        raise ValueError("Source URLs cannot contain credentials")
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"source", "ref", "referral", "gh_src", "lever-source"}
    ]
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, urlencode(query), ""))


def html_text(value: str | None) -> str:
    if not value:
        return ""
    # Greenhouse may HTML-encode its markup. Avoid double-decoding arbitrary text repeatedly.
    decoded = html.unescape(value)
    soup = BeautifulSoup(decoded, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return "\n".join(
        line
        for raw in soup.get_text(separator="\n").splitlines()
        if (line := re.sub(r"[ \t]+", " ", raw).strip())
    )


def fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def content_hash(value: Any) -> str:
    return fingerprint(value)


def parse_employer_date(
    raw: Any,
) -> tuple[str | None, Literal["timestamp", "date", "unknown"]]:
    if not isinstance(raw, str) or not raw.strip():
        return None, "unknown"
    raw = raw.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw).isoformat(), "date"
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).isoformat(), "timestamp"
    except ValueError:
        pass
    return None, "unknown"


def is_student_candidate(title: str, source: SourceConfig | None = None) -> bool:
    return bool(
        (source and source.student_only)
        or _STUDENT.search(title)
        or (source and any(re.search(p, title, re.I) for p in source.candidate_title_patterns))
    )


def _country(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("alpha2Code") or value.get("addressCountry") or value.get("descriptor")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return (
        _COUNTRY_NAMES.get(value.lower())
        or _ISO3_COUNTRIES.get(value.upper())
        or (value.upper() if len(value) == 2 else None)
    )


def _location(raw: str, country_hint: str | None, address: dict[str, Any]) -> Location:
    raw = re.sub(r"\s+", " ", raw).strip()
    country = _country(address.get("addressCountry"))
    state = address.get("addressRegion")
    city = address.get("addressLocality")
    if state:
        state = next(
            (code for code, name in US_STATES.items() if name.lower() == str(state).lower()),
            str(state),
        )
    # Names take precedence over a posting-wide hint (important for multi-country jobs).
    for name, code in sorted(_COUNTRY_NAMES.items(), key=lambda pair: -len(pair[0])):
        if len(name) > 3 and re.search(r"\b" + re.escape(name) + r"\b", raw, re.I):
            country = country or code
            break
    if not country:
        iso3 = re.search(r"\b(" + "|".join(_ISO3_COUNTRIES) + r")\b", raw)
        if iso3:
            country = _ISO3_COUNTRIES[iso3.group(1)]
        elif re.search(r"(?:^|[,;\s-])(?:USA|US|U\.S\.)(?:$|[,;\s])", raw):
            country = "US"
        elif re.search(r"(?:^|[,;\s-])(?:UK|GB)(?:$|[,;\s])", raw):
            country = "GB"
        elif re.search(r",\s*NZ(?:$|[,;\s])", raw):
            country = "NZ"
        elif re.search(r"(?:,\s*|\s+)(?:ON|BC|QC|AB|NS|NB|MB|SK|NL|PE)(?:$|[,\s])", raw):
            country = "CA"
    country = country or _country(country_hint)
    # Workday campuses commonly use strings such as US-MA-MARLBOROUGH-MA4.
    campus = re.match(r"^(?:US|USA)-([A-Z]{2})(?:-|\s|$)", raw)
    if campus and campus.group(1) in US_STATES and country in {None, "US"}:
        country = "US"
        state = state or campus.group(1)
    if not state and country in {None, "US"}:
        # An explicit abbreviation wins over a city that happens to be a state name.
        for code in US_STATES:
            if re.search(r",\s*" + code + r"(?:$|[,\s])", raw):
                state = code
                break
        if not state:
            for code, name in sorted(US_STATES.items(), key=lambda item: -len(item[1])):
                if re.search(r"\b" + re.escape(name) + r"\b", raw, re.I):
                    state = code
                    break
    country = country or ("US" if state in US_STATES else None)
    if not city and "," in raw:
        possible = raw.split(",", 1)[0].strip()
        if possible.lower() not in {"remote", "hybrid", "onsite", "united states", "usa"}:
            city = possible
    return Location(raw=raw, city=city, state=state, country=country)


def normalize_locations(value: Any, country_hint: str | None = None) -> list[Location]:
    """Normalize ATS strings/addresses without asserting a country for ambiguous cities."""
    values = value if isinstance(value, list) else [value]
    locations: list[Location] = []
    for entry in values:
        if entry is None:
            continue
        address: dict[str, Any] = {}
        hint = country_hint
        if isinstance(entry, dict):
            address = entry.get("address") or entry.get("postalAddress") or {}
            address = address.get("postalAddress", address)
            if not address and any(k in entry for k in ("addressLocality", "addressCountry")):
                address = entry
            hint = _country(entry.get("country")) or hint
            raw = entry.get("location") or entry.get("name") or entry.get("descriptor")
            raw = raw or ", ".join(
                str(address[k])
                for k in ("addressLocality", "addressRegion", "addressCountry")
                if address.get(k)
            )
        else:
            raw = entry
        if not isinstance(raw, str) or not raw.strip():
            continue
        for part in re.split(r"\s*[;|]\s*|\s+/\s+", raw):
            if part.strip():
                normalized = _location(part, hint, address)
                if normalized not in locations:
                    locations.append(normalized)
    return locations


def description_locations(description: str | None) -> list[Location]:
    """Extract US cities only from explicit role availability/location statements.

    This deliberately ignores company headquarters, office directories and travel
    destinations. It supplements ambiguous ATS location labels, never contradicts
    a structured country, and does not interpret bare city names as US locations.
    """
    if not description:
        return []
    anchors = re.compile(
        r"\b(?:internships?|co[\s-]?ops?|this (?:position|role)|these (?:positions|roles))"
        r"\s+(?:are|is|will be|may be)\s+(?:available|located|based|offered)\b"
        r"([^\n!?]{0,1200})",
        re.I,
    )
    city_state = re.compile(
        r"(?:^|[;,]|\band\b)\s*"
        r"([A-Z][A-Za-z .'-]*(?:\([A-Za-z .'-]+\))?),\s*([A-Z]{2})\b"
    )
    found: list[Location] = []
    for match in anchors.finditer(description):
        tail = match.group(1)
        start = re.search(r"\b(?:at|in)\s+", tail, re.I)
        if not start:
            continue
        text = tail[start.end() :]
        end = re.search(r"\b(?:" + "|".join(US_STATES) + r")\.(?:\s|$)", text)
        if end:
            text = text[: end.end()]
        for city, state in city_state.findall(text):
            if state not in US_STATES:
                continue
            for location in normalize_locations(f"{city.strip()}, {state}"):
                if location not in found:
                    found.append(location)
    return found
