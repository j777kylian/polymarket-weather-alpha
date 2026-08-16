"""Settlement labels from resolved Gamma outcomePrices.

Settlement is a training/evaluation label only. It must never be used as a
feature at decision time. Labels are accepted only when the market is closed
(or resolved) and outcomePrices are an unambiguous binary 1/0 pair.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SettlementParseResult:
    label: str | None
    quarantine_reason: str | None


def parse_settlement_label(
    *,
    closed: bool | None,
    resolved: bool | None = None,
    outcomes: Any,
    outcome_prices: Any,
) -> SettlementParseResult:
    if not (closed is True or resolved is True):
        return SettlementParseResult(
            label=None,
            quarantine_reason="market not closed/resolved; settlement label unavailable",
        )
    labels = _as_str_list(outcomes)
    prices = _as_float_list(outcome_prices)
    if len(labels) < 2 or len(prices) < 2:
        return SettlementParseResult(
            label=None,
            quarantine_reason="outcomes/outcomePrices missing or too short for binary settlement",
        )
    if len(labels) != len(prices):
        return SettlementParseResult(
            label=None,
            quarantine_reason="outcomes and outcomePrices length mismatch",
        )
    winners = [index for index, price in enumerate(prices) if _is_one(price)]
    losers = [index for index, price in enumerate(prices) if _is_zero(price)]
    if len(winners) != 1 or len(losers) != len(prices) - 1:
        return SettlementParseResult(
            label=None,
            quarantine_reason="outcomePrices not unambiguous binary 1/0 settlement",
        )
    return SettlementParseResult(label=labels[winners[0]], quarantine_reason=None)


def _as_str_list(value: Any) -> list[str]:
    items = _json_list(value)
    return [str(item) for item in items]


def _as_float_list(value: Any) -> list[float]:
    items = _json_list(value)
    out: list[float] = []
    for item in items:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            return []
    return out


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _is_one(value: float) -> bool:
    return abs(value - 1.0) <= 1e-9


def _is_zero(value: float) -> bool:
    return abs(value) <= 1e-9
