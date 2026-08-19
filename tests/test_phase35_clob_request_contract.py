"""CLOB request-contract repair tests. Fake GET transports only.

Does not contact Gamma/ECMWF/CLOB, create production manifests/receipts, or write git.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.fakes import RecordingGetTransport
from tests.test_phase35_full_collection_orchestrator import (
    PARIS_DAY,
    _production_routes,
    _service,
)
from weather_alpha.phase35.checkpoints import decision_timestamp
from weather_alpha.phase35.full_collection.audit import ExpectedCell
from weather_alpha.phase35.full_collection.clob_contract import (
    canonical_clob_identity,
    clob_range_params,
    clob_window_timestamps,
    plan_clob_gets,
)
from weather_alpha.phase35.full_collection.orchestrator import FullHistoricalCollectionService
from weather_alpha.phase35.full_collection.policy import (
    CHECKPOINTS,
    CLOB_FIDELITY_MINUTES,
    CLOB_WINDOW_EXTRA_LOOKBACK_SECONDS,
)
from weather_alpha.research.prices import PricePoint, select_price_at_or_before

TOKEN = "tok-yes-1"
CITY = "paris"
STATION = "LFPG"
TIMEZONE = "Europe/Paris"
FAMILY_ID = "event_id:evt-paris-2026-03-01"


def _family() -> dict[str, Any]:
    return {
        "city": CITY,
        "date": PARIS_DAY,
        "event_family_id": FAMILY_ID,
        "has_settlement": True,
        "station": STATION,
        "timezone_name": TIMEZONE,
        "yes_token_ids": [TOKEN],
    }


def _expected() -> tuple[ExpectedCell, ...]:
    return tuple(
        ExpectedCell(
            date=PARIS_DAY,
            city=CITY,
            station=STATION,
            checkpoint=lead,
            event_family_id=FAMILY_ID,
            month=PARIS_DAY[:7],
            ecmwf_run_cycle=None,
        )
        for lead in CHECKPOINTS
    )


def test_clob_planner_emits_market_start_end_fidelity() -> None:
    plans, mapping = plan_clob_gets(_expected(), [_family()])
    assert len(plans) == 1
    params = dict(plans[0].params)
    assert set(params) == {"market", "startTs", "endTs", "fidelity"}
    assert params["market"] == TOKEN
    assert params["fidelity"] == CLOB_FIDELITY_MINUTES
    assert isinstance(params["startTs"], int)
    assert isinstance(params["endTs"], int)
    assert mapping[plans[0].identity]
    assert plans[0].identity != f"clob:{CITY}:{PARIS_DAY}"


def test_clob_window_follows_frozen_rule() -> None:
    start_ts, end_ts = clob_window_timestamps(PARIS_DAY, TIMEZONE)
    expected_end = int(decision_timestamp(PARIS_DAY, TIMEZONE, 1).timestamp())
    expected_start = (
        int(decision_timestamp(PARIS_DAY, TIMEZONE, 48).timestamp())
        - CLOB_WINDOW_EXTRA_LOOKBACK_SECONDS
    )
    assert end_ts == expected_end
    assert start_ts == expected_start
    plans, _mapping = plan_clob_gets(_expected(), [_family()])
    params = dict(plans[0].params)
    assert params["startTs"] == expected_start
    assert params["endTs"] == expected_end
    assert params["fidelity"] == 60


def test_canonical_identity_changes_with_contract_fields() -> None:
    start_ts, end_ts = clob_window_timestamps(PARIS_DAY, TIMEZONE)
    base = canonical_clob_identity(market=TOKEN, start_ts=start_ts, end_ts=end_ts, fidelity=60)
    assert base != f"clob:{CITY}:{PARIS_DAY}"
    assert (
        canonical_clob_identity(market="other", start_ts=start_ts, end_ts=end_ts, fidelity=60)
        != base
    )
    assert (
        canonical_clob_identity(market=TOKEN, start_ts=start_ts + 1, end_ts=end_ts, fidelity=60)
        != base
    )
    assert (
        canonical_clob_identity(market=TOKEN, start_ts=start_ts, end_ts=end_ts + 1, fidelity=60)
        != base
    )
    assert (
        canonical_clob_identity(market=TOKEN, start_ts=start_ts, end_ts=end_ts, fidelity=5) != base
    )
    params = clob_range_params(TOKEN, start_ts, end_ts)
    plans, _mapping = plan_clob_gets(_expected(), [_family()])
    assert plans[0].identity == canonical_clob_identity(
        market=str(params["market"]),
        start_ts=int(params["startTs"]),
        end_ts=int(params["endTs"]),
        fidelity=int(params["fidelity"]),
    )


def test_old_market_fidelity_only_shape_cannot_be_emitted() -> None:
    plans, _mapping = plan_clob_gets(_expected(), [_family()])
    for planned in plans:
        keys = set(planned.params)
        assert "startTs" in keys
        assert "endTs" in keys
        assert keys != {"fidelity", "market"}
        assert planned.identity.startswith("clob:")
        assert planned.identity != f"clob:{CITY}:{PARIS_DAY}"


def test_pit_selection_observed_at_lte_decision_for_all_checkpoints() -> None:
    points = []
    for lead in CHECKPOINTS:
        decision = decision_timestamp(PARIS_DAY, TIMEZONE, lead)
        points.append(PricePoint(observed_at=decision, price=0.10 + (lead / 1000.0)))
    post = decision_timestamp(PARIS_DAY, TIMEZONE, 1).replace(year=2026)
    later = datetime(2026, 3, 1, 12, tzinfo=UTC)
    points.append(PricePoint(observed_at=later, price=0.99))
    del post
    for lead in CHECKPOINTS:
        decision = decision_timestamp(PARIS_DAY, TIMEZONE, lead)
        chosen = select_price_at_or_before(points, decision)
        assert chosen is not None
        assert chosen.observed_at <= decision
        assert chosen.price != 0.99


def test_post_decision_price_never_usable(tmp_path: Path) -> None:
    post = {
        "history": [
            {"t": int(datetime(2026, 3, 2, 12, tzinfo=UTC).timestamp()), "p": 0.77},
        ]
    }
    transport = RecordingGetTransport(_production_routes(clob=post))
    result = _service(tmp_path, transport).run()
    namespace = tmp_path / "collections" / result.collection_id
    plans = json.loads((namespace / "plans" / "clob.json").read_text(encoding="utf-8"))
    assert plans
    params = dict(plans[0]["params"])
    assert set(params) == {"market", "startTs", "endTs", "fidelity"}
    observations = json.loads((namespace / "observations.json").read_text(encoding="utf-8"))
    assert len(observations) == 6
    for row in observations:
        assert row["has_price_history"] is False
        assert "NO_PRE_DECISION_PRICE" in row["missing_reasons"] or row["future_leakage"] is True
    clob_calls = [call for call in transport.calls if "/prices-history" in call[1]]
    assert clob_calls
    joined = clob_calls[0][1]
    assert "startTs=" in joined
    assert "endTs=" in joined
    assert "fidelity=60" in joined
    assert "network_authorized" not in FullHistoricalCollectionService.__init__.__code__.co_varnames
