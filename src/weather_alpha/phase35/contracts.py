"""Typed Phase 3.5 namespaces: historical descriptive vs forward executable.

It must be impossible to confuse historical CLOB descriptive prices with
forward executable order-book snapshots. Never call historical p an ask/bid/fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypeGuard

from weather_alpha.models.timeutil import ensure_utc
from weather_alpha.phase35.config import FORWARD_DATA_ROOT, HISTORICAL_DATA_ROOT

DataTrack = Literal["historical_descriptive", "forward_executable"]

HISTORICAL_TRACK: DataTrack = "historical_descriptive"
FORWARD_TRACK: DataTrack = "forward_executable"


class HistoricalSourceMode(StrEnum):
    """How historical research readiness was established (distinct from universe completeness)."""

    SURVIVORSHIP_LIMITED_DESCRIPTIVE = "survivorship_limited_descriptive"
    NOT_ESTABLISHED = "not_established"


class HistoricalUniverseComplete(StrEnum):
    """Whether the historical market universe has been proven complete."""

    YES = "yes"
    NO = "no"
    NOT_PROVEN = "not_proven"


FORBIDDEN_HISTORICAL_EXECUTABLE_LABELS = frozenset(
    {
        "executable_entry",
        "executable entry",
        "ask",
        "bid",
        "fill",
        "realized trade price",
        "realized_trade_price",
    }
)

# Canonical serialized provenance for forward raw payloads (root-independent).
FORWARD_RAW_PROVENANCE_PREFIX = "forward/raw/"


def stable_forward_raw_provenance_path(raw_path: str | Path) -> str:
    """Map a runtime raw path to stable POSIX provenance under forward/raw/.

    Absolute storage locations may differ across output roots; canonical reports
    must expose only the repository data-namespace relative identity, e.g.
    ``forward/raw/<source>/<hh>/<digest>.json``. Does not special-case host roots.
    """
    posix = Path(raw_path).as_posix()
    marker = FORWARD_RAW_PROVENANCE_PREFIX
    index = posix.find(marker)
    if index >= 0:
        return posix[index:]
    if posix == marker.rstrip("/"):
        return posix
    raise ValueError(
        "forward raw_path must contain "
        f"{marker!r} for stable provenance serialization; got {posix!r}"
    )


class Phase35TypeConfusionError(TypeError):
    """Raised when historical descriptive data is treated as executable."""


@dataclass(frozen=True, slots=True)
class HistoricalDescriptivePrice:
    """CLOB prices-history point. Descriptive probability only — never executable."""

    track: DataTrack
    observed_at: datetime
    descriptive_probability: float
    token_id: str
    condition_id: str
    canonical_event_key: tuple[str, ...]
    provenance_url: str | None = None
    raw_path: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.track != HISTORICAL_TRACK:
            raise ValueError("HistoricalDescriptivePrice.track must be historical_descriptive")
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))
        if not 0.0 <= self.descriptive_probability <= 1.0:
            raise ValueError("descriptive_probability must be in [0, 1]")

    @property
    def executable_entry_price(self) -> None:
        return None

    @property
    def best_bid(self) -> None:
        return None

    @property
    def best_ask(self) -> None:
        return None

    def as_executable_price(self) -> float:
        raise Phase35TypeConfusionError(
            "historical CLOB descriptive probability is not an executable price, "
            "ask, bid, fill, or realized trade price"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical_event_key": list(self.canonical_event_key),
            "condition_id": self.condition_id,
            "content_sha256": self.content_sha256,
            "descriptive_probability": self.descriptive_probability,
            "executable_entry_price": None,
            "observed_at": self.observed_at.isoformat(),
            "provenance_url": self.provenance_url,
            "raw_path": self.raw_path,
            "token_id": self.token_id,
            "track": self.track,
        }


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: float
    size: float

    def __post_init__(self) -> None:
        if self.price <= 0 or self.size <= 0:
            raise ValueError("book levels must have positive price and size")


@dataclass(frozen=True, slots=True)
class ForwardExecutableBookSnapshot:
    """Prospective GET /book snapshot. Observed facts separate from paper assumptions."""

    track: DataTrack
    canonical_event_id: str
    condition_id: str
    market_id: str | None
    token_id: str
    city: str | None
    station_icao: str | None
    event_date: str
    native_unit: str | None
    bucket_definition: str | None
    decision_ts: datetime
    retrieval_ts: datetime
    orderbook_ts: datetime | None
    checkpoint_lead_hours: int
    provider: str
    model: str | None
    forecast_issued_at: datetime | None
    forecast_available_at: datetime | None
    model_probability: float | None
    descriptive_market_probability: float | None
    best_bid: float | None
    best_ask: float | None
    midpoint: float | None
    spread: float | None
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    liquidity_state: str
    raw_payload: dict[str, Any]
    provenance_url: str
    raw_path: str
    content_sha256: str
    fee_rate: float | None
    fee_status: Literal["unknown", "externally_sourced"]
    settlement_label: str | None = None
    settlement_retrieved_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.track != FORWARD_TRACK:
            raise ValueError("ForwardExecutableBookSnapshot.track must be forward_executable")
        object.__setattr__(self, "decision_ts", ensure_utc(self.decision_ts))
        object.__setattr__(self, "retrieval_ts", ensure_utc(self.retrieval_ts))
        if self.orderbook_ts is not None:
            object.__setattr__(self, "orderbook_ts", ensure_utc(self.orderbook_ts))
        if self.forecast_issued_at is not None:
            object.__setattr__(self, "forecast_issued_at", ensure_utc(self.forecast_issued_at))
        if self.forecast_available_at is not None:
            object.__setattr__(
                self, "forecast_available_at", ensure_utc(self.forecast_available_at)
            )
        if self.settlement_retrieved_at is not None:
            object.__setattr__(
                self, "settlement_retrieved_at", ensure_utc(self.settlement_retrieved_at)
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "asks": [{"price": level.price, "size": level.size} for level in self.asks],
            "best_ask": self.best_ask,
            "best_bid": self.best_bid,
            "bids": [{"price": level.price, "size": level.size} for level in self.bids],
            "bucket_definition": self.bucket_definition,
            "canonical_event_id": self.canonical_event_id,
            "checkpoint_lead_hours": self.checkpoint_lead_hours,
            "city": self.city,
            "condition_id": self.condition_id,
            "content_sha256": self.content_sha256,
            "decision_ts": self.decision_ts.isoformat(),
            "descriptive_market_probability": self.descriptive_market_probability,
            "event_date": self.event_date,
            "fee_rate": self.fee_rate,
            "fee_status": self.fee_status,
            "forecast_available_at": (
                None
                if self.forecast_available_at is None
                else self.forecast_available_at.isoformat()
            ),
            "forecast_issued_at": (
                None if self.forecast_issued_at is None else self.forecast_issued_at.isoformat()
            ),
            "liquidity_state": self.liquidity_state,
            "market_id": self.market_id,
            "midpoint": self.midpoint,
            "model": self.model,
            "model_probability": self.model_probability,
            "native_unit": self.native_unit,
            "orderbook_ts": None if self.orderbook_ts is None else self.orderbook_ts.isoformat(),
            "provenance_url": self.provenance_url,
            "provider": self.provider,
            "raw_path": stable_forward_raw_provenance_path(self.raw_path),
            "retrieval_ts": self.retrieval_ts.isoformat(),
            "settlement_label": self.settlement_label,
            "settlement_retrieved_at": (
                None
                if self.settlement_retrieved_at is None
                else self.settlement_retrieved_at.isoformat()
            ),
            "spread": self.spread,
            "station_icao": self.station_icao,
            "token_id": self.token_id,
            "track": self.track,
        }


def is_historical_descriptive(value: object) -> TypeGuard[HistoricalDescriptivePrice]:
    return isinstance(value, HistoricalDescriptivePrice) and value.track == HISTORICAL_TRACK


def is_forward_executable(value: object) -> TypeGuard[ForwardExecutableBookSnapshot]:
    return isinstance(value, ForwardExecutableBookSnapshot) and value.track == FORWARD_TRACK


def reject_historical_as_executable(value: object) -> None:
    if isinstance(value, HistoricalDescriptivePrice):
        raise Phase35TypeConfusionError(
            "cannot treat HistoricalDescriptivePrice as forward executable book evidence"
        )
    if isinstance(value, dict) and value.get("track") == HISTORICAL_TRACK:
        label = str(value.get("label") or value.get("role") or "")
        if label.lower() in FORBIDDEN_HISTORICAL_EXECUTABLE_LABELS:
            raise Phase35TypeConfusionError(
                f"historical descriptive price cannot be labeled {label!r}"
            )
        raise Phase35TypeConfusionError(
            "historical descriptive track record is not executable book evidence"
        )


def storage_root_for(track: DataTrack, *, base: Path | None = None) -> Path:
    root = base or Path.cwd()
    if track == HISTORICAL_TRACK:
        return root / HISTORICAL_DATA_ROOT
    if track == FORWARD_TRACK:
        return root / FORWARD_DATA_ROOT
    raise ValueError(f"unknown track: {track!r}")


def assert_separate_storage_roots(*, base: Path | None = None) -> None:
    historical = storage_root_for(HISTORICAL_TRACK, base=base)
    forward = storage_root_for(FORWARD_TRACK, base=base)
    if historical.resolve() == forward.resolve():
        raise Phase35TypeConfusionError("historical and forward storage roots must differ")
