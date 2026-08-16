"""Immutable Phase 3 research snapshot types.

UTC-aware timestamps only. CLOB prices-history `p` is descriptive
market_probability; executable book fields stay null unless genuinely sourced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from weather_alpha.models.timeutil import ensure_utc, parse_timestamp

# Shared quarantine code for fail-closed canonical identity (never city/station/date alone).
EVENT_IDENTITY_AMBIGUOUS = "event_identity_ambiguous"


def _opt_utc(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


@dataclass(frozen=True, slots=True)
class HourlyForecastState:
    """Per-hour forecast valid at decision time for the event local day."""

    valid_time_utc: datetime
    temperature_c: float | None
    dew_point_c: float | None = None
    humidity_pct: float | None = None
    cloud_cover_pct: float | None = None
    wind_speed: float | None = None
    wind_direction_deg: float | None = None
    precipitation: float | None = None
    surface_pressure: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "valid_time_utc", ensure_utc(self.valid_time_utc))

    def to_json_obj(self) -> dict[str, Any]:
        return {
            "cloud_cover_pct": self.cloud_cover_pct,
            "dew_point_c": self.dew_point_c,
            "humidity_pct": self.humidity_pct,
            "precipitation": self.precipitation,
            "surface_pressure": self.surface_pressure,
            "temperature_c": self.temperature_c,
            "valid_time_utc": self.valid_time_utc.isoformat(),
            "wind_direction_deg": self.wind_direction_deg,
            "wind_speed": self.wind_speed,
        }

    def to_json_tuple(
        self,
    ) -> tuple[
        str,
        float | None,
        float | None,
        float | None,
        float | None,
        float | None,
        float | None,
        float | None,
        float | None,
    ]:
        return (
            self.valid_time_utc.isoformat(),
            self.temperature_c,
            self.dew_point_c,
            self.humidity_pct,
            self.cloud_cover_pct,
            self.wind_speed,
            self.wind_direction_deg,
            self.precipitation,
            self.surface_pressure,
        )

    @classmethod
    def from_json_obj(cls, value: Any) -> HourlyForecastState:
        if isinstance(value, dict):
            return cls(
                valid_time_utc=parse_timestamp(value["valid_time_utc"]),
                temperature_c=_opt_float(value.get("temperature_c")),
                dew_point_c=_opt_float(value.get("dew_point_c")),
                humidity_pct=_opt_float(value.get("humidity_pct")),
                cloud_cover_pct=_opt_float(value.get("cloud_cover_pct")),
                wind_speed=_opt_float(value.get("wind_speed")),
                wind_direction_deg=_opt_float(value.get("wind_direction_deg")),
                precipitation=_opt_float(value.get("precipitation")),
                surface_pressure=_opt_float(value.get("surface_pressure")),
            )
        if isinstance(value, list | tuple) and len(value) >= 2:
            padded = tuple(value) + (None,) * (9 - len(value))
            return cls(
                valid_time_utc=parse_timestamp(padded[0]),
                temperature_c=_opt_float(padded[1]),
                dew_point_c=_opt_float(padded[2]),
                humidity_pct=_opt_float(padded[3]),
                cloud_cover_pct=_opt_float(padded[4]),
                wind_speed=_opt_float(padded[5]),
                wind_direction_deg=_opt_float(padded[6]),
                precipitation=_opt_float(padded[7]),
                surface_pressure=_opt_float(padded[8]),
            )
        raise TypeError(f"unsupported hourly forecast encoding: {type(value)!r}")


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


@dataclass(frozen=True, slots=True)
class ResearchSnapshot:
    condition_id: str
    market_id: str | None
    token_id: str
    city: str | None
    station_icao: str | None
    event_date: str
    bucket_label: str | None
    bucket_kind: str | None
    temperature_celsius_min: float | None
    temperature_celsius_max: float | None
    decision_ts: datetime
    market_probability: float | None
    executable_entry_price: float | None
    best_bid: float | None
    best_ask: float | None
    midpoint: float | None
    spread: float | None
    volume: float | None
    liquidity: float | None
    weather_issued_at: datetime | None
    weather_available_at: datetime | None
    forecast_daily_max_c: float | None
    observation_max_so_far_c: float | None
    observation_as_of: datetime | None
    settlement_label: str | None
    diagnostic_actual_max_c: float | None
    provenance_urls: tuple[str, ...]
    raw_paths: tuple[str, ...]
    content_hashes: tuple[str, ...]
    limitations: tuple[str, ...]
    event_id: str | None = None
    question: str | None = None
    group_item_title: str | None = None
    slug: str | None = None
    event_slug: str | None = None
    neg_risk_market_id: str | None = None
    temperature_unit: str | None = None
    temperature_native_min: float | None = None
    temperature_native_max: float | None = None
    weather_valid_times: tuple[datetime, ...] = ()
    dew_point_c: float | None = None
    humidity_pct: float | None = None
    cloud_cover_pct: float | None = None
    wind_speed: float | None = None
    wind_direction_deg: float | None = None
    precipitation: float | None = None
    surface_pressure: float | None = None
    forecast_lead_hours: float | None = None
    source_station_icao: str | None = None
    market_price_observed_at: datetime | None = None
    price_request_url: str | None = None
    price_raw_path: str | None = None
    price_content_sha256: str | None = None
    forecast_hourly: tuple[HourlyForecastState, ...] = ()
    canonical_event_key: tuple[str, ...] | None = None
    canonical_event_source: str | None = None
    canonical_event_evidence: tuple[str, ...] = ()
    canonical_event_ambiguous: bool = False
    canonical_event_quarantine_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_ts", ensure_utc(self.decision_ts))
        object.__setattr__(self, "weather_issued_at", _opt_utc(self.weather_issued_at))
        object.__setattr__(self, "weather_available_at", _opt_utc(self.weather_available_at))
        object.__setattr__(self, "observation_as_of", _opt_utc(self.observation_as_of))
        object.__setattr__(
            self, "market_price_observed_at", _opt_utc(self.market_price_observed_at)
        )
        object.__setattr__(
            self,
            "weather_valid_times",
            tuple(ensure_utc(ts) for ts in self.weather_valid_times),
        )
        hourly = tuple(
            hour
            if isinstance(hour, HourlyForecastState)
            else HourlyForecastState.from_json_obj(hour)
            for hour in self.forecast_hourly
        )
        object.__setattr__(self, "forecast_hourly", hourly)
        price_at = self.market_price_observed_at
        if price_at is not None and price_at > self.decision_ts:
            raise ValueError("market_price_observed_at must be at or before decision_ts")


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    reason: str
    condition_id: str | None = None
    market_id: str | None = None
    token_id: str | None = None
    city: str | None = None
    station_icao: str | None = None
    event_date: str | None = None
    details: str | None = None


def snapshot_dedup_key(snapshot: ResearchSnapshot) -> str:
    return "|".join(
        (
            snapshot.condition_id,
            snapshot.token_id,
            snapshot.event_date,
            snapshot.decision_ts.isoformat(),
        )
    )


@dataclass(frozen=True, slots=True)
class CanonicalEventIdentity:
    canonical_event_key: tuple[str, ...]
    source: str
    evidence_fields: tuple[str, ...]
    ambiguous: bool = False
    quarantine_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ambiguous": self.ambiguous,
            "canonical_event_key": list(self.canonical_event_key),
            "evidence_fields": list(self.evidence_fields),
            "quarantine_reason": self.quarantine_reason,
            "source": self.source,
        }


_SLUG_TEMP_SUFFIX_RE = re.compile(
    r"-(?:\d+(?:-\d+)?[cf]|or-below|or-higher|or-lower|or-above)$",
    re.IGNORECASE,
)
_QUESTION_FAMILY_RE = re.compile(
    r"(?P<family>highest\s+temperature\s+in\s+.+?\s+on\s+[A-Za-z]+\s+\d{1,2}(?:,\s*\d{4})?)",
    re.IGNORECASE,
)


def normalize_slug_family(slug: str | None) -> str | None:
    if not slug:
        return None
    text = slug.strip().lower()
    if not text:
        return None
    # Strip trailing bucket suffixes from market slugs when present.
    trimmed = _SLUG_TEMP_SUFFIX_RE.sub("", text)
    # Also strip will-the- / be-<bucket> segments for Will-form market slugs.
    trimmed = re.sub(r"^will-the-", "", trimmed)
    trimmed = re.sub(r"-be-[^-]+(?=-on-)", "", trimmed)
    return trimmed or None


def question_family(question: str | None) -> str | None:
    if not question:
        return None
    # Remove bucket-specific fragments before matching the shared family phrase.
    cleaned = re.sub(
        r"\bbe\s+(?:between\s+)?[^?]+\s+on\s+",
        "on ",
        question,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\bbe\s+[^?]+\s+on\s+",
        "on ",
        cleaned,
        flags=re.IGNORECASE,
    )
    match = _QUESTION_FAMILY_RE.search(cleaned) or _QUESTION_FAMILY_RE.search(question)
    if match is None:
        return None
    family = re.sub(r"\s+", " ", match.group("family").strip().lower())
    return family or None


def _normalize_slug_family(slug: str | None) -> str | None:
    return normalize_slug_family(slug)


def _question_family(question: str | None) -> str | None:
    return question_family(question)


def build_canonical_event_identity(snapshot: ResearchSnapshot) -> CanonicalEventIdentity:
    """Deterministic event identity with explicit evidence hierarchy.

    Hierarchy:
    1. explicit event_id
    2. parent/neg-risk market-family identifier
    3. event_slug / slug-family evidence
    4. otherwise fail closed as ambiguous (never city+station+date alone)
    """
    if snapshot.event_id:
        return CanonicalEventIdentity(
            canonical_event_key=("event_id", snapshot.event_id),
            source="event_id",
            evidence_fields=("event_id",),
        )
    if snapshot.neg_risk_market_id:
        return CanonicalEventIdentity(
            canonical_event_key=("neg_risk_market_id", snapshot.neg_risk_market_id),
            source="neg_risk_market_id",
            evidence_fields=("neg_risk_market_id",),
        )
    if snapshot.event_slug:
        normalized_event_slug = snapshot.event_slug.strip().lower()
        return CanonicalEventIdentity(
            canonical_event_key=("event_slug", normalized_event_slug),
            source="event_slug",
            evidence_fields=("event_slug",),
        )
    slug_family = normalize_slug_family(snapshot.slug)
    if slug_family:
        return CanonicalEventIdentity(
            canonical_event_key=("slug_family", slug_family),
            source="slug_family",
            evidence_fields=("slug",),
        )
    q_family = question_family(snapshot.question)
    if q_family and snapshot.city and snapshot.station_icao and snapshot.event_date:
        return CanonicalEventIdentity(
            canonical_event_key=(
                "question_family",
                q_family,
                snapshot.city,
                snapshot.station_icao,
                snapshot.event_date,
            ),
            source="question_family",
            evidence_fields=("question", "city", "station_icao", "event_date"),
        )
    return CanonicalEventIdentity(
        canonical_event_key=("ambiguous", snapshot.condition_id, snapshot.token_id),
        source="ambiguous",
        evidence_fields=(),
        ambiguous=True,
        quarantine_reason=EVENT_IDENTITY_AMBIGUOUS,
    )


def event_group_key(snapshot: ResearchSnapshot) -> tuple[str, ...]:
    """Group mutually exclusive buckets via canonical identity; never city/station/date alone."""
    if snapshot.canonical_event_key:
        return snapshot.canonical_event_key
    return build_canonical_event_identity(snapshot).canonical_event_key
