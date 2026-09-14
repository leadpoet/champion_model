"""Deterministic contact enrichment for opted-in Arena rounds."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from experiments.harness_bakeoff.models import ContactResult


CONTACT_POLICY = "contacts_v1"
_PROFILE_LIMIT_PER_COMPANY = 3
_EMAIL_PREVERIFY_LIMIT = 3
_ROLE_QUERY_HINT_LIMIT = 3
_ROLE_QUERY_HINT_CHARS = 100
_GENERIC_MAILBOXES = frozenset(
    {
        "admin",
        "billing",
        "careers",
        "contact",
        "customerservice",
        "hello",
        "help",
        "hr",
        "info",
        "jobs",
        "legal",
        "marketing",
        "office",
        "privacy",
        "recruiting",
        "sales",
        "security",
        "support",
        "team",
    }
)
_TITLE_EXPANSIONS = {
    "ceo": "chief executive officer",
    "cfo": "chief financial officer",
    "cio": "chief information officer",
    "cmo": "chief marketing officer",
    "coo": "chief operating officer",
    "cro": "chief revenue officer",
    "cto": "chief technology officer",
    "evp": "executive vice president",
    "svp": "senior vice president",
    "vp": "vice president",
}
_SENIORITY_BOILERPLATE = {
    "c_level": frozenset(
        {
            "chief",
            "executive",
            "founder",
            "managing",
            "officer",
            "owner",
            "partner",
            "president",
        }
    ),
    "vp": frozenset({"executive", "president", "senior", "vice"}),
    "head": frozenset({"head"}),
    "director": frozenset({"director", "executive", "managing", "senior"}),
    "manager": frozenset({"manager", "senior"}),
}
_LEGAL_SUFFIXES = frozenset(
    {
        "co",
        "company",
        "corp",
        "corporation",
        "gmbh",
        "inc",
        "incorporated",
        "limited",
        "llc",
        "ltd",
        "plc",
    }
)
_COUNTRY_ALIASES = {
    "australia": "AU",
    "canada": "CA",
    "france": "FR",
    "germany": "DE",
    "great britain": "GB",
    "india": "IN",
    "ireland": "IE",
    "new zealand": "NZ",
    "singapore": "SG",
    "u k": "GB",
    "uk": "GB",
    "united kingdom": "GB",
    "united states": "US",
    "united states of america": "US",
    "u s": "US",
    "u s a": "US",
    "usa": "US",
}
_US_REGION_NAMES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AS": "American Samoa",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "GU": "Guam",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "MP": "Northern Mariana Islands",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "PR": "Puerto Rico",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "UM": "United States Minor Outlying Islands",
    "VT": "Vermont",
    "VA": "Virginia",
    "VI": "United States Virgin Islands",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


ProviderCall = Callable[[str, dict[str, Any]], Any]


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _norm(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", _text(value))
    letters = "".join(
        character for character in raw if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[\W_]+", " ", letters.casefold()).split())


_US_REGION_CODES = {_norm(name): code for code, name in _US_REGION_NAMES.items()}
_US_REGION_CODES["washington dc"] = "DC"


def _bounded_strings(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item)
        identity = text.casefold()
        if text and identity not in seen:
            seen.add(identity)
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _canonical_linkedin_profile(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if (
        host not in {"linkedin.com", "www.linkedin.com"}
        or len(parts) != 2
        or parts[0].casefold() != "in"
        or not parts[1]
    ):
        return ""
    return urlunsplit(("https", "www.linkedin.com", f"/in/{parts[1]}/", "", ""))


def _linkedin_company_slug(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if (
        host not in {"linkedin.com", "www.linkedin.com"}
        or len(parts) != 2
        or parts[0].casefold() != "company"
    ):
        return ""
    return _norm(parts[1])


def _domain(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlsplit(raw).hostname or "").casefold().rstrip(".")
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return ""
    host = host.removeprefix("www.")
    if "." not in host:
        return ""
    return host


def _company_name(value: Any) -> str:
    words = _norm(value).split()
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def _us_region_code(value: Any) -> str:
    normalized = _norm(value)
    code = _US_REGION_CODES.get(normalized, "")
    if not code:
        parts = normalized.split()
        candidate = parts[-1].upper() if len(parts) in {1, 2} else ""
        if len(parts) == 2 and parts[0] != "us":
            candidate = ""
        code = candidate if candidate in _US_REGION_NAMES else ""
    return code


def _explicit_us_region_code(value: Any) -> str:
    parts = _norm(value).split()
    if len(parts) != 2 or parts[0] != "us":
        return ""
    code = parts[1].upper()
    return code if code in _US_REGION_NAMES else ""


def _harvestapi_region(value: Any, *, allow_bare_us_region: bool) -> str:
    code = _explicit_us_region_code(value)
    if not code and allow_bare_us_region:
        code = _us_region_code(value)
    return _US_REGION_NAMES.get(code, _text(value))


def _harvestapi_country(value: Any) -> str:
    """Use an established provider-compatible full country name."""

    text = _text(value)
    if text.casefold() == "gb":
        return "United Kingdom"
    if len(text) != 2:
        return text
    code = text.upper()
    names = [name for name, resolved in _COUNTRY_ALIASES.items() if resolved == code]
    return names[0].title() if len(names) == 1 else text


def _unwrap(value: Any, *, require_success: bool = False) -> Any:
    current = value
    for _ in range(8):
        if not isinstance(current, Mapping):
            return current
        if require_success:
            status = current.get("status")
            if (
                current.get("ok") is False
                or current.get("success") is False
                or bool(current.get("error"))
                or (
                    isinstance(status, str)
                    and status.strip().casefold()
                    in {"error", "failed", "failure"}
                )
                or (type(status) is int and status >= 400)
            ):
                return None
        moved = False
        for key in (
            "toolResponse",
            "tool_response",
            "rawV2",
            "raw_v2",
            "raw",
            "result",
            "data",
            "output",
        ):
            child = current.get(key)
            if isinstance(child, (Mapping, list, tuple)) and child is not current:
                current = child
                moved = True
                break
        if not moved:
            return current
    return current


def _company_slug_not_found(value: Any, current_companies: Any) -> bool:
    """Match only Harvest's explicit company-slug lookup failure."""

    if isinstance(value, Mapping) and (
        value.get("ok") is False
        or value.get("success") is False
        or (
            isinstance(value.get("status"), str)
            and value["status"].strip().casefold() in {"error", "failed", "failure"}
        )
    ):
        return False
    document = _unwrap(value)
    requested_slug = _linkedin_company_slug(current_companies)
    if not isinstance(document, Mapping) or not requested_slug:
        return False
    errors = document.get("error")
    if (
        type(document.get("status")) is not int
        or document["status"] != 400
        or not isinstance(errors, Sequence)
        or isinstance(errors, (str, bytes, bytearray))
        or len(errors) != 1
        or not isinstance(errors[0], Mapping)
        or type(errors[0].get("status")) is not int
        or errors[0]["status"] != 404
    ):
        return False
    match = re.fullmatch(
        r"Company or school not found by slug: (.+)\. Please check the URL\.",
        str(errors[0].get("error") or ""),
    )
    return match is not None and _norm(match.group(1)) == requested_slug


def _profiles(value: Any, depth: int = 0) -> list[Mapping[str, Any]]:
    if depth > 5:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result: list[Mapping[str, Any]] = []
        for item in list(value)[:25]:
            result.extend(_profiles(item, depth + 1))
        return result
    if not isinstance(value, Mapping):
        return []
    profile_keys = {
        "publicIdentifier",
        "public_identifier",
        "linkedinUrl",
        "linkedin_url",
        "firstName",
        "lastName",
        "currentPosition",
        "currentPositions",
        "experience",
    }
    result = [value] if profile_keys.intersection(value) else []
    for key in ("elements", "items", "profiles", "profile", "element", "results"):
        if key in value:
            result.extend(_profiles(value[key], depth + 1))
    return result


def _profile_linkedin(profile: Mapping[str, Any]) -> str:
    direct = _canonical_linkedin_profile(
        profile.get("linkedinUrl")
        or profile.get("linkedin_url")
        or profile.get("profileUrl")
        or profile.get("profile_url")
        or profile.get("url")
    )
    if direct:
        return direct
    identifier = _text(
        profile.get("publicIdentifier") or profile.get("public_identifier")
    )
    return (
        _canonical_linkedin_profile(f"linkedin.com/in/{identifier}")
        if identifier
        else ""
    )


def _profile_name(profile: Mapping[str, Any]) -> str:
    first = _text(profile.get("firstName") or profile.get("first_name"))
    last = _text(profile.get("lastName") or profile.get("last_name"))
    return f"{first} {last}".strip() or _text(
        profile.get("fullName") or profile.get("full_name") or profile.get("name")
    )


def _is_current(position: Mapping[str, Any]) -> bool:
    if position.get("current") is False or position.get("isCurrent") is False:
        return False
    if position.get("current") is True or position.get("isCurrent") is True:
        return True
    end = position.get("endDate", position.get("end_date"))
    if end in (None, ""):
        return True
    if isinstance(end, Mapping):
        if not any(end.values()):
            return True
        end = end.get("text")
    return _norm(end) in {"present", "current", "now"}


def _current_positions(profile: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for key in ("currentPosition", "currentPositions", "current_position"):
        current = profile.get(key)
        if isinstance(current, Mapping) and _is_current(current):
            result.append(current)
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            result.extend(
                item
                for item in current[:10]
                if isinstance(item, Mapping) and _is_current(item)
            )
    experience = profile.get("experience") or profile.get("experiences") or []
    if isinstance(experience, Sequence) and not isinstance(
        experience, (str, bytes, bytearray)
    ):
        result.extend(
            item
            for item in experience[:25]
            if isinstance(item, Mapping) and _is_current(item)
        )
    return result


def _position_title(position: Mapping[str, Any]) -> str:
    return _text(
        position.get("title")
        or position.get("position")
        or position.get("role")
        or position.get("jobTitle")
    )


def _position_company(position: Mapping[str, Any]) -> dict[str, str]:
    nested = position.get("company")
    company = nested if isinstance(nested, Mapping) else {}
    return {
        "name": _company_name(
            position.get("companyName")
            or position.get("company_name")
            or position.get("employerName")
            or company.get("name")
        ),
        "domain": _domain(
            position.get("companyDomain")
            or position.get("company_domain")
            or company.get("domain")
            or company.get("website")
        ),
        "linkedin_slug": _linkedin_company_slug(
            position.get("companyLinkedinUrl")
            or position.get("companyLinkedInUrl")
            or company.get("linkedinUrl")
            or company.get("linkedin_url")
        ),
    }


def _expected_company(company: Mapping[str, Any]) -> dict[str, str]:
    return {
        "name": _company_name(company.get("company_name")),
        "domain": _domain(company.get("company_website")),
        "linkedin_slug": _linkedin_company_slug(company.get("company_linkedin")),
    }


def _company_matches(expected: Mapping[str, str], observed: Mapping[str, str]) -> bool:
    name_matches = bool(
        expected.get("name") and expected["name"] == observed.get("name")
    )
    strong: list[bool] = []
    expected_domain = expected.get("domain", "")
    observed_domain = observed.get("domain", "")
    if expected_domain and observed_domain:
        strong.append(
            expected_domain == observed_domain
            or (
                name_matches
                and (
                    observed_domain.endswith("." + expected_domain)
                    or expected_domain.endswith("." + observed_domain)
                )
            )
        )
    expected_slug = expected.get("linkedin_slug", "")
    observed_slug = observed.get("linkedin_slug", "")
    if expected_slug and observed_slug:
        strong.append(expected_slug == observed_slug)
    if strong:
        return all(strong)
    return name_matches


def _search_company_matches(
    expected: Mapping[str, str], observed: Mapping[str, str]
) -> bool:
    """Match Harvest search metadata whose company URL uses a numeric ID."""

    if not expected.get("name") or expected["name"] != observed.get("name"):
        return False
    observed_slug = observed.get("linkedin_slug", "")
    expected_slug = expected.get("linkedin_slug", "")
    if observed_slug and not observed_slug.isdecimal() and expected_slug:
        return observed_slug == expected_slug
    return True


def _numeric_search_company_reference(observed: Mapping[str, str]) -> bool:
    """Identify Harvest search positions that expose only a numeric company URL."""

    linkedin_slug = observed.get("linkedin_slug", "")
    return bool(linkedin_slug) and linkedin_slug.isdecimal()


def _normalized_title(value: Any) -> str:
    expanded: list[str] = []
    for word in _norm(value).split():
        expanded.extend(_TITLE_EXPANSIONS.get(word, word).split())
    return " ".join(word for word in expanded if word not in {"and", "of", "the"})


def _seniority(value: Any) -> str:
    title = _normalized_title(value)
    if re.search(r"\bchief\b.*\bofficer\b", title):
        return "c_level"
    if (
        "managing partner" in title
        or "managing director" in title
        or re.search(r"\b(owner|founder)\b", title)
        or ("president" in title and "vice president" not in title)
    ):
        return "c_level"
    if "vice president" in title:
        return "vp"
    if "head" in title.split():
        return "head"
    if "director" in title:
        return "director"
    if "manager" in title:
        return "manager"
    return "other"


def _seniority_matches(title: str, requested: Any) -> bool:
    raw = _text(requested).casefold()
    target = _norm(requested)
    if not target:
        return True
    actual = _seniority(title)
    if ("+" in raw and target in {"vp", "vice president"}) or target in {
        "vp above",
        "vp and above",
        "vice president above",
        "vice president and above",
    }:
        return actual in {"vp", "c_level"}
    if ("+" in raw and target == "director") or target in {
        "director above",
        "director and above",
    }:
        return actual in {"director", "head", "vp", "c_level"}
    expected = {
        "c level": "c_level",
        "c suite": "c_level",
        "executive": "c_level",
        "vp": "vp",
        "vice president": "vp",
        "head": "head",
        "head of": "head",
        "director": "director",
        "manager": "manager",
    }.get(target)
    return actual == expected if expected else False


def _role_seniority_matches(
    title: str, targets: Sequence[str], requested_seniority: Any
) -> bool:
    """Use explicit seniority, or the seniority stated by target roles."""

    if _text(requested_seniority):
        return _seniority_matches(title, requested_seniority)
    target_levels = {
        level for target in targets if (level := _seniority(target)) != "other"
    }
    return not target_levels or _seniority(title) in target_levels


def _is_bare_seniority_prefix(value: str) -> bool:
    """Accept punctuation after a known seniority prefix, not a second title."""

    normalized = _normalized_title(value)
    if not normalized:
        return False
    raw = _norm(value)
    if raw in _TITLE_EXPANSIONS or normalized in _TITLE_EXPANSIONS.values():
        return True
    seniority = _seniority(value)
    return seniority != "other" and set(normalized.split()).issubset(
        _SENIORITY_BOILERPLATE[seniority]
    )


def _validated_role_query_hints(
    icp: Mapping[str, Any], value: Sequence[str] | None
) -> tuple[str, ...]:
    """Validate bounded discovery hints without treating them as role matches."""

    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError("role_query_hints must be a list")
    if len(value) > _ROLE_QUERY_HINT_LIMIT:
        raise ValueError(
            f"role_query_hints cannot contain more than {_ROLE_QUERY_HINT_LIMIT} titles"
        )
    targets = _bounded_strings(icp.get("target_roles"), limit=70)
    requested_seniority = icp.get("target_seniority")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("each role_query_hint must be a string")
        hint = item.strip()
        if (
            not hint
            or len(hint) > _ROLE_QUERY_HINT_CHARS
            or any(unicodedata.category(character).startswith("C") for character in item)
        ):
            raise ValueError("each role_query_hint must be a bounded single title")
        if "," in hint:
            if hint.count(",") != 1:
                raise ValueError("each role_query_hint must be a bounded single title")
            prefix, suffix = (part.strip() for part in hint.split(",", 1))
            if (
                not prefix
                or not suffix
                or not _is_bare_seniority_prefix(prefix)
                or _seniority(suffix) != "other"
            ):
                raise ValueError("each role_query_hint must be a bounded single title")
            hint = f"{prefix} {suffix}"
        normalized = _norm(hint)
        if not normalized or normalized in seen:
            raise ValueError("role_query_hints must be distinct")
        if not _role_seniority_matches(hint, targets, requested_seniority):
            raise ValueError("role_query_hints must match the requested seniority")
        seen.add(normalized)
        result.append(hint)
    return tuple(result)


def _role_matches(title: str, targets: Sequence[str], requested_seniority: Any) -> bool:
    actual = _normalized_title(title)
    if not actual or not _seniority_matches(title, requested_seniority):
        return False
    actual_words = actual.split()
    for target in targets:
        normalized = _normalized_title(target)
        if normalized == actual:
            return True
        target_words = normalized.split()
        width = len(target_words)
        if width < 2:
            continue
        # Allow modifiers such as "VP Software Engineering", while keeping
        # the title words in order. The independent scorer still judges fit.
        matched = 0
        for word in actual_words:
            if word == target_words[matched]:
                matched += 1
                if matched == width:
                    return True
    return False


def _functional_title(value: Any) -> str:
    """Keep the role function while removing recognized seniority boilerplate."""

    normalized = _normalized_title(value)
    boilerplate = _SENIORITY_BOILERPLATE.get(_seniority(value))
    if not normalized or boilerplate is None:
        return ""
    return " ".join(word for word in normalized.split() if word not in boilerplate)


def _profile_location(profile: Mapping[str, Any]) -> dict[str, str]:
    location = profile.get("location")
    location = location if isinstance(location, Mapping) else {}
    parsed = location.get("parsed")
    parsed = parsed if isinstance(parsed, Mapping) else {}
    country_code = _text(
        profile.get("countryCode")
        or profile.get("country_code")
        or location.get("countryCode")
        or location.get("country_code")
        or parsed.get("countryCode")
        or parsed.get("country_code")
    ).upper()
    direct_country = _text(
        profile.get("country")
        or location.get("country")
        or parsed.get("country")
        or parsed.get("countryFull")
        or parsed.get("country_full")
    )
    if not country_code and len(direct_country) == 2 and direct_country.isalpha():
        country_code = direct_country.upper()
    if not country_code:
        country_code = _COUNTRY_ALIASES.get(_norm(direct_country), "")
    if len(country_code) != 2 or not country_code.isalpha():
        country_code = ""
    return {
        "country": country_code,
        "country_full": _text(
            profile.get("countryName")
            or profile.get("country_name")
            or location.get("countryName")
            or location.get("country_name")
            or parsed.get("countryFull")
            or parsed.get("country_full")
            or (direct_country if len(direct_country) != 2 else "")
        ),
        "region": _text(
            profile.get("region")
            or profile.get("state")
            or location.get("region")
            or location.get("state")
            or parsed.get("state")
            or parsed.get("regionCode")
            or parsed.get("region_code")
        ),
        "city": _text(
            profile.get("city") or location.get("city") or parsed.get("city")
        ),
    }


def _location_matches(
    location: Mapping[str, str], geography: Mapping[str, Any]
) -> bool:
    if not location.get("country"):
        return False
    constraints = {
        "country": _bounded_strings(geography.get("countries"), limit=70),
        "region": _bounded_strings(geography.get("regions"), limit=70),
        "city": _bounded_strings(geography.get("cities"), limit=70),
    }
    if constraints["country"]:
        allowed_codes = {
            _COUNTRY_ALIASES.get(_norm(item), _text(item).upper())
            for item in constraints["country"]
            if len(_text(item)) == 2 or _norm(item) in _COUNTRY_ALIASES
        }
        allowed_names = {
            _norm(item) for item in constraints["country"] if len(_text(item)) != 2
        }
        if (
            location["country"].upper() not in allowed_codes
            and _norm(location.get("country_full")) not in allowed_names
        ):
            return False
    if constraints["region"]:
        actual_region = _norm(location.get("region"))
        exact_match = any(
            actual_region == _norm(item)
            and (
                not _explicit_us_region_code(item)
                or location["country"].upper() == "US"
            )
            for item in constraints["region"]
        )
        equivalent_us_region = False
        if location["country"].upper() == "US":
            actual_code = _us_region_code(location.get("region"))
            equivalent_us_region = bool(actual_code) and actual_code in {
                _us_region_code(item) for item in constraints["region"]
            }
        if not exact_match and not equivalent_us_region:
            return False
    if constraints["city"] and _norm(location.get("city")) not in {
        _norm(item) for item in constraints["city"]
    }:
        return False
    return True


def _emails(profile: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for key in (
        "workEmail",
        "work_email",
        "professionalEmail",
        "professional_email",
        "email",
    ):
        value = profile.get(key)
        if isinstance(value, str):
            result.append(value)
    collection = profile.get("emails") or profile.get("emailAddresses") or []
    if isinstance(collection, Sequence) and not isinstance(
        collection, (str, bytes, bytearray)
    ):
        for item in collection[:20]:
            value = (
                item.get("email") or item.get("value")
                if isinstance(item, Mapping)
                else item
            )
            if isinstance(value, str):
                result.append(value)
    clean: list[str] = []
    seen: set[str] = set()
    for value in result:
        email = _text(value).casefold()
        local = email.partition("@")[0]
        mailbox = local.replace(".", "").replace("_", "").replace("-", "")
        valid_local = (
            len(local) <= 64
            and not local.startswith(".")
            and not local.endswith(".")
            and ".." not in local
        )
        if (
            _EMAIL_RE.fullmatch(email)
            and valid_local
            and mailbox not in _GENERIC_MAILBOXES
            and email not in seen
        ):
            seen.add(email)
            clean.append(email)
    return clean


def _zerobounce_accepts_email(value: Any, email: str) -> bool:
    """Classify one exact-address ZeroBounce response without guessing."""

    result = _unwrap(value, require_success=True)
    if not isinstance(result, Mapping):
        raise ValueError("ZeroBounce response is malformed")
    returned = _text(result.get("address") or result.get("email")).casefold()
    if not returned or returned != email.casefold():
        raise ValueError("ZeroBounce returned a different email address")
    status = _norm(result.get("status"))
    unsafe_statuses = {
        "abuse",
        "disposable",
        "do not mail",
        "invalid",
        "role based",
        "spamtrap",
        "toxic",
    }
    if status == "unknown" or status in unsafe_statuses:
        return False
    if status not in {"catch all", "valid"}:
        raise ValueError("ZeroBounce status is malformed")
    sub_status = _norm(result.get("sub_status"))
    if sub_status and sub_status not in {"catch all", "catchall domain"}:
        return False
    for field in (
        "free_email",
        "freeEmail",
        "is_free_email",
        "isFreeEmail",
        "is_disposable",
        "isDisposable",
        "disposable",
        "is_toxic",
        "isToxic",
        "toxic",
        "is_spamtrap",
        "isSpamtrap",
        "is_abuse",
        "isAbuse",
        "role_based",
        "spamtrap",
        "abuse",
        "do_not_mail",
        "doNotMail",
        "is_do_not_mail",
        "isDoNotMail",
    ):
        flag = result.get(field)
        if flag is True or _norm(flag) in {"1", "true", "yes"}:
            return False
    return True


def _search_request(
    icp: Mapping[str, Any],
    company: Mapping[str, Any],
    *,
    role_query_hints: Sequence[str] = (),
    use_company_name: bool = False,
) -> dict[str, Any]:
    # Preserve the established target-role request exactly. Only suppress a
    # query hint when it repeats an existing requested role.
    roles = _bounded_strings(icp.get("target_roles"), limit=70)
    seen_roles = {_norm(role) for role in roles}
    for role in role_query_hints:
        normalized = _norm(role)
        if normalized and normalized not in seen_roles:
            seen_roles.add(normalized)
            roles.append(role)
    joined_roles = ",".join(roles)
    if role_query_hints and len(joined_roles) > 2_048:
        raise ValueError("role_query_hints exceed the contact search title limit")
    request: dict[str, Any] = {
        "currentJobTitles": joined_roles,
        "page": 1,
    }
    company_linkedin = _text(company.get("company_linkedin"))
    if _linkedin_company_slug(company_linkedin) and not use_company_name:
        request["currentCompanies"] = company_linkedin
    else:
        company_name = _text(company.get("company_name"))
        request["search"] = _company_name(company_name) or company_name
    geography = icp.get("contact_geography")
    geography = geography if isinstance(geography, Mapping) else {}
    countries = _bounded_strings(geography.get("countries"), limit=70)
    allow_bare_us_region = bool(countries) and all(
        _COUNTRY_ALIASES.get(_norm(country), _text(country).upper()) == "US"
        for country in countries
    )
    regions = [
        _harvestapi_region(region, allow_bare_us_region=allow_bare_us_region)
        for region in _bounded_strings(geography.get("regions"), limit=70)
    ]
    locations = (
        _bounded_strings(geography.get("cities"), limit=70)
        or list(dict.fromkeys(regions))
        or [_harvestapi_country(country) for country in countries]
    )
    if locations:
        request["locations"] = ",".join(locations)
    return request


def _fallback_search_request(
    icp: Mapping[str, Any],
    company: Mapping[str, Any],
    *,
    role_query_hints: Sequence[str] = (),
    use_company_name: bool = False,
) -> dict[str, Any] | None:
    request = _search_request(
        icp,
        company,
        role_query_hints=role_query_hints,
        use_company_name=use_company_name,
    )
    roles: list[str] = []
    seen: set[str] = set()
    for target in _bounded_strings(icp.get("target_roles"), limit=70):
        role = _functional_title(target)
        if role and role not in seen:
            seen.add(role)
            roles.append(role)
    for hint in role_query_hints:
        normalized = _norm(hint)
        if normalized and normalized not in seen:
            seen.add(normalized)
            roles.append(hint)
    fallback_titles = ",".join(roles)
    if not fallback_titles or fallback_titles.casefold() == str(
        request["currentJobTitles"]
    ).casefold():
        return None
    request["currentJobTitles"] = fallback_titles
    return request


def _successful_empty_search(value: Any) -> bool:
    unwrapped = _unwrap(value, require_success=True)
    if not isinstance(unwrapped, Mapping):
        return False
    elements = unwrapped.get("elements")
    status = unwrapped.get("status")
    return (
        (
            (isinstance(status, str) and status.strip().casefold() == "ok")
            or (type(status) is int and status == 200)
        )
        and isinstance(elements, Sequence)
        and not isinstance(elements, (str, bytes, bytearray))
        and not elements
    )


def _contact_from_profile(
    profile: Mapping[str, Any],
    *,
    company: Mapping[str, Any],
    icp: Mapping[str, Any],
    selected_observed_role: str | None = None,
) -> dict[str, Any] | None:
    name = _profile_name(profile)
    linkedin = _profile_linkedin(profile)
    record_id = _text(
        profile.get("recordId") or profile.get("record_id") or profile.get("id")
    )
    email = next(iter(_emails(profile)), "")
    if not all((name, linkedin, record_id, email)):
        return None

    targets = _bounded_strings(icp.get("target_roles"), limit=70)
    expected = _expected_company(company)
    position = next(
        (
            item
            for item in _current_positions(profile)
            if _company_matches(expected, _position_company(item))
            and (
                _position_title(item) == selected_observed_role
                and _role_seniority_matches(
                    _position_title(item), targets, icp.get("target_seniority")
                )
                if selected_observed_role is not None
                else _role_matches(
                    _position_title(item), targets, icp.get("target_seniority")
                )
            )
        ),
        None,
    )
    if position is None:
        return None

    location = _profile_location(profile)
    geography = icp.get("contact_geography")
    geography = geography if isinstance(geography, Mapping) else {}
    if not _location_matches(location, geography):
        return None
    claim_location: dict[str, str] = {"country": location["country"]}
    for key in ("region", "city"):
        if location[key]:
            claim_location[key] = location[key]
    return {
        "full_name": name,
        "role": _position_title(position),
        "linkedin_url": linkedin,
        "location": claim_location,
        "email": email,
        "email_source": {
            "provider": "harvestapi",
            "tool": "harvestapi_get_profile",
            "record_id": record_id,
        },
    }


def _search_contact_candidates(
    icp: Mapping[str, Any],
    company: Mapping[str, Any],
    call_provider: ProviderCall,
    *,
    role_query_hints: Sequence[str] = (),
    use_company_name: bool = False,
) -> list[Mapping[str, Any]]:
    search = call_provider(
        "harvestapi_search_leads",
        _search_request(
            icp,
            company,
            role_query_hints=role_query_hints,
            use_company_name=use_company_name,
        ),
    )
    candidates = _profiles(_unwrap(search))
    if not candidates and _successful_empty_search(search):
        fallback = _fallback_search_request(
            icp,
            company,
            role_query_hints=role_query_hints,
            use_company_name=use_company_name,
        )
        if fallback is not None:
            search = call_provider("harvestapi_search_leads", fallback)
            candidates = _profiles(_unwrap(search))
    return candidates


def _ranked_role_candidates(
    icp: Mapping[str, Any],
    company: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    expected = _expected_company(company)
    targets = _bounded_strings(icp.get("target_roles"), limit=70)
    seniority = icp.get("target_seniority")
    selected: list[Mapping[str, Any]] = []
    numeric_company_fallback: list[Mapping[str, Any]] = []
    exact_titles = {_normalized_title(target) for target in targets}

    def title_priority(candidate: Mapping[str, Any]) -> int:
        # Spend the existing bounded profile lookups on exact requested roles
        # before broader matches such as divisional product-strategy titles.
        return int(not any(
            _normalized_title(_position_title(position)) in exact_titles
            and (
                _search_company_matches(expected, _position_company(position))
                or _numeric_search_company_reference(_position_company(position))
            )
            for position in _current_positions(candidate)
        ))

    for candidate in candidates:
        positions = _current_positions(candidate)
        matching_positions = [
            position
            for position in positions
            if _search_company_matches(expected, _position_company(position))
            and _role_matches(_position_title(position), targets, seniority)
        ]
        if matching_positions:
            selected.append(candidate)
            continue
        if any(
            _numeric_search_company_reference(_position_company(position))
            and _role_matches(_position_title(position), targets, seniority)
            for position in positions
        ):
            numeric_company_fallback.append(candidate)
    selected.sort(key=title_priority)
    numeric_company_fallback.sort(key=title_priority)
    selected.extend(numeric_company_fallback)
    return selected


def _contact_from_candidates(
    icp: Mapping[str, Any],
    company: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    call_provider: ProviderCall,
    *,
    selected_observed_role: str | None = None,
    preverify_emails: bool = False,
) -> dict[str, Any] | None:
    for candidate in candidates[:_PROFILE_LIMIT_PER_COMPANY]:
        linkedin = _profile_linkedin(candidate)
        if not linkedin:
            continue
        profile_request = {"url": linkedin, "findEmail": "true"}
        if preverify_emails:
            profile_request["skipSmtp"] = "true"
        profile_response = call_provider("harvestapi_get_profile", profile_request)
        for profile in _profiles(_unwrap(profile_response)):
            contact = _contact_from_profile(
                profile,
                company=company,
                icp=icp,
                selected_observed_role=selected_observed_role,
            )
            if contact is not None:
                if not preverify_emails:
                    return contact
                for email in _emails(profile)[:_EMAIL_PREVERIFY_LIMIT]:
                    result = call_provider("zerobounce_validate", {"email": email})
                    if _zerobounce_accepts_email(result, email):
                        verified = deepcopy(contact)
                        verified["email"] = email
                        return verified
    return None


def _observed_role_candidates(
    icp: Mapping[str, Any],
    company: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    """Keep bounded provider-observed roles for an Arena semantic handoff."""

    expected = _expected_company(company)
    targets = _bounded_strings(icp.get("target_roles"), limit=70)
    seniority = icp.get("target_seniority")
    result: dict[str, list[Mapping[str, Any]]] = {}
    normalized_titles: dict[str, str] = {}
    for candidate in candidates:
        linkedin = _profile_linkedin(candidate)
        if not linkedin:
            continue
        for position in _current_positions(candidate):
            if not _search_company_matches(expected, _position_company(position)):
                continue
            title = _position_title(position)
            normalized = _norm(title)
            if (
                not title
                or len(title) > 200
                or not normalized
                or not _role_seniority_matches(title, targets, seniority)
            ):
                continue
            retained = normalized_titles.get(normalized)
            if retained is None:
                if len(result) >= _PROFILE_LIMIT_PER_COMPANY:
                    continue
                normalized_titles[normalized] = title
                retained = title
                result[retained] = []
            retained_urls = {
                _profile_linkedin(item) for item in result[retained]
            }
            if (
                linkedin not in retained_urls
                and len(result[retained]) < _PROFILE_LIMIT_PER_COMPANY
            ):
                result[retained].append(candidate)
    return result


def _find_contact(
    icp: Mapping[str, Any],
    company: Mapping[str, Any],
    call_provider: ProviderCall,
    *,
    preverify_emails: bool = False,
) -> dict[str, Any] | None:
    candidates = _search_contact_candidates(icp, company, call_provider)
    selected = _ranked_role_candidates(icp, company, candidates)
    return _contact_from_candidates(
        icp,
        company,
        selected,
        call_provider,
        preverify_emails=preverify_emails,
    )


def enrich_contacts(
    icp: Mapping[str, Any],
    companies: Sequence[Mapping[str, Any]],
    call_provider: ProviderCall,
) -> list[dict[str, Any]]:
    """Attach supported contacts while preserving every company row and order."""

    output = [deepcopy(dict(company)) for company in companies]
    for company in output:
        company.pop("contact", None)
    if _text(icp.get("contact_policy")) != CONTACT_POLICY:
        return output
    if not _bounded_strings(icp.get("target_roles"), limit=70):
        return output
    for company in output:
        try:
            contact = _find_contact(icp, company, call_provider)
        except (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError):
            contact = None
        if contact is not None:
            try:
                company["contact"] = ContactResult.model_validate(contact).model_dump(
                    mode="json", exclude_none=True
                )
            except (TypeError, ValueError):
                pass
    return output


class ContactLookup:
    """Reuse checked contacts and bound explicit transient-failure retries."""

    def __init__(
        self,
        icp: Mapping[str, Any],
        *,
        allow_role_selection: bool = False,
        preverify_emails: bool = False,
    ) -> None:
        self.icp = deepcopy(dict(icp))
        self.allow_role_selection = allow_role_selection
        self.preverify_emails = preverify_emails
        self._results: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        self._statuses: dict[tuple[str, str, str], str] = {}
        self._role_candidates: dict[
            tuple[str, str, str], dict[str, list[Mapping[str, Any]]]
        ] = {}
        self._selected_roles: dict[tuple[str, str, str], str] = {}
        self._query_hints: dict[tuple[str, str, str], tuple[str, ...]] = {}
        self._unavailable_retries: set[tuple[str, str, str]] = set()
        self._slug_lookup_failures: set[tuple[str, str, str]] = set()

    @staticmethod
    def _key(company: Mapping[str, Any]) -> tuple[str, str, str]:
        expected = _expected_company(company)
        return (expected["domain"], expected["linkedin_slug"], expected["name"])

    def find(
        self, company: Mapping[str, Any], call_provider: ProviderCall,
        *,
        retry_missing: bool = False,
        retry_unavailable: bool = False,
        selected_observed_role: str | None = None,
        role_query_hints: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        key = self._key(company)
        if role_query_hints is not None and not self.allow_role_selection:
            raise ValueError("role query hints are unavailable")
        frozen_hints = self._query_hints.get(key)
        supplied_hints = (
            _validated_role_query_hints(self.icp, role_query_hints)
            if frozen_hints is None or role_query_hints is not None
            else frozen_hints
        )
        if frozen_hints is not None and supplied_hints != frozen_hints:
            raise ValueError("role_query_hints are already fixed")
        if frozen_hints is None and supplied_hints:
            # Reject an oversized combined title query before a provider call
            # can consume either the semantic or provider budget.
            _search_request(
                self.icp, company, role_query_hints=supplied_hints
            )
        selected_role = _text(selected_observed_role)
        if selected_role:
            if not self.allow_role_selection:
                raise ValueError("observed role selection is unavailable")
            options = self._role_candidates.get(key, {})
            if selected_role not in options:
                raise ValueError("selected_observed_role must exactly match an offered role")
            prior_selection = self._selected_roles.get(key)
            if prior_selection is not None and prior_selection != selected_role:
                raise ValueError("the observed role selection is already fixed")
        if frozen_hints is None:
            self._query_hints[key] = supplied_hints
        if selected_role:
            self._selected_roles[key] = selected_role

        status = self._statuses.get(key)
        if status == "role_selection_required" and not selected_role:
            return None
        retry_after_unavailable = (
            retry_unavailable
            and status == "unavailable"
            and key not in self._unavailable_retries
        )
        if retry_after_unavailable:
            self._unavailable_retries.add(key)
        should_lookup = (
            key not in self._results
            or (retry_missing and self._results[key] is None)
            or retry_after_unavailable
        )
        if selected_role and status == "role_selection_required":
            should_lookup = True
        if should_lookup:
            unavailable = False
            slug_lookup_failed = False

            def checked_call(name: str, arguments: dict[str, Any]) -> Any:
                nonlocal slug_lookup_failed, unavailable
                try:
                    result = call_provider(name, arguments)
                except (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError):
                    unavailable = True
                    raise
                if name == "harvestapi_search_leads" and _company_slug_not_found(
                    result, arguments.get("currentCompanies")
                ):
                    slug_lookup_failed = True
                if _unwrap(result, require_success=True) is None:
                    unavailable = True
                    return {"ok": False}
                return result

            try:
                approved_role = self._selected_roles.get(key)
                if approved_role is not None:
                    contact = _contact_from_candidates(
                        self.icp,
                        company,
                        self._role_candidates[key][approved_role],
                        checked_call,
                        selected_observed_role=approved_role,
                        preverify_emails=self.preverify_emails,
                    )
                elif self.allow_role_selection:
                    candidates = _search_contact_candidates(
                        self.icp,
                        company,
                        checked_call,
                        role_query_hints=self._query_hints[key],
                        use_company_name=key in self._slug_lookup_failures,
                    )
                    selected = _ranked_role_candidates(
                        self.icp, company, candidates
                    )
                    if selected:
                        contact = _contact_from_candidates(
                            self.icp,
                            company,
                            selected,
                            checked_call,
                            preverify_emails=self.preverify_emails,
                        )
                    else:
                        options = _observed_role_candidates(
                            self.icp, company, candidates
                        )
                        if options and not unavailable:
                            self._role_candidates[key] = deepcopy(options)
                            self._results[key] = None
                            self._statuses[key] = "role_selection_required"
                            return None
                        contact = None
                else:
                    if self.preverify_emails:
                        raw_contact = _find_contact(
                            self.icp,
                            company,
                            checked_call,
                            preverify_emails=True,
                        )
                        contact = (
                            ContactResult.model_validate(raw_contact).model_dump(
                                mode="json", exclude_none=True
                            )
                            if raw_contact is not None
                            else None
                        )
                    else:
                        rows = enrich_contacts(self.icp, [company], checked_call)
                        contact = rows[0].get("contact")
            except (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError):
                unavailable = True
                contact = None
            self._results[key] = contact
            if slug_lookup_failed:
                self._slug_lookup_failures.add(key)
            if contact is not None:
                self._statuses[key] = "found"
            elif unavailable:
                self._statuses[key] = "unavailable"
            else:
                self._statuses[key] = "not_found"
        return deepcopy(self._results[key])

    def role_options(self, company: Mapping[str, Any]) -> list[str]:
        """Return only bounded observed titles; candidate identities remain private."""

        key = self._key(company)
        if self._statuses.get(key) != "role_selection_required":
            return []
        return list(self._role_candidates.get(key, {}))

    def status(self, company: Mapping[str, Any]) -> str | None:
        """Return the bounded outcome of the latest lookup for this identity."""

        return self._statuses.get(self._key(company))

    def enrich(
        self,
        companies: Sequence[Mapping[str, Any]],
        call_provider: ProviderCall | None,
    ) -> list[dict[str, Any]]:
        """Attach checked contacts; ``None`` is a cache-only path with no I/O."""

        rows = [deepcopy(dict(company)) for company in companies]
        finalized: set[tuple[str, str, str]] = set()
        for company in rows:
            company.pop("contact", None)
            key = self._key(company)
            if call_provider is None:
                contact = (
                    deepcopy(self._results.get(key))
                    if self._statuses.get(key) == "found"
                    else None
                )
            else:
                contact = self.find(
                    company,
                    call_provider,
                    retry_missing=(
                        key not in finalized
                        and self._statuses.get(key) != "role_selection_required"
                    ),
                )
            finalized.add(key)
            if contact is not None:
                company["contact"] = contact
        return rows


__all__ = ["CONTACT_POLICY", "ContactLookup", "enrich_contacts"]
