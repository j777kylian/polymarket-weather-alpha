"""CLOB prices-history helpers. `p` is descriptive only; missing stays missing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from weather_alpha.models.timeutil import ensure_utc, parse_timestamp
from weather_alpha.research.provider_schema import (
    ProviderSchemaError,
    validate_prices_history_payload,
)


@dataclass(frozen=True, slots=True)
class PricePoint:
    observed_at: datetime
    price: float | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))


def is_valid_probability(value: float | None) -> bool:
    if value is None:
        return False
    return isfinite(value) and 0.0 <= value <= 1.0


def select_price_at_or_before(
    points: tuple[PricePoint, ...] | list[PricePoint],
    decision_ts: datetime,
) -> PricePoint | None:
    cutoff = ensure_utc(decision_ts)
    eligible = [
        point
        for point in points
        if point.observed_at <= cutoff and is_valid_probability(point.price)
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda point: point.observed_at)


def parse_price_history_points(payload: object) -> tuple[PricePoint, ...]:
    validation = validate_prices_history_payload(payload)
    if validation.status not in {"ok", "empty"}:
        raise ProviderSchemaError(
            validation.detail or "prices-history schema invalid",
            status=validation.status,
            provider=validation.provider,
        )
    if validation.status == "empty":
        return ()
    assert isinstance(payload, dict)
    history = payload.get("history")
    assert isinstance(history, list)
    points: list[PricePoint] = []
    for item in history:
        assert isinstance(item, dict)
        observed_at = parse_timestamp(item["t"])
        raw_p = item["p"]
        price = float(raw_p.strip()) if isinstance(raw_p, str) else float(raw_p)
        if not is_valid_probability(price):
            raise ProviderSchemaError(
                "prices-history nested record has invalid p",
                status="malformed",
                provider=validation.provider,
            )
        points.append(PricePoint(observed_at=observed_at, price=price))
    return tuple(points)
