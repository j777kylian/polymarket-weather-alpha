"""Price-history selection: no future prices; missing stays missing."""

from __future__ import annotations

from datetime import UTC, datetime

from weather_alpha.research.prices import PricePoint, select_price_at_or_before


def test_selects_nearest_price_at_or_before_decision() -> None:
    points = (
        PricePoint(observed_at=datetime(2026, 7, 14, 10, 0, tzinfo=UTC), price=0.41),
        PricePoint(observed_at=datetime(2026, 7, 14, 11, 0, tzinfo=UTC), price=0.44),
        PricePoint(observed_at=datetime(2026, 7, 14, 13, 0, tzinfo=UTC), price=0.90),
    )
    decision = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    selected = select_price_at_or_before(points, decision)
    assert selected is not None
    assert selected.price == 0.44
    assert selected.observed_at == datetime(2026, 7, 14, 11, 0, tzinfo=UTC)


def test_future_only_prices_are_not_used() -> None:
    points = (PricePoint(observed_at=datetime(2026, 7, 14, 13, 0, tzinfo=UTC), price=0.90),)
    decision = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    assert select_price_at_or_before(points, decision) is None


def test_invalid_or_missing_p_is_skipped_never_zero() -> None:
    points = (
        PricePoint(observed_at=datetime(2026, 7, 14, 10, 0, tzinfo=UTC), price=None),
        PricePoint(observed_at=datetime(2026, 7, 14, 10, 30, tzinfo=UTC), price=float("nan")),
        PricePoint(observed_at=datetime(2026, 7, 14, 11, 0, tzinfo=UTC), price=-0.1),
        PricePoint(observed_at=datetime(2026, 7, 14, 11, 30, tzinfo=UTC), price=1.5),
    )
    decision = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    assert select_price_at_or_before(points, decision) is None


def test_naive_decision_timestamp_is_rejected() -> None:
    points = (PricePoint(observed_at=datetime(2026, 7, 14, 10, 0, tzinfo=UTC), price=0.4),)
    try:
        select_price_at_or_before(points, datetime(2026, 7, 14, 12, 0, 0))
    except ValueError as exc:
        assert "timezone-aware" in str(exc)
    else:
        raise AssertionError("expected naive datetime rejection")
