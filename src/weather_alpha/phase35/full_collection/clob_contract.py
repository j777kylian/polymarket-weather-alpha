"""Frozen CLOB /prices-history request contract for Phase 3.5.

Reuses the Phase 3 absolute-range semantic: market + startTs + endTs + fidelity.
The extra 48h lookback is a deterministic pre-checkpoint observation window; it
does not guarantee a pre-48h price.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from weather_alpha.config.stations import Station
from weather_alpha.phase35.checkpoints import decision_timestamp
from weather_alpha.phase35.full_collection.audit import ExpectedCell
from weather_alpha.phase35.full_collection.executor import PlannedGet
from weather_alpha.phase35.full_collection.policy import (
    CLOB_ENDPOINT,
    CLOB_FIDELITY_MINUTES,
    CLOB_WINDOW_EXTRA_LOOKBACK_SECONDS,
    PRICE_PROVIDER,
)
from weather_alpha.phase35.full_collection.schedule import catalog_stations, stations_for_city


def clob_window_timestamps(event_date: str, timezone_name: str) -> tuple[int, int]:
    end_ts = int(decision_timestamp(event_date, timezone_name, 1).timestamp())
    start_ts = (
        int(decision_timestamp(event_date, timezone_name, 48).timestamp())
        - CLOB_WINDOW_EXTRA_LOOKBACK_SECONDS
    )
    return start_ts, end_ts


def clob_range_params(
    market: str,
    start_ts: int,
    end_ts: int,
    fidelity: int = CLOB_FIDELITY_MINUTES,
) -> dict[str, Any]:
    return {
        "endTs": int(end_ts),
        "fidelity": int(fidelity),
        "market": str(market),
        "startTs": int(start_ts),
    }


def canonical_clob_identity(
    *,
    market: str,
    start_ts: int,
    end_ts: int,
    fidelity: int = CLOB_FIDELITY_MINUTES,
) -> str:
    material = json.dumps(
        {
            "endTs": int(end_ts),
            "fidelity": int(fidelity),
            "market": str(market),
            "startTs": int(start_ts),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"clob:range:{digest}"


def resolve_clob_timezone(
    *,
    family: dict[str, Any],
    cell: ExpectedCell,
    stations: tuple[Station, ...] | None = None,
) -> str:
    named = str(family.get("timezone_name") or "").strip()
    if named:
        return named
    catalog = stations if stations is not None else catalog_stations()
    try:
        matched = stations_for_city(cell.city, catalog)
    except ValueError:
        return "UTC"
    station = next((row for row in matched if row.station_id == cell.station), None)
    if station is not None:
        return station.timezone_name
    return matched[0].timezone_name if matched else "UTC"


def plan_clob_gets(
    expected: tuple[ExpectedCell, ...] | list[ExpectedCell],
    families: list[dict[str, Any]],
    *,
    stations: tuple[Station, ...] | None = None,
) -> tuple[list[PlannedGet], dict[str, list[dict[str, Any]]]]:
    family_index = {row["event_family_id"]: row for row in families}
    # Shared-token ownership is fail-closed: if a token appears in multiple
    # families' yes_token_ids, the planner must not map it positionally.
    token_owners: dict[str, set[str]] = {}
    for family in families:
        family_id = str(family.get("event_family_id") or "")
        for token in family.get("yes_token_ids") or ():
            tok = str(token)
            token_owners.setdefault(tok, set()).add(family_id)
    canonical_token_by_family_id: dict[str, str | None] = {}
    for family_id, family in family_index.items():
        tokens = [str(token) for token in (family.get("yes_token_ids") or [])]
        owned = [tok for tok in tokens if len(token_owners.get(tok) or set()) == 1]
        # Deterministic selection without positional assumption.
        canonical_token_by_family_id[str(family_id)] = min(owned, key=str) if owned else None

    catalog = stations if stations is not None else catalog_stations()
    plans: dict[str, PlannedGet] = {}
    mapping: dict[str, list[dict[str, Any]]] = {}
    for cell in expected:
        maybe_family = family_index.get(cell.event_family_id)
        if maybe_family is None:
            continue
        family = maybe_family
        canonical_token = canonical_token_by_family_id.get(str(cell.event_family_id))
        if not canonical_token:
            continue
        timezone_name = resolve_clob_timezone(family=family, cell=cell, stations=catalog)
        start_ts, end_ts = clob_window_timestamps(cell.date, timezone_name)
        market = canonical_token
        params = clob_range_params(market, start_ts, end_ts)
        identity = canonical_clob_identity(
            market=market, start_ts=start_ts, end_ts=end_ts, fidelity=CLOB_FIDELITY_MINUTES
        )
        if identity not in plans:
            plans[identity] = PlannedGet(
                identity=identity,
                provider=PRICE_PROVIDER,
                endpoint=CLOB_ENDPOINT,
                day=cell.date,
                params=params,
            )
        mapping.setdefault(identity, []).append(cell.as_dict())
    return list(plans.values()), mapping
