"""Small public input/output contract for the sourcing harness."""

from __future__ import annotations

import ipaddress
import re
from copy import deepcopy
from datetime import date as ISODate
from typing import Any, Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)


_EMPLOYEE_BANDS = (
    "0-1",
    "2-10",
    "11-50",
    "51-200",
    "201-500",
    "501-1,000",
    "1,001-5,000",
    "5,001-10,000",
    "10,001+",
)

_PUBLIC_LISTING_STAGE = re.compile(
    r"^(?:public|public company|publicly traded|listed company)\s*"
    r"\(\s*(?:asx|nasdaq|nyse|lse|tsx|tsxv|hkex|sgx|jpx|tse|krx|"
    r"euronext|six|jse|nse|bse)\s*:\s*[a-z0-9][a-z0-9.\-]{0,19}\s*\)$",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_LINKEDIN_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,119}$")
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/~-]{0,199}$")
_ISO2_CODES = frozenset(
    "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW".split()
)


def _canonical_employee_band(value: Any) -> Any:
    """Normalize formatting only; never move a company to a nearby bucket."""
    if not isinstance(value, str):
        return value
    text = (
        " ".join(value.strip().split())
        .lower()
        .replace("employees", "")
        .replace("employee", "")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace(",", "")
        .replace(" ", "")
    )
    canonical = {
        band.lower().replace(",", "").replace(" ", ""): band
        for band in _EMPLOYEE_BANDS
    }
    return canonical.get(text, value)


def _canonical_company_stage(value: Any) -> Any:
    """Normalize true stage synonyms to the scorer's public stage buckets."""
    if not isinstance(value, str):
        return value
    text = " ".join(value.strip().lower().split())
    compact = " ".join(
        text.replace("-", " ").replace("_", " ").replace("/", " ").split()
    )
    if compact == "seed":
        return "Seed"
    if compact == "bootstrapped":
        return "Bootstrapped"
    if compact in {"series a", "series a round"}:
        return "Series A"
    if compact in {"series b", "series b round"}:
        return "Series B"
    if compact in {
        "series c+",
        "series c or later",
        "series c and later",
        "series c plus",
        "series c",
        "series d",
        "series e",
        "series f",
        "series g",
        "series h",
    }:
        return "Series C+"
    if compact in {
        "private equity",
        "private equity backed",
        "private equity owned",
        "pe backed",
        "pe owned",
    }:
        return "Private Equity"
    if compact in {"public", "public company", "publicly traded", "listed company"}:
        return "Public"
    if _PUBLIC_LISTING_STAGE.fullmatch(text):
        return "Public"
    return value


def _public_http_url(value: str, *, allow_empty: bool = False) -> str:
    value = str(value or "").strip()
    if not value and allow_empty:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("must be an absolute HTTP(S) URL")
    try:
        host = parsed.hostname.rstrip(".").lower()
        ascii_host = host.encode("idna").decode("ascii")
        parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("must be a public URL") from exc
    if host == "localhost" or host.endswith(
        (".internal", ".invalid", ".local", ".localhost", ".onion", ".test")
    ):
        raise ValueError("must be a public URL")
    try:
        address = ipaddress.ip_address(ascii_host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("must be a public URL")
    if address is None:
        labels = ascii_host.split(".")
        if len(labels) < 2 or not any(character.isalpha() for character in labels[-1]):
            raise ValueError("must be a public URL")
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, "")
    )


class IntentSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    matched_icp_signal: int = Field(ge=0)
    description: str = Field(min_length=1, max_length=350)
    date: ISODate
    why_now: str = Field(min_length=1, max_length=600)
    url: str
    snippet: str = Field(min_length=1, max_length=600)

    @field_validator("snippet", mode="before")
    @classmethod
    def normalize_snippet_spacing(cls, value: Any) -> Any:
        """Remove invisible web-page word breaks from quoted evidence."""
        return value.replace("\u200b", "") if isinstance(value, str) else value

    @field_validator("why_now", mode="before")
    @classmethod
    def normalize_why_now_spacing(cls, value: Any) -> Any:
        """Keep the outreach explanation readable without changing its claims."""
        return " ".join(value.split()) if isinstance(value, str) else value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _public_http_url(value)


class RequiredAttributeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=2_000)
    passed: bool
    evidence_url: str
    evidence_quote: str = Field(min_length=1, max_length=2_000)
    explanation: str = Field(min_length=1, max_length=2_000)

    @field_validator("evidence_url")
    @classmethod
    def validate_evidence_url(cls, value: str) -> str:
        return _public_http_url(value)


class ContactLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    country: str = Field(min_length=2, max_length=2)
    region: Optional[str] = Field(default=None, min_length=1, max_length=120)
    city: Optional[str] = Field(default=None, min_length=1, max_length=120)

    @field_validator("country", mode="before")
    @classmethod
    def validate_country(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        code = value.strip().upper()
        if code not in _ISO2_CODES:
            raise ValueError("must be a recognized two-letter country code")
        return code


class ContactEmailSource(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: str = Field(pattern=r"^harvestapi$")
    tool: str = Field(pattern=r"^harvestapi_get_profile$")
    broker_call_id: Optional[str] = Field(default=None, min_length=1, max_length=200)
    record_id: Optional[str] = Field(default=None, min_length=1, max_length=200)

    @field_validator("broker_call_id", "record_id")
    @classmethod
    def validate_source_id(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and _SOURCE_ID_RE.fullmatch(value) is None:
            raise ValueError("source reference is invalid")
        return value

    @model_validator(mode="after")
    def validate_reference(self) -> "ContactEmailSource":
        if not self.broker_call_id and not self.record_id:
            raise ValueError("email_source requires broker_call_id or record_id")
        return self


class ContactResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    full_name: str = Field(min_length=1, max_length=160)
    role: str = Field(min_length=1, max_length=200)
    linkedin_url: str = Field(min_length=1, max_length=512)
    location: ContactLocation
    email: str = Field(min_length=3, max_length=254)
    email_source: ContactEmailSource

    @field_validator("linkedin_url")
    @classmethod
    def validate_linkedin_url(cls, value: str) -> str:
        normalized = _public_http_url(value)
        parsed = urlsplit(normalized)
        host = (parsed.hostname or "").lower()
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if (
            (host != "linkedin.com" and not host.endswith(".linkedin.com"))
            or len(parts) != 2
            or parts[0].lower() != "in"
            or _LINKEDIN_SLUG_RE.fullmatch(parts[1]) is None
        ):
            raise ValueError("must be a LinkedIn profile URL")
        return urlunsplit(
            (
                "https",
                "www.linkedin.com",
                f"/in/{quote(parts[1], safe='._~-')}/",
                "",
                "",
            )
        )

    @field_validator("email", mode="before")
    @classmethod
    def validate_email(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        email = value.strip().casefold()
        if _EMAIL_RE.fullmatch(email) is None:
            raise ValueError("must be an email address")
        local = email.rsplit("@", 1)[0]
        if (
            len(local) > 64
            or local.startswith(".")
            or local.endswith(".")
            or ".." in local
        ):
            raise ValueError("must be an email address")
        return email


class CompanyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    company_name: str = Field(min_length=1, max_length=200)
    company_website: str
    company_linkedin: str = ""
    industry: str
    employee_count: str = Field(
        description="Use a canonical LinkedIn bucket when the ICP requires one."
    )
    company_stage: str = Field(
        default="",
        description=(
            "Verified lifecycle stage; use Seed, Series A, Series B, Series C+, "
            "Private Equity, Public, or Bootstrapped when one of those ICP stages applies."
        ),
    )
    country: str
    state: str = ""
    fit_summary: str = Field(min_length=1, max_length=500)
    fit_evidence_urls: list[str]
    intent_signals: list[IntentSignal] = Field(min_length=1)
    required_attribute: Optional[RequiredAttributeEvidence] = None

    @field_validator("company_website")
    @classmethod
    def validate_website(cls, value: str) -> str:
        return _public_http_url(value)

    @field_validator("company_linkedin")
    @classmethod
    def validate_linkedin(cls, value: str) -> str:
        normalized = _public_http_url(value, allow_empty=True)
        if not normalized:
            return ""
        parsed = urlsplit(normalized)
        host = (parsed.hostname or "").lower()
        parts = [part for part in parsed.path.split("/") if part]
        if (
            host != "linkedin.com"
            and not host.endswith(".linkedin.com")
        ) or len(parts) != 2 or parts[0].lower() != "company":
            raise ValueError("must be a LinkedIn company URL")
        return normalized

    @field_validator("fit_evidence_urls")
    @classmethod
    def validate_fit_urls(cls, values: list[str]) -> list[str]:
        return [_public_http_url(value) for value in values]

    @field_validator("employee_count", mode="before")
    @classmethod
    def normalize_employee_band(cls, value: Any) -> Any:
        return _canonical_employee_band(value)

    @field_validator("company_stage", mode="before")
    @classmethod
    def normalize_company_stage(cls, value: Any) -> Any:
        return _canonical_company_stage(value)


class ContactCompanyResult(CompanyResult):
    contact: Optional[ContactResult] = None


class CompaniesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    companies: list[CompanyResult] = Field(default_factory=list)


def company_list_json_schema() -> dict[str, Any]:
    """Return the canonical company-array schema with local references inlined."""
    raw = TypeAdapter(list[CompanyResult]).json_schema()
    definitions = raw.get("$defs") if isinstance(raw.get("$defs"), dict) else {}

    def inline(value: Any) -> Any:
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            target = definitions.get(name)
            if not isinstance(target, dict):
                raise ValueError(f"unresolved schema reference: {reference}")
            merged = {
                **target,
                **{key: item for key, item in value.items() if key != "$ref"},
            }
            return inline(merged)
        return {key: inline(item) for key, item in value.items() if key != "$defs"}

    schema = inline(raw)
    if not isinstance(schema, dict):
        raise RuntimeError("canonical company schema is unavailable")
    return schema


def _employee_bands(value: Any) -> list[str]:
    if value is None:
        return []
    raw = value if isinstance(value, (list, tuple, set)) else str(value).split("|")
    return [str(item).strip() for item in raw if str(item).strip()]


def _intent_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("signal") or value.get("intent_signal") or value.get("text")
    return str(value or "").strip()


def _clean_intent_row(
    value: Any, *, fallback_category: Any = "", fallback_age: Any = None
) -> dict[str, Any] | None:
    row = value if isinstance(value, dict) else {"signal": value}
    signal = _intent_text(row)
    if not signal:
        return None
    category = str(
        row.get("category")
        or row.get("intent_category")
        or row.get("evidence_type")
        or fallback_category
        or ""
    ).strip().upper()
    age = row.get("max_age_days", row.get("intent_max_age_days", fallback_age))
    try:
        age = int(age) if age not in (None, "") else None
    except (TypeError, ValueError):
        age = None
    return {"signal": signal, "category": category, "max_age_days": age}


def _structured_intents(
    icp: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return explicit required rows and explicit bonus rows."""
    required = [
        row
        for value in (
            icp.get("required_intents")
            if isinstance(icp.get("required_intents"), list)
            else []
        )
        if (row := _clean_intent_row(value)) is not None
    ]
    bonuses = [
        row
        for value in (
            icp.get("bonus_intents")
            if isinstance(icp.get("bonus_intents"), list)
            else []
        )
        if (row := _clean_intent_row(value)) is not None
    ]
    return required, bonuses


def _intent_contract(icp: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve host signal indexes while separating required and bonus intent."""
    explicit_required, explicit_bonuses = _structured_intents(icp)
    required_by_text = {row["signal"]: row for row in explicit_required}
    bonus_by_text = {row["signal"]: row for row in explicit_bonuses}

    raw_signals = icp.get("intent_signals")
    if isinstance(raw_signals, (str, dict)):
        raw_signals = [raw_signals]
    ordered_values = list(raw_signals) if isinstance(raw_signals, list) else []
    if not ordered_values:
        ordered_values.extend(explicit_required)
        ordered_values.extend(explicit_bonuses)
    else:
        ordered_texts = {_intent_text(value) for value in ordered_values}
        for row in (*explicit_required, *explicit_bonuses):
            if row["signal"] not in ordered_texts:
                ordered_values.append(row)
                ordered_texts.add(row["signal"])
    if not ordered_values:
        primary = icp.get("intent_signal_text") or icp.get("intent_signal")
        if primary:
            ordered_values.append(primary)

    categories = icp.get("intent_signal_evidence_types")
    ages = icp.get("intent_signal_max_age_days")
    explicit_required_texts = set(required_by_text)
    contract: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, value in enumerate(ordered_values):
        signal = _intent_text(value)
        if not signal or signal in seen:
            continue
        seen.add(signal)
        metadata = required_by_text.get(signal) or bonus_by_text.get(signal) or {}
        aligned_category = (
            categories[index]
            if isinstance(categories, list) and index < len(categories)
            else ""
        )
        aligned_age = (
            ages[index] if isinstance(ages, list) and index < len(ages) else None
        )
        primary_category = (
            icp.get("intent_category") or icp.get("intent_source") or ""
            if index == 0
            else ""
        )
        primary_age = icp.get("intent_max_age_days") if index == 0 else None
        row = _clean_intent_row(
            value,
            fallback_category=(
                metadata.get("category") or aligned_category or primary_category
            ),
            fallback_age=(
                metadata.get("max_age_days")
                if metadata.get("max_age_days") is not None
                else aligned_age if aligned_age is not None else primary_age
            ),
        )
        if row is None:
            continue
        row["index"] = len(contract)
        row["required"] = not contract or signal in explicit_required_texts
        contract.append(row)
    return contract


def normalize_icp(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize only the inconsistent fields needed by the public runner."""
    if not isinstance(value, dict):
        raise TypeError("icp must be a dictionary")
    icp = deepcopy(value)
    icp["employee_count"] = _employee_bands(icp.get("employee_count"))
    contract = _intent_contract(icp)
    icp["intent_contract"] = contract
    icp["intent_signals"] = [row["signal"] for row in contract]
    icp["required_intents"] = [
        {key: row[key] for key in ("signal", "category", "max_age_days")}
        for row in contract
        if row["required"]
    ]
    icp["bonus_intents"] = [
        {key: row[key] for key in ("signal", "category", "max_age_days")}
        for row in contract
        if not row["required"]
    ]
    if contract:
        primary = contract[0]
        icp["intent_signal"] = primary["signal"]
        icp["intent_category"] = primary["category"]
        icp["intent_max_age_days"] = primary["max_age_days"]
    return icp


def validate_companies(
    value: Any,
    max_companies: int | None = None,
    *,
    allow_contacts: bool = False,
) -> list[dict[str, Any]]:
    """Parse ordinary JSON without repairing quality or ranking mistakes."""
    if isinstance(value, dict) and "companies" in value:
        value = value["companies"]
    if not isinstance(value, list):
        raise ValueError("runner output must be a list or {'companies': [...]} object")
    cap = max(0, int(max_companies if max_companies is not None else 5))
    if cap == 0:
        return []
    if len(value) > cap:
        raise ValueError(f"runner output cannot contain more than {cap} companies")
    results: list[dict[str, Any]] = []
    for raw in value:
        model = (
            ContactCompanyResult
            if allow_contacts and isinstance(raw, dict) and "contact" in raw
            else CompanyResult
        )
        company = model.model_validate(raw)
        serialized = company.model_dump(mode="json")
        contact = getattr(company, "contact", None)
        if contact is None:
            serialized.pop("contact", None)
        else:
            source = serialized["contact"]["email_source"]
            for key in ("broker_call_id", "record_id"):
                if source.get(key) is None:
                    source.pop(key, None)
            location = serialized["contact"]["location"]
            for key in ("region", "city"):
                if location.get(key) is None:
                    location.pop(key, None)
        results.append(serialized)
    return results
