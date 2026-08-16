"""Phase 3.5 pre-registered configuration. Not optimized from outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# Exact pre-registered decision lead times (hours before local event midnight).
PRE_REGISTERED_CHECKPOINT_HOURS: tuple[int, ...] = (48, 24, 12, 6, 3, 1)

# Descriptive-only historical price bands (cents). Never executable labels.
DESCRIPTIVE_PRICE_BANDS: tuple[str, ...] = (
    "<1c",
    "1-3c",
    "3-5c",
    "5-10c",
    "10-20c",
    ">20c",
)

# Pre-registered acceptance thresholds (event groups = inference blocks).
MIN_HELD_OUT_EVENT_GROUPS = 250
MIN_HELD_OUT_EVENT_GROUPS_PER_LEAD = 50
MIN_CITIES = 3
MIN_SEASONS = 2

# Paper-only hypothetical ask sizes (shares). Fixed; not outcome-optimized.
DEFAULT_HYPOTHETICAL_SIZES: tuple[float, ...] = (10.0, 50.0, 100.0, 500.0)

# Documented Open-Meteo Single Runs ECMWF IFS availability floor (not a skill claim).
ECMWF_SINGLE_RUNS_EARLIEST_DEFENSIBLE = "2024-03-01"

FeeStatus = Literal["unknown", "externally_sourced"]

HISTORICAL_DATA_ROOT = Path("data/phase35/historical")
FORWARD_DATA_ROOT = Path("data/phase35/forward")


@dataclass(frozen=True, slots=True)
class Phase35ForwardConfig:
    """Forward collection paper assumptions. Observed book facts stay separate."""

    hypothetical_sizes: tuple[float, ...] = DEFAULT_HYPOTHETICAL_SIZES
    fee_rate: float | None = None
    fee_status: FeeStatus = "unknown"
    fee_source: str | None = None
    slippage_methodology: str = (
        "offline_vwap_walk_asks_only; no order submission; no dynamic optimization"
    )
    depth_methodology: str = (
        "sum ask sizes at or better than VWAP fill ladder until size filled or exhausted"
    )

    def __post_init__(self) -> None:
        if any(size <= 0 for size in self.hypothetical_sizes):
            raise ValueError("hypothetical sizes must be positive")
        if self.fee_status == "unknown" and self.fee_rate is not None:
            raise ValueError("fee_rate must be null when fee_status is unknown")
        if self.fee_status == "externally_sourced" and self.fee_rate is None:
            raise ValueError("externally_sourced fee_status requires fee_rate")
        if self.fee_status == "externally_sourced" and not self.fee_source:
            raise ValueError("externally_sourced fee_status requires fee_source")


@dataclass(frozen=True, slots=True)
class Phase35AcceptanceThresholds:
    min_held_out_event_groups: int = MIN_HELD_OUT_EVENT_GROUPS
    min_held_out_per_lead: int = MIN_HELD_OUT_EVENT_GROUPS_PER_LEAD
    min_cities: int = MIN_CITIES
    min_seasons: int = MIN_SEASONS
