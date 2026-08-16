"""Typed research records. Timestamps are timezone-aware UTC."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from weather_alpha.models.timeutil import ensure_utc


def _opt_utc(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


@dataclass(frozen=True, slots=True)
class Provenance:
    source: str
    retrieved_at: datetime
    request_url: str
    raw_path: str | None
    content_sha256: str | None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "retrieved_at", ensure_utc(self.retrieved_at))


@dataclass(frozen=True, slots=True)
class NormalizedMarket:
    condition_id: str
    question: str
    parse_status: str
    provenance: Provenance
    market_id: str | None = None
    event_id: str | None = None
    slug: str | None = None
    event_slug: str | None = None
    neg_risk_market_id: str | None = None
    description: str | None = None
    city: str | None = None
    station_icao: str | None = None
    event_date: str | None = None
    parse_notes: tuple[str, ...] = ()
    closed: bool | None = None
    active: bool | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_time", _opt_utc(self.start_time))
        object.__setattr__(self, "end_time", _opt_utc(self.end_time))


@dataclass(frozen=True, slots=True)
class MarketOutcome:
    condition_id: str
    token_id: str
    outcome_label: str
    provenance: Provenance
    outcome_index: int | None = None
    temperature_celsius_min: float | None = None
    temperature_celsius_max: float | None = None
    bucket_kind: str | None = None
    group_item_title: str | None = None
    temperature_unit: str | None = None
    temperature_native_min: float | None = None
    temperature_native_max: float | None = None


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    token_id: str
    observed_at: datetime
    price: float
    provenance: Provenance
    condition_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))


@dataclass(frozen=True, slots=True)
class TradeRecord:
    trade_id: str
    token_id: str
    side: str
    price: float
    size: float
    traded_at: datetime
    provenance: Provenance
    transaction_hash: str | None = None
    condition_id: str | None = None
    outcome_label: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "traded_at", ensure_utc(self.traded_at))


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    side: str
    price: float
    size: float
    level_index: int


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    snapshot_id: str
    token_id: str
    observed_at: datetime
    provenance: Provenance
    condition_id: str | None = None
    hash: str | None = None
    tick_size: float | None = None
    min_order_size: float | None = None
    last_trade_price: float | None = None
    levels: tuple[OrderBookLevel, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))


@dataclass(frozen=True, slots=True)
class WeatherForecast:
    station_id: str
    provider: str
    valid_time: datetime
    variable: str
    provenance: Provenance
    model: str | None = None
    issued_at: datetime | None = None
    temperature_celsius: float | None = None
    source_value: float | None = None
    source_unit: str | None = None
    lead_hours: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "valid_time", ensure_utc(self.valid_time))
        object.__setattr__(self, "issued_at", _opt_utc(self.issued_at))


@dataclass(frozen=True, slots=True)
class ForecastEnsembleMember:
    station_id: str
    provider: str
    valid_time: datetime
    member_id: str
    variable: str
    provenance: Provenance
    model: str | None = None
    issued_at: datetime | None = None
    temperature_celsius: float | None = None
    source_value: float | None = None
    source_unit: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "valid_time", ensure_utc(self.valid_time))
        object.__setattr__(self, "issued_at", _opt_utc(self.issued_at))


@dataclass(frozen=True, slots=True)
class HourlyObservation:
    station_id: str
    provider: str
    observed_at: datetime
    variable: str
    provenance: Provenance
    temperature_celsius: float | None = None
    source_value: float | None = None
    source_unit: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))


@dataclass(frozen=True, slots=True)
class DailyActualMaximum:
    station_id: str
    provider: str
    local_date: str
    timezone_name: str
    provenance: Provenance
    temperature_celsius: float | None = None
    source_value: float | None = None
    source_unit: str | None = None
