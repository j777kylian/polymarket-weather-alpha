"""Conservative parsers for Polymarket temperature-market metadata.

Unresolved questions are retained. City, station, date, and temperature
buckets are filled only when the source text supports them; nothing is
invented from nearby airports or climatology.

Event dates are parsed as complete calendar dates (month/day/year together)
independently per source. Never borrow a year from one source onto month/day
from another.

Priority: (a) explicit structured Gamma fields ``eventDate`` / ``event_date`` /
``date`` when they are a full ISO calendar date ``YYYY-MM-DD`` (not a datetime).
``startDate`` / ``endDate`` / close times are never used as the event day.
Structured provenance uses those exact field names. Conflicting structured
fields leave event_date unresolved; matching structured fields may resolve to
that date, subject to question/slug/description conflicts.
(b) question and slug, only when they are internally consistent; a complete
date from either may stand if the other source's month/day (when present)
agrees. (c) description never establishes event_date by itself. A description
date may only validate or conflict with an event date already supported by a
structured field, question, or slug. If description is the only date evidence,
event_date stays unresolved and parse_notes keep the normalized description
date with its source label. Description does not lend its year onto other
month/day evidence unless its complete date independently agrees with that
prior evidence. Any conflict leaves event_date unresolved. parse_notes record
each source label and its normalized date evidence. The original payload
remains on ParsedGammaMarket.raw.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from weather_alpha.models.records import MarketOutcome, NormalizedMarket, Provenance
from weather_alpha.models.timeutil import parse_timestamp, utc_now
from weather_alpha.models.units import fahrenheit_to_celsius

TARGET_CITIES: frozenset[str] = frozenset(
    {"paris", "london", "munich", "amsterdam", "new york", "milan"}
)


def validate_cities(cities: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not cities:
        raise ValueError("at least one city is required")
    normalized: list[str] = []
    for city in cities:
        key = city.strip().lower()
        if key not in TARGET_CITIES:
            allowed = ", ".join(sorted(TARGET_CITIES))
            raise ValueError(f"unsupported city {city!r}; allowed: {allowed}")
        normalized.append(key)
    return tuple(normalized)


_CITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (city, re.compile(rf"\b{re.escape(city)}\b", re.IGNORECASE))
    for city in sorted(TARGET_CITIES, key=len, reverse=True)
)

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_TEMP_MARKET_RE = re.compile(
    r"(?:will\s+the\s+)?highest\s+temperature\s+in\s+"
    r"(?P<city>new\s+york|paris|london|munich|amsterdam|milan)"
    r"(?:\s+city)?"
    r"(?:\s+be\s+[^?]+?)?"
    r"\s+on\s+(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:[a-z]{2})?"
    r"(?:,?\s*(?P<year>\d{4}))?",
    re.IGNORECASE,
)

_SLUG_DATE_RE = re.compile(
    r"(?:will-the-)?highest-temperature-in-(?P<city>new-york|paris|london|munich|amsterdam|milan)"
    r"(?:-city)?"
    r"(?:-be-[^-]+?)?"
    r"-on-(?P<month>[a-z]+)-(?P<day>\d{1,2})(?:-(?P<year>\d{4}))?",
    re.IGNORECASE,
)

# ICAO as a URL path segment, then URL separator, punctuation/whitespace, or end.
# Does not match /LONDON (continuation letters) or arbitrary English tokens.
_ICAO_URL_RE = re.compile(r"/([A-Z]{4})(?=[/?#]|[^\w]|$)", re.IGNORECASE)
_ICAO_PAREN_RE = re.compile(r"\(([A-Z]{4})\)")
_SHORT_YEAR_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+'?(\d{2})\b")
_MONTH_NAME = (
    r"january|february|march|april|may|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_DESC_MDY_RE = re.compile(
    rf"\b(?P<month>{_MONTH_NAME})\s+(?P<day>\d{{1,2}})(?:[a-z]{{2}})?,?\s+(?P<year>20\d{{2}})\b",
    re.IGNORECASE,
)
_ISO_CALENDAR_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_STRUCTURED_DATE_KEYS = ("eventDate", "event_date", "date")

_SIGNED_NUM = r"[+-]?\d+(?:\.\d+)?"
_EXACT_TEMP_RE = re.compile(
    rf"^\s*({_SIGNED_NUM})\s*°?\s*(?P<unit>C|F|Celsius|Fahrenheit)?\s*$",
    re.IGNORECASE,
)
_BELOW_RE = re.compile(
    rf"^\s*({_SIGNED_NUM})\s*°?\s*(?P<unit>C|F|Celsius|Fahrenheit)?\s+"
    rf"or\s+(below|lower|less)\s*$",
    re.IGNORECASE,
)
_ABOVE_RE = re.compile(
    rf"^\s*({_SIGNED_NUM})\s*°?\s*(?P<unit>C|F|Celsius|Fahrenheit)?\s+"
    rf"or\s+(higher|above|more)\s*$",
    re.IGNORECASE,
)
_RANGE_RE = re.compile(
    rf"^\s*({_SIGNED_NUM})\s*[\-\u2013]\s*({_SIGNED_NUM})\s*°?\s*"
    rf"(?P<unit>C|F|Celsius|Fahrenheit)?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TemperatureBucket:
    kind: str
    min_c: float | None
    max_c: float | None
    label: str
    unit: str
    min_native: float | None
    max_native: float | None


@dataclass(frozen=True, slots=True)
class ParsedGammaMarket:
    market: NormalizedMarket
    outcomes: tuple[MarketOutcome, ...]
    raw: dict[str, Any]


def parse_temperature_bucket(label: str | None) -> TemperatureBucket | None:
    if label is None:
        return None
    text = label.strip()
    if not text:
        return None
    match = _RANGE_RE.match(text)
    if match:
        unit = _bucket_unit(match.group("unit"), text)
        if unit is None:
            return None
        low_native = float(match.group(1))
        high_native = float(match.group(2))
        low = _to_celsius(low_native, unit)
        high = _to_celsius(high_native, unit)
        if low <= high:
            return TemperatureBucket(
                "range",
                low,
                high,
                text,
                unit,
                min(low_native, high_native),
                max(low_native, high_native),
            )
        return TemperatureBucket(
            "range",
            high,
            low,
            text,
            unit,
            min(low_native, high_native),
            max(low_native, high_native),
        )
    match = _BELOW_RE.match(text)
    if match:
        unit = _bucket_unit(match.group("unit"), text)
        if unit is None:
            return None
        native = float(match.group(1))
        return TemperatureBucket("below", None, _to_celsius(native, unit), text, unit, None, native)
    match = _ABOVE_RE.match(text)
    if match:
        unit = _bucket_unit(match.group("unit"), text)
        if unit is None:
            return None
        native = float(match.group(1))
        return TemperatureBucket("above", _to_celsius(native, unit), None, text, unit, native, None)
    match = _EXACT_TEMP_RE.match(text)
    if match:
        unit = _bucket_unit(match.group("unit"), text)
        if unit is None:
            return None
        native = float(match.group(1))
        value = _to_celsius(native, unit)
        return TemperatureBucket("exact", value, value, text, unit, native, native)
    return None


def _bucket_unit(explicit: str | None, label: str) -> str | None:
    if explicit:
        token = explicit.strip().upper()
        if token.startswith("F"):
            return "F"
        if token.startswith("C"):
            return "C"
    upper = label.upper()
    if "°F" in upper or re.search(r"\bF\b", upper):
        return "F"
    if "°C" in upper or re.search(r"\bC\b", upper):
        return "C"
    # Bare numeric labels without a unit are treated as Celsius (European markets).
    if re.fullmatch(rf"\s*{_SIGNED_NUM}\s*", label):
        return "C"
    if re.fullmatch(
        rf"\s*{_SIGNED_NUM}\s*[\-\u2013]\s*{_SIGNED_NUM}\s*",
        label,
    ):
        return "C"
    return None


def _to_celsius(value: float, unit: str) -> float:
    if unit == "F":
        return fahrenheit_to_celsius(value)
    return float(value)


def is_temperature_market_text(question: str, slug: str | None = None) -> bool:
    blob = f"{question} {slug or ''}".lower()
    return "temperature" in blob and any(city in blob for city in TARGET_CITIES)


def parse_gamma_market(
    payload: dict[str, Any],
    *,
    retrieved_url: str,
    retrieved_at: Any = None,
    raw_path: str | None = None,
    content_sha256: str | None = None,
) -> ParsedGammaMarket:
    question = str(payload.get("question") or "").strip()
    description = _optional_str(payload.get("description"))
    slug = _optional_str(payload.get("slug"))
    condition_id = str(payload.get("conditionId") or payload.get("condition_id") or "")
    notes: list[str] = []
    city = _extract_city(question, slug, description)
    event_date = _extract_event_date(
        payload, question=question, slug=slug, description=description, notes=notes
    )
    station_icao = _extract_station_icao(description, notes)
    parse_status = _parse_status(city, event_date, question)
    if city is None:
        notes.append("city not identified among configured research cities")
    if event_date is None:
        notes.append("event date not parsed; question retained")
    if station_icao is None:
        notes.append("station ICAO not found in source text; not inferred")

    provenance = Provenance(
        source="gamma-api.polymarket.com",
        retrieved_at=retrieved_at or utc_now(),
        request_url=retrieved_url,
        raw_path=raw_path,
        content_sha256=content_sha256,
        limitations=("gamma metadata only; not a historical order book",),
    )
    market = NormalizedMarket(
        condition_id=condition_id or f"unknown:{payload.get('id')}",
        question=question or "(missing question)",
        parse_status=parse_status,
        provenance=provenance,
        market_id=_optional_str(payload.get("id")),
        event_id=_event_id(payload),
        slug=slug,
        event_slug=_optional_str(payload.get("event_slug") or payload.get("eventSlug")),
        neg_risk_market_id=_optional_str(
            payload.get("negRiskMarketID") or payload.get("neg_risk_market_id")
        ),
        description=description,
        city=city,
        station_icao=station_icao,
        event_date=event_date,
        parse_notes=tuple(notes),
        closed=_optional_bool(payload.get("closed")),
        active=_optional_bool(payload.get("active")),
        start_time=_optional_ts(payload.get("startDate") or payload.get("start_date")),
        end_time=_optional_ts(payload.get("endDate") or payload.get("end_date")),
    )
    outcomes = _parse_outcomes(payload, market, provenance)
    return ParsedGammaMarket(market=market, outcomes=outcomes, raw=payload)


def _parse_outcomes(
    payload: dict[str, Any],
    market: NormalizedMarket,
    provenance: Provenance,
) -> tuple[MarketOutcome, ...]:
    labels = _json_list(payload.get("outcomes"))
    token_ids = _json_list(payload.get("clobTokenIds") or payload.get("clob_token_ids"))
    bucket = parse_temperature_bucket(_optional_str(payload.get("groupItemTitle")))
    if not labels and not token_ids:
        return ()
    count = max(len(labels), len(token_ids))
    results: list[MarketOutcome] = []
    for index in range(count):
        label = labels[index] if index < len(labels) else f"outcome-{index}"
        token_id = token_ids[index] if index < len(token_ids) else f"{market.condition_id}:{index}"
        apply_bucket = bucket is not None and str(label).lower() == "yes"
        min_c = bucket.min_c if bucket is not None and apply_bucket else None
        max_c = bucket.max_c if bucket is not None and apply_bucket else None
        kind = bucket.kind if bucket is not None and apply_bucket else None
        unit = bucket.unit if bucket is not None and apply_bucket else None
        native_min = bucket.min_native if bucket is not None and apply_bucket else None
        native_max = bucket.max_native if bucket is not None and apply_bucket else None
        results.append(
            MarketOutcome(
                condition_id=market.condition_id,
                token_id=str(token_id),
                outcome_label=str(label),
                provenance=provenance,
                outcome_index=index,
                temperature_celsius_min=min_c,
                temperature_celsius_max=max_c,
                bucket_kind=kind,
                group_item_title=_optional_str(payload.get("groupItemTitle")),
                temperature_unit=unit,
                temperature_native_min=native_min,
                temperature_native_max=native_max,
            )
        )
    return tuple(results)


def _extract_city(question: str, slug: str | None, description: str | None) -> str | None:
    blob = " ".join(part for part in (question, slug or "", description or "") if part)
    for city, pattern in _CITY_PATTERNS:
        if pattern.search(blob.replace("-", " ")):
            return city
    return None


def _extract_event_date(
    payload: dict[str, Any],
    *,
    question: str,
    slug: str | None,
    description: str | None,
    notes: list[str],
) -> str | None:
    structured_pairs = _structured_event_dates(payload)
    question_parts = _date_parts_from_match(_TEMP_MARKET_RE.search(question))
    slug_parts = _date_parts_from_match(_SLUG_DATE_RE.search(slug) if slug else None)
    description_dates = _description_complete_dates(description)

    evidence: list[tuple[str, str]] = []
    for key, iso in structured_pairs:
        evidence.append((key, iso))
    question_label = _source_date_label(question_parts)
    if question_label is not None:
        evidence.append(("question", question_label))
    slug_label = _source_date_label(slug_parts)
    if slug_label is not None:
        evidence.append(("slug", slug_label))

    supporting_complete = [
        iso
        for iso in (
            *[iso for _, iso in structured_pairs],
            _complete_iso(question_parts),
            _complete_iso(slug_parts),
        )
        if iso is not None
    ]
    unique_supporting = list(dict.fromkeys(supporting_complete))
    has_prior_evidence = bool(evidence)

    if _supporting_sources_conflict(question_parts, slug_parts, unique_supporting):
        for iso in description_dates:
            evidence.append(("description", iso))
        notes.append(_conflict_note(evidence))
        return None

    unique_description = list(dict.fromkeys(description_dates))
    if unique_description:
        if not has_prior_evidence:
            notes.append(
                "description date evidence not sufficient to establish event_date: "
                + "; ".join(f"description={iso}" for iso in unique_description)
            )
            return None
        for iso in unique_description:
            evidence.append(("description", iso))
        desc_iso = unique_description[0]
        description_agrees = len(unique_description) == 1 and (
            (not unique_supporting or unique_supporting[0] == desc_iso)
            and _parts_agree_with_iso(question_parts, desc_iso)
            and _parts_agree_with_iso(slug_parts, desc_iso)
        )
        if not description_agrees:
            notes.append(_conflict_note(evidence))
            return None
        return desc_iso

    if len(unique_supporting) == 1:
        return unique_supporting[0]
    if _month_day(question_parts) is not None or _month_day(slug_parts) is not None:
        notes.append("year missing; date not synthesized")
    return None


@dataclass(frozen=True, slots=True)
class _DateParts:
    year: int | None = None
    month: int | None = None
    day: int | None = None


def _date_parts_from_match(match: re.Match[str] | None) -> _DateParts:
    if match is None:
        return _DateParts()
    month_token = match.group("month")
    month = _MONTHS.get(month_token.lower()) if month_token else None
    day_token = match.group("day")
    year_token = match.group("year")
    return _DateParts(
        year=int(year_token) if year_token else None,
        month=month,
        day=int(day_token) if day_token else None,
    )


def _month_day(parts: _DateParts) -> tuple[int, int] | None:
    if parts.month is None or parts.day is None:
        return None
    return parts.month, parts.day


def _complete_iso(parts: _DateParts) -> str | None:
    if parts.year is None or parts.month is None or parts.day is None:
        return None
    return _iso_calendar_date(parts.year, parts.month, parts.day)


def _source_date_label(parts: _DateParts) -> str | None:
    complete = _complete_iso(parts)
    if complete is not None:
        return complete
    month_day = _month_day(parts)
    if month_day is None:
        return None
    return f"{month_day[0]:02d}-{month_day[1]:02d}"


def _iso_calendar_date(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _parse_iso_calendar_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    match = _ISO_CALENDAR_DATE_RE.fullmatch(text)
    if match is None:
        return None
    return _iso_calendar_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _structured_event_dates(payload: dict[str, Any]) -> list[tuple[str, str]]:
    # Only explicit event calendar fields. Never startDate/endDate/close timestamps.
    found: list[tuple[str, str]] = []
    for key in _STRUCTURED_DATE_KEYS:
        if key not in payload:
            continue
        parsed = _parse_iso_calendar_date(payload.get(key))
        if parsed is not None:
            found.append((key, parsed))
    return found


def _description_complete_dates(description: str | None) -> list[str]:
    if not description:
        return []
    found: list[str] = []
    for match in _DESC_MDY_RE.finditer(description):
        month = _MONTHS.get(match.group("month").lower())
        if month is None:
            continue
        iso = _iso_calendar_date(int(match.group("year")), month, int(match.group("day")))
        if iso is not None:
            found.append(iso)
    for match in _SHORT_YEAR_RE.finditer(description):
        month = _MONTHS.get(match.group(2).lower())
        if month is None:
            continue
        iso = _iso_calendar_date(2000 + int(match.group(3)), month, int(match.group(1)))
        if iso is not None:
            found.append(iso)
    return list(dict.fromkeys(found))


def _parts_agree_with_iso(parts: _DateParts, iso: str) -> bool:
    year = int(iso[0:4])
    month = int(iso[5:7])
    day = int(iso[8:10])
    return (
        (parts.month is None or parts.month == month)
        and (parts.day is None or parts.day == day)
        and (parts.year is None or parts.year == year)
    )


def _supporting_sources_conflict(
    question_parts: _DateParts,
    slug_parts: _DateParts,
    unique_supporting: list[str],
) -> bool:
    question_md = _month_day(question_parts)
    slug_md = _month_day(slug_parts)
    if question_md is not None and slug_md is not None and question_md != slug_md:
        return True
    if (
        question_parts.year is not None
        and slug_parts.year is not None
        and question_parts.year != slug_parts.year
    ):
        return True
    if len(unique_supporting) > 1:
        return True
    if len(unique_supporting) == 1:
        candidate = unique_supporting[0]
        return not (
            _parts_agree_with_iso(question_parts, candidate)
            and _parts_agree_with_iso(slug_parts, candidate)
        )
    return False


def _conflict_note(evidence: list[tuple[str, str]]) -> str:
    detail = "; ".join(f"{source}={value}" for source, value in evidence)
    return f"conflicting event dates: {detail}; date left unresolved"


def _extract_station_icao(description: str | None, notes: list[str]) -> str | None:
    if not description:
        return None
    url_match = _ICAO_URL_RE.search(description)
    if url_match:
        return url_match.group(1).upper()
    # Conservative fallback: parenthetical ICAO only. Do not accept bare English tokens
    # such as CITY/THIS/WILL even when airport/station wording is present.
    paren = [match.group(1).upper() for match in _ICAO_PAREN_RE.finditer(description.upper())]
    unique_paren = list(dict.fromkeys(paren))
    if len(unique_paren) == 1:
        return unique_paren[0]
    if len(unique_paren) > 1:
        notes.append("multiple ICAO-like tokens; station left unresolved")
    return None


def _parse_status(city: str | None, event_date: str | None, question: str) -> str:
    if city and event_date and "temperature" in question.lower():
        return "resolved"
    if city or event_date or "temperature" in question.lower():
        return "partial"
    return "unresolved"


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return None


def _optional_ts(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


def _event_id(payload: dict[str, Any]) -> str | None:
    events = payload.get("events")
    if isinstance(events, list) and events:
        first = events[0]
        if isinstance(first, dict) and first.get("id") is not None:
            return str(first["id"])
    if payload.get("eventId") is not None:
        return str(payload["eventId"])
    return None
