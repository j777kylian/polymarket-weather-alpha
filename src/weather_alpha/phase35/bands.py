"""Fixed descriptive bands and stratification keys for Phase 3.5 historical analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from weather_alpha.phase35.config import DESCRIPTIVE_PRICE_BANDS
from weather_alpha.research.backtest import classify_bucket_region, classify_season

STRATIFICATION_KEYS: tuple[str, ...] = (
    "city",
    "station",
    "month",
    "season",
    "lead",
    "native_bucket",
    "center_tail",
    "price",
    "model",
    "edge",
)

MODEL_PROBABILITY_BANDS: tuple[tuple[str, float, float], ...] = (
    ("<10%", 0.0, 0.10),
    ("10-20%", 0.10, 0.20),
    ("20-30%", 0.20, 0.30),
    ("30-40%", 0.30, 0.40),
    ("40-50%", 0.40, 0.50),
    ("50-60%", 0.50, 0.60),
    ("60-70%", 0.60, 0.70),
    ("70-80%", 0.70, 0.80),
    ("80-90%", 0.80, 0.90),
    (">=90%", 0.90, 1.0000001),
)

RAW_EDGE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("<=-20c", float("-inf"), -0.20),
    ("-20c--10c", -0.20, -0.10),
    ("-10c--5c", -0.10, -0.05),
    ("-5c-0c", -0.05, 0.0),
    ("0c-5c", 0.0, 0.05),
    ("5c-10c", 0.05, 0.10),
    ("10c-20c", 0.10, 0.20),
    (">20c", 0.20, float("inf")),
)


@dataclass(frozen=True, slots=True)
class StratificationRecord:
    city: str | None
    station: str | None
    month: str | None
    season: str | None
    lead: str | None
    native_bucket: str | None
    center_tail: str
    price: str | None
    model: str | None
    edge: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "center_tail": self.center_tail,
            "city": self.city,
            "edge": self.edge,
            "lead": self.lead,
            "model": self.model,
            "month": self.month,
            "native_bucket": self.native_bucket,
            "price": self.price,
            "season": self.season,
            "station": self.station,
        }


def descriptive_price_band(probability: float | None) -> str | None:
    """Map descriptive market probability to a fixed cent band. Never executable."""
    if probability is None:
        return None
    cents = probability * 100.0
    if cents < 1.0:
        return "<1c"
    if cents < 3.0:
        return "1-3c"
    if cents < 5.0:
        return "3-5c"
    if cents < 10.0:
        return "5-10c"
    if cents < 20.0:
        return "10-20c"
    return ">20c"


def model_probability_band(probability: float | None) -> str | None:
    if probability is None:
        return None
    for label, lo, hi in MODEL_PROBABILITY_BANDS:
        if lo <= probability < hi:
            return label
    return None


def raw_edge_band(edge: float | None) -> str | None:
    if edge is None:
        return None
    for label, lo, hi in RAW_EDGE_BANDS:
        if lo <= edge < hi:
            return label
    return None


def calendar_month(event_date: str) -> str:
    return event_date[5:7]


def build_stratification(
    *,
    city: str | None,
    station: str | None,
    event_date: str,
    lead_hours: int | float | None,
    bucket_kind: str | None,
    descriptive_probability: float | None,
    model_probability: float | None,
    raw_edge: float | None,
) -> StratificationRecord:
    return StratificationRecord(
        city=city,
        station=station,
        month=calendar_month(event_date),
        season=classify_season(event_date),
        lead=None if lead_hours is None else f"{int(lead_hours)}h",
        native_bucket=bucket_kind,
        center_tail=classify_bucket_region(bucket_kind),
        price=descriptive_price_band(descriptive_probability),
        model=model_probability_band(model_probability),
        edge=raw_edge_band(raw_edge),
    )


def assert_price_bands_registered() -> tuple[str, ...]:
    if DESCRIPTIVE_PRICE_BANDS != (
        "<1c",
        "1-3c",
        "3-5c",
        "5-10c",
        "10-20c",
        ">20c",
    ):
        raise RuntimeError("descriptive price bands drifted from pre-registration")
    return DESCRIPTIVE_PRICE_BANDS
