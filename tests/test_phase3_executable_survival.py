"""Blocker 5 adversarial: executable survival is a validated tri-state only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from weather_alpha.research.tail import (
    ExecutableSurvivalState,
    analyze_tails,
    build_executable_survival,
)
from weather_alpha.research.types import ResearchSnapshot


def _snap(*, token: str = "a", p: float = 0.005) -> ResearchSnapshot:
    return ResearchSnapshot(
        condition_id="c",
        market_id="m",
        token_id=token,
        city="paris",
        station_icao="LFPG",
        event_date="2026-07-15",
        bucket_label="40°C or higher",
        bucket_kind="above",
        temperature_celsius_min=40.0,
        temperature_celsius_max=None,
        decision_ts=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        market_probability=p,
        executable_entry_price=None,
        best_bid=None,
        best_ask=None,
        midpoint=None,
        spread=None,
        volume=None,
        liquidity=None,
        weather_issued_at=datetime(2026, 7, 14, 0, 0, tzinfo=UTC),
        weather_available_at=datetime(2026, 7, 14, 6, 0, tzinfo=UTC),
        forecast_daily_max_c=31.0,
        observation_max_so_far_c=None,
        observation_as_of=None,
        settlement_label="No",
        diagnostic_actual_max_c=31.0,
        provenance_urls=(),
        raw_paths=(),
        content_hashes=(),
        limitations=("test",),
    )


def test_unknown_measured_survived_and_failed_construction() -> None:
    unknown = build_executable_survival(None, "unknown_no_historical_asks")
    assert unknown is ExecutableSurvivalState.UNKNOWN
    assert unknown.survival is None
    assert unknown.status == "unknown_no_historical_asks"

    survived = build_executable_survival(True, "measured_survived")
    assert survived is ExecutableSurvivalState.MEASURED_SURVIVED
    assert survived.survival is True

    failed = build_executable_survival(False, "measured_did_not_survive")
    assert failed is ExecutableSurvivalState.MEASURED_DID_NOT_SURVIVE
    assert failed.survival is False


@pytest.mark.parametrize(
    ("value", "status"),
    [
        (True, "unknown_no_historical_asks"),
        (False, "unknown_no_historical_asks"),
        (None, "measured_survived"),
        (None, "measured_did_not_survive"),
        (True, "measured_did_not_survive"),
        (False, "measured_survived"),
        (True, "arbitrary"),
        (False, ""),
        (None, None),
    ],
)
def test_contradictory_survival_combinations_rejected(
    value: bool | None, status: str | None
) -> None:
    with pytest.raises(ValueError):
        build_executable_survival(value, status)
    if status is None:
        # analyze_tails infers status when omitted; only explicit contradictions fail.
        return
    with pytest.raises(ValueError):
        analyze_tails(
            snapshots=(_snap(),),
            model_probabilities={"a": 0.02},
            split_name="test",
            executable_survival=value,
            executable_survival_status=status,
        )


def test_default_analyze_tails_is_unknown_with_null_economics() -> None:
    report = analyze_tails(
        snapshots=(_snap(),),
        model_probabilities={"a": 0.02},
        split_name="test",
    )
    assert report.executable_survival is None
    assert report.executable_survival_status == "unknown_no_historical_asks"
    assert report.pnl is None
    assert report.roi is None
    assert report.max_drawdown is None
    assert report.profit_factor is None
    assert report.robustness_remove_largest_1_pnl is None
    assert "unknown_no_historical_asks" in report.notes[0]
    assert "null" in report.notes[0].lower() or "unavailable" in report.notes[0].lower()


def test_measured_states_keep_notes_aligned() -> None:
    survived = analyze_tails(
        snapshots=(_snap(),),
        model_probabilities={"a": 0.02},
        split_name="test",
        executable_survival=True,
        executable_survival_status="measured_survived",
    )
    assert survived.executable_survival is True
    assert "measured_survived" in survived.notes[0]
    failed = analyze_tails(
        snapshots=(_snap(),),
        model_probabilities={"a": 0.02},
        split_name="test",
        executable_survival=False,
        executable_survival_status="measured_did_not_survive",
    )
    assert failed.executable_survival is False
    assert "measured_did_not_survive" in failed.notes[0]
