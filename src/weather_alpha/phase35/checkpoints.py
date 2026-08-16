"""Pre-registered Phase 3.5 decision checkpoints and leakage-safe selection.

Canonical event group is the inference/blocking unit. Forecast and price inputs
must be at or before decision_ts; ages are recorded; future/missing inputs reject.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from weather_alpha.models.timeutil import ensure_utc
from weather_alpha.phase35.config import PRE_REGISTERED_CHECKPOINT_HOURS
from weather_alpha.research.prices import PricePoint, select_price_at_or_before

SelectionStatus = Literal["selected", "rejected"]


@dataclass(frozen=True, slots=True)
class ForecastCandidate:
    issued_at: datetime
    available_at: datetime
    run_param: str
    provider: str = "open-meteo-single-run"
    model: str = "ecmwf_ifs"

    def __post_init__(self) -> None:
        object.__setattr__(self, "issued_at", ensure_utc(self.issued_at))
        object.__setattr__(self, "available_at", ensure_utc(self.available_at))


@dataclass(frozen=True, slots=True)
class CheckpointSelection:
    status: SelectionStatus
    checkpoint_lead_hours: int
    decision_ts: datetime
    canonical_event_key: tuple[str, ...]
    forecast: ForecastCandidate | None
    price: PricePoint | None
    forecast_age_seconds: float | None
    price_age_seconds: float | None
    rejection_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_ts", ensure_utc(self.decision_ts))

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical_event_key": list(self.canonical_event_key),
            "checkpoint_lead_hours": self.checkpoint_lead_hours,
            "decision_ts": self.decision_ts.isoformat(),
            "forecast": None
            if self.forecast is None
            else {
                "available_at": self.forecast.available_at.isoformat(),
                "issued_at": self.forecast.issued_at.isoformat(),
                "model": self.forecast.model,
                "provider": self.forecast.provider,
                "run_param": self.forecast.run_param,
            },
            "forecast_age_seconds": self.forecast_age_seconds,
            "price": None
            if self.price is None
            else {
                "descriptive_probability": self.price.price,
                "observed_at": self.price.observed_at.isoformat(),
            },
            "price_age_seconds": self.price_age_seconds,
            "rejection_reasons": list(self.rejection_reasons),
            "status": self.status,
        }


def registered_checkpoints() -> tuple[int, ...]:
    return PRE_REGISTERED_CHECKPOINT_HOURS


def decision_timestamp(
    event_date: str,
    timezone_name: str,
    lead_hours: int | float,
) -> datetime:
    """Deterministic decision time: local event-day midnight minus lead hours (UTC)."""
    lead = int(lead_hours)
    if lead != lead_hours or lead not in PRE_REGISTERED_CHECKPOINT_HOURS:
        raise ValueError(
            f"lead_hours {lead_hours} is not a pre-registered checkpoint "
            f"{PRE_REGISTERED_CHECKPOINT_HOURS}"
        )
    year, month, day = (int(part) for part in event_date.split("-"))
    local_start = datetime(year, month, day, 0, 0, tzinfo=ZoneInfo(timezone_name))
    return (local_start - timedelta(hours=lead)).astimezone(UTC)


def select_forecast_at_or_before(
    candidates: tuple[ForecastCandidate, ...] | list[ForecastCandidate],
    decision_ts: datetime,
) -> ForecastCandidate | None:
    cutoff = ensure_utc(decision_ts)
    eligible = [row for row in candidates if row.available_at <= cutoff]
    if not eligible:
        return None
    # Prefer latest available_at; tie-break on issued_at then run_param.
    return max(eligible, key=lambda row: (row.available_at, row.issued_at, row.run_param))


def select_checkpoint_inputs(
    *,
    event_date: str,
    timezone_name: str,
    lead_hours: int,
    canonical_event_key: tuple[str, ...],
    forecasts: tuple[ForecastCandidate, ...] | list[ForecastCandidate],
    prices: tuple[PricePoint, ...] | list[PricePoint],
) -> CheckpointSelection:
    if lead_hours not in PRE_REGISTERED_CHECKPOINT_HOURS:
        raise ValueError(
            f"lead_hours {lead_hours} is not a pre-registered checkpoint "
            f"{PRE_REGISTERED_CHECKPOINT_HOURS}"
        )
    decision = decision_timestamp(event_date, timezone_name, lead_hours)
    reasons: list[str] = []

    # Reject future-dated forecast candidates explicitly (defense in depth).
    future_forecasts = [row for row in forecasts if row.available_at > decision]
    forecast = select_forecast_at_or_before(forecasts, decision)
    if forecast is None:
        if future_forecasts and not any(row.available_at <= decision for row in forecasts):
            reasons.append("forecast_future_only")
        else:
            reasons.append("forecast_missing_or_unavailable")

    future_prices = [row for row in prices if row.observed_at > decision and row.price is not None]
    price = select_price_at_or_before(prices, decision)
    if price is None:
        if future_prices and not any(
            row.observed_at <= decision and row.price is not None for row in prices
        ):
            reasons.append("price_post_decision_only")
        else:
            reasons.append("price_missing_or_invalid")

    if reasons:
        return CheckpointSelection(
            status="rejected",
            checkpoint_lead_hours=lead_hours,
            decision_ts=decision,
            canonical_event_key=canonical_event_key,
            forecast=None,
            price=None,
            forecast_age_seconds=None,
            price_age_seconds=None,
            rejection_reasons=tuple(reasons),
        )

    assert forecast is not None
    assert price is not None
    forecast_age = (decision - forecast.available_at).total_seconds()
    price_age = (decision - price.observed_at).total_seconds()
    if forecast_age < 0 or price_age < 0:
        return CheckpointSelection(
            status="rejected",
            checkpoint_lead_hours=lead_hours,
            decision_ts=decision,
            canonical_event_key=canonical_event_key,
            forecast=None,
            price=None,
            forecast_age_seconds=None,
            price_age_seconds=None,
            rejection_reasons=("negative_age_leakage",),
        )
    return CheckpointSelection(
        status="selected",
        checkpoint_lead_hours=lead_hours,
        decision_ts=decision,
        canonical_event_key=canonical_event_key,
        forecast=forecast,
        price=price,
        forecast_age_seconds=forecast_age,
        price_age_seconds=price_age,
        rejection_reasons=(),
    )


def select_all_registered_checkpoints(
    *,
    event_date: str,
    timezone_name: str,
    canonical_event_key: tuple[str, ...],
    forecasts: tuple[ForecastCandidate, ...] | list[ForecastCandidate],
    prices: tuple[PricePoint, ...] | list[PricePoint],
) -> tuple[CheckpointSelection, ...]:
    return tuple(
        select_checkpoint_inputs(
            event_date=event_date,
            timezone_name=timezone_name,
            lead_hours=lead,
            canonical_event_key=canonical_event_key,
            forecasts=forecasts,
            prices=prices,
        )
        for lead in PRE_REGISTERED_CHECKPOINT_HOURS
    )
