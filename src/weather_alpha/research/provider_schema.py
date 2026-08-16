"""Strict HTTP-200 response-schema validation for Phase 3 providers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from weather_alpha.collectors.polymarket.parser import is_temperature_market_text

SchemaStatus = Literal["ok", "empty", "malformed", "http_failure", "source_drift"]
Phase3Eligibility = Literal["eligible", "ineligible", "not_applicable"]
PayloadSemanticClass = Literal[
    "schema_valid_eligible",
    "schema_valid_phase3_ineligible",
    "schema_error",
    "valid_empty",
    "http_network_failure",
]

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CELSIUS_UNITS = {"°c", "c", "celsius"}


class ProviderSchemaError(ValueError):
    """Raised when an HTTP-200 payload does not match the expected provider schema."""

    def __init__(self, message: str, *, status: SchemaStatus, provider: str) -> None:
        super().__init__(message)
        self.status = status
        self.provider = provider


@dataclass(frozen=True, slots=True)
class SchemaValidationResult:
    status: SchemaStatus
    provider: str
    detail: str | None = None
    phase3_eligibility: Phase3Eligibility = "not_applicable"

    def raise_for_status(self) -> None:
        if self.status in {"ok", "empty"}:
            return
        raise ProviderSchemaError(
            self.detail or f"{self.provider} schema status={self.status}",
            status=self.status,
            provider=self.provider,
        )

    @property
    def semantic_class(self) -> PayloadSemanticClass:
        return classify_payload_semantic_class(self)


def classify_payload_semantic_class(result: SchemaValidationResult) -> PayloadSemanticClass:
    """Map schema+eligibility into the exact Phase 3 semantic classes."""
    if result.status == "http_failure":
        return "http_network_failure"
    if result.status in {"malformed", "source_drift"}:
        return "schema_error"
    if result.status == "empty":
        return "valid_empty"
    if result.status == "ok" and result.phase3_eligibility == "ineligible":
        return "schema_valid_phase3_ineligible"
    if result.status == "ok":
        return "schema_valid_eligible"
    return "schema_error"


def classify_schema_counter(result: SchemaValidationResult) -> str:
    """Counter bucket: schema_error only for structural/type/domain consumed-field failures."""
    if result.status in {"malformed", "source_drift"}:
        return "schema_error"
    return result.status


def validate_prices_history_payload(payload: object) -> SchemaValidationResult:
    provider = "polymarket-clob-prices-history"
    if not isinstance(payload, dict):
        return _malformed(provider, "prices-history payload must be an object")
    if ("error" in payload or "message" in payload) and "history" not in payload:
        return SchemaValidationResult(
            status="source_drift",
            provider=provider,
            detail="prices-history error/message object is not a valid history container",
        )
    if "history" not in payload:
        return _malformed(provider, "prices-history missing required history key")
    history = payload.get("history")
    if not isinstance(history, list):
        return _malformed(provider, "prices-history history must be a list")
    if not history:
        return SchemaValidationResult(status="empty", provider=provider)
    for item in history:
        if not isinstance(item, dict):
            return _malformed(provider, "prices-history nested record must be an object")
        if "t" not in item or "p" not in item:
            return _malformed(provider, "prices-history nested record missing t or p")
        if not _valid_history_t(item.get("t")):
            return _malformed(provider, "prices-history nested record has invalid t")
        if not _valid_history_p(item.get("p")):
            return _malformed(provider, "prices-history nested record has invalid p")
    return SchemaValidationResult(status="ok", provider=provider)


def validate_gamma_search_payload(payload: object) -> SchemaValidationResult:
    provider = "polymarket-gamma-public-search"
    if not isinstance(payload, dict):
        return _malformed(provider, "gamma search payload must be an object")
    if "error" in payload:
        return SchemaValidationResult(
            status="source_drift",
            provider=provider,
            detail="gamma search error object is not a valid search container",
        )
    has_events = "events" in payload
    has_markets = "markets" in payload
    if not has_events and not has_markets:
        return SchemaValidationResult(
            status="source_drift",
            provider=provider,
            detail="gamma search missing events/markets containers",
        )
    events = payload.get("events") if has_events else []
    markets = payload.get("markets") if has_markets else []
    if has_events and not isinstance(events, list):
        return _malformed(provider, "gamma search events must be a list")
    if has_markets and not isinstance(markets, list):
        return _malformed(provider, "gamma search markets must be a list")
    assert isinstance(events, list)
    assert isinstance(markets, list)
    if not events and not markets:
        return SchemaValidationResult(
            status="empty",
            provider=provider,
            phase3_eligibility="not_applicable",
        )
    for event in events:
        if not isinstance(event, dict):
            return _malformed(provider, "gamma search event must be an object")
        event_id = event.get("id")
        if not isinstance(event_id, str | int) or (
            isinstance(event_id, str) and not event_id.strip()
        ):
            return _malformed(provider, "gamma search event missing nonempty parent id")
        nested = event.get("markets")
        if not isinstance(nested, list):
            return _malformed(provider, "gamma search event markets must be a list")
        for market in nested:
            err = _validate_gamma_market_record(market, provider=provider)
            if err is not None:
                return err
    for market in markets:
        err = _validate_gamma_market_record(market, provider=provider)
        if err is not None:
            return err
    eligibility = classify_gamma_phase3_eligibility(payload)
    return SchemaValidationResult(
        status="ok",
        provider=provider,
        phase3_eligibility=eligibility,
        detail=None
        if eligibility == "eligible"
        else "structurally valid gamma payload lacks Phase 3 weather eligibility fields",
    )


def classify_gamma_phase3_eligibility(payload: object) -> Phase3Eligibility:
    """Weather text/city/date/station/bucket/token coverage — not schema structure."""
    if not isinstance(payload, dict):
        return "ineligible"
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    markets = payload.get("markets") if isinstance(payload.get("markets"), list) else []
    assert isinstance(events, list)
    assert isinstance(markets, list)
    candidates: list[dict[str, Any]] = []
    for event in events:
        if isinstance(event, dict):
            nested = event.get("markets")
            if isinstance(nested, list):
                for market in nested:
                    if isinstance(market, dict):
                        candidates.append(market)
    for market in markets:
        if isinstance(market, dict):
            candidates.append(market)
    if not candidates:
        return "ineligible"
    for market in candidates:
        if _gamma_market_phase3_eligible(market):
            return "eligible"
    return "ineligible"


def _gamma_market_phase3_eligible(market: dict[str, Any]) -> bool:
    question = str(market.get("question") or "")
    slug = market.get("slug")
    slug_text = str(slug) if isinstance(slug, str) else None
    if not is_temperature_market_text(question, slug_text):
        return False
    # Phase 3 eligibility also needs parseable weather market coverage signals.
    has_bucket = bool(
        str(market.get("groupItemTitle") or market.get("group_item_title") or "").strip()
    )
    token_ids = market.get("clobTokenIds") or market.get("tokens") or market.get("clob_token_ids")
    has_token = False
    if (isinstance(token_ids, list) and token_ids) or (
        isinstance(token_ids, str) and token_ids.strip() not in {"", "[]", "null"}
    ):
        has_token = True
    # City/date/station evidence may live in question/slug/description; temperature
    # text gate above is required. Bucket label and YES token identity are also required
    # for Phase 3 market usability.
    return has_bucket and has_token


def validate_single_run_payload(payload: object) -> SchemaValidationResult:
    provider = "open-meteo-single-run"
    if not isinstance(payload, dict):
        return _malformed(provider, f"{provider} payload must be an object")
    if "error" in payload:
        return SchemaValidationResult(
            status="source_drift",
            provider=provider,
            detail=f"{provider} error object is not a valid forecast container",
        )
    tz_err = _validate_timezone_offset(payload, provider=provider)
    if tz_err is not None:
        return tz_err
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        return _malformed(provider, f"{provider} hourly must be an object")
    if "time" not in hourly or "temperature_2m" not in hourly:
        return _malformed(provider, f"{provider} hourly missing time/temperature_2m")
    times = hourly.get("time")
    temps = hourly.get("temperature_2m")
    if not isinstance(times, list) or not isinstance(temps, list):
        return _malformed(provider, f"{provider} hourly time/temperature_2m must be lists")
    if len(times) != len(temps):
        return _malformed(provider, f"{provider} hourly time/temperature_2m length mismatch")
    units = payload.get("hourly_units")
    unit = None
    if isinstance(units, dict):
        unit = units.get("temperature_2m")
    if unit is None or str(unit).strip().lower() not in _CELSIUS_UNITS:
        return _malformed(provider, f"{provider} temperature_2m unit must be Celsius")
    for stamp in times:
        if not _valid_local_timestamp(stamp):
            return _malformed(provider, f"{provider} hourly timestamp invalid")
    for value in temps:
        if not _valid_temp_or_null(value):
            return _malformed(provider, f"{provider} hourly temperature invalid")
    for key, series in hourly.items():
        if key in {"time", "temperature_2m"}:
            continue
        if series is None:
            continue
        if not isinstance(series, list) or len(series) != len(times):
            return _malformed(provider, f"{provider} ancillary series {key} misaligned")
        for value in series:
            if not _valid_temp_or_null(value):
                return _malformed(provider, f"{provider} ancillary series {key} invalid")
    if not times:
        return SchemaValidationResult(status="empty", provider=provider)
    return SchemaValidationResult(status="ok", provider=provider)


def validate_archive_payload(payload: object) -> SchemaValidationResult:
    provider = "open-meteo-archive"
    if not isinstance(payload, dict):
        return _malformed(provider, f"{provider} payload must be an object")
    if "error" in payload:
        return SchemaValidationResult(
            status="source_drift",
            provider=provider,
            detail=f"{provider} error object is not a valid archive container",
        )
    tz_err = _validate_timezone_offset(payload, provider=provider)
    if tz_err is not None:
        return tz_err
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        return _malformed(provider, f"{provider} daily must be an object")
    if "time" not in daily or "temperature_2m_max" not in daily:
        return _malformed(provider, f"{provider} daily missing time/temperature_2m_max")
    times = daily.get("time")
    temps = daily.get("temperature_2m_max")
    if not isinstance(times, list) or not isinstance(temps, list):
        return _malformed(provider, f"{provider} daily time/temperature_2m_max must be lists")
    if len(times) != len(temps):
        return _malformed(provider, f"{provider} daily time/temperature_2m_max length mismatch")
    units = payload.get("daily_units")
    unit = None
    if isinstance(units, dict):
        unit = units.get("temperature_2m_max")
    if unit is None or str(unit).strip().lower() not in _CELSIUS_UNITS:
        return _malformed(provider, f"{provider} temperature_2m_max unit must be Celsius")
    for stamp in times:
        if not _valid_iso_date(stamp):
            return _malformed(provider, f"{provider} daily date invalid")
    for value in temps:
        if not _valid_temp_or_null(value):
            return _malformed(provider, f"{provider} daily temperature invalid")
    if not times:
        return SchemaValidationResult(status="empty", provider=provider)
    return SchemaValidationResult(status="ok", provider=provider)


def _validate_gamma_market_record(
    market: object, *, provider: str
) -> SchemaValidationResult | None:
    if not isinstance(market, dict):
        return _malformed(provider, "gamma search market must be an object")
    identity = market.get("conditionId")
    if identity is None:
        identity = market.get("id")
    if not isinstance(identity, str | int) or (isinstance(identity, str) and not identity.strip()):
        return _malformed(provider, "gamma search market missing required identity")
    return None


def _validate_timezone_offset(
    payload: dict[str, Any], *, provider: str
) -> SchemaValidationResult | None:
    timezone = payload.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip():
        return _malformed(provider, f"{provider} timezone must be nonempty string")
    try:
        ZoneInfo(timezone)
    except Exception:
        return _malformed(provider, f"{provider} timezone invalid")
    offset = payload.get("utc_offset_seconds")
    if isinstance(offset, bool) or not isinstance(offset, int):
        return _malformed(provider, f"{provider} utc_offset_seconds must be int")
    return None


def _valid_history_t(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int | float):
        return math.isfinite(float(value))
    if isinstance(value, str):
        try:
            from weather_alpha.models.timeutil import parse_timestamp

            parse_timestamp(value)
            return True
        except ValueError:
            return False
    return False


def _valid_history_p(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int | float):
        number = float(value)
        return math.isfinite(number) and 0.0 <= number <= 1.0
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return False
        return math.isfinite(number) and 0.0 <= number <= 1.0
    return False


def _valid_temp_or_null(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return math.isfinite(float(value))
    if isinstance(value, str):
        try:
            return math.isfinite(float(value.strip()))
        except ValueError:
            return False
    return False


def _valid_local_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            datetime.strptime(text, fmt)
            return True
        except ValueError:
            continue
    return False


def _valid_iso_date(value: object) -> bool:
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _malformed(provider: str, detail: str) -> SchemaValidationResult:
    return SchemaValidationResult(status="malformed", provider=provider, detail=detail)
