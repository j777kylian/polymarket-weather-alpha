"""Order-book validation and offline hypothetical ask VWAP (paper only).

Never submits an order. Fee remains null/unknown unless externally sourced.
Observed book facts stay separate from paper assumptions.

One-sided books (ASK_ONLY / BID_ONLY) are valid market-state observations with
provenance; they are never treated as executable two-sided snapshots.
Empty, crossed, and malformed books fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any, Literal

from weather_alpha.models.timeutil import parse_timestamp
from weather_alpha.phase35.config import Phase35ForwardConfig
from weather_alpha.phase35.contracts import BookLevel

BookValidationStatus = Literal["ok", "invalid"]


class ForwardLiquidityState(StrEnum):
    """Typed forward book liquidity / validity classification."""

    TWO_SIDED = "TWO_SIDED"
    ASK_ONLY = "ASK_ONLY"
    BID_ONLY = "BID_ONLY"
    EMPTY = "EMPTY"
    CROSSED_INVALID = "CROSSED_INVALID"
    MALFORMED = "MALFORMED"


@dataclass(frozen=True, slots=True)
class ValidatedOrderBook:
    status: BookValidationStatus
    liquidity_state: ForwardLiquidityState
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    best_bid: float | None
    best_ask: float | None
    midpoint: float | None
    spread: float | None
    orderbook_ts: datetime | None
    reasons: tuple[str, ...]
    asset_id: str | None = None
    market: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "asks": [{"price": level.price, "size": level.size} for level in self.asks],
            "asset_id": self.asset_id,
            "best_ask": self.best_ask,
            "best_bid": self.best_bid,
            "bids": [{"price": level.price, "size": level.size} for level in self.bids],
            "liquidity_state": self.liquidity_state.value,
            "market": self.market,
            "midpoint": self.midpoint,
            "orderbook_ts": None if self.orderbook_ts is None else self.orderbook_ts.isoformat(),
            "reasons": list(self.reasons),
            "spread": self.spread,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class HypotheticalAskFill:
    """Paper-only offline calculation. Not an order, fill, or trading signal."""

    size: float
    available_ask_depth: float
    vwap_entry: float | None
    spread_cost: float | None
    insufficient_depth: bool
    fee_rate: float | None
    fee_status: str
    assumption_note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "assumption_note": self.assumption_note,
            "available_ask_depth": self.available_ask_depth,
            "fee_rate": self.fee_rate,
            "fee_status": self.fee_status,
            "insufficient_depth": self.insufficient_depth,
            "observed_vs_assumed": {
                "observed": "raw ask ladder / best bid / best ask / spread",
                "paper_assumed": "VWAP walk of asks for fixed hypothetical size",
            },
            "size": self.size,
            "spread_cost": self.spread_cost,
            "vwap_entry": self.vwap_entry,
        }


def _parse_levels(raw: object, *, side: str) -> tuple[list[BookLevel], list[str]]:
    reasons: list[str] = []
    if not isinstance(raw, list):
        return [], [f"{side}_not_list"]
    levels: list[BookLevel] = []
    for item in raw:
        if not isinstance(item, dict):
            reasons.append(f"{side}_malformed_level")
            continue
        raw_price = item.get("price")
        raw_size = item.get("size")
        if raw_price is None or raw_size is None:
            reasons.append(f"{side}_non_numeric_level")
            continue
        try:
            price = float(raw_price)
            size = float(raw_size)
        except (TypeError, ValueError):
            reasons.append(f"{side}_non_numeric_level")
            continue
        if not isfinite(price) or not isfinite(size) or price <= 0 or size <= 0:
            reasons.append(f"{side}_non_positive_or_nonfinite")
            continue
        levels.append(BookLevel(price=price, size=size))
    return levels, reasons


def _invalid_book(
    *,
    liquidity_state: ForwardLiquidityState,
    reasons: tuple[str, ...],
    orderbook_ts: datetime | None,
    asset_id: str | None,
    market: str | None,
) -> ValidatedOrderBook:
    return ValidatedOrderBook(
        status="invalid",
        liquidity_state=liquidity_state,
        bids=(),
        asks=(),
        best_bid=None,
        best_ask=None,
        midpoint=None,
        spread=None,
        orderbook_ts=orderbook_ts,
        reasons=reasons,
        asset_id=asset_id,
        market=market,
    )


def is_executable_two_sided_book(book: ValidatedOrderBook) -> bool:
    """True only for accepted two-sided books with bid < ask and positive depth."""
    if book.status != "ok":
        return False
    if book.liquidity_state is not ForwardLiquidityState.TWO_SIDED:
        return False
    if book.best_bid is None or book.best_ask is None:
        return False
    if book.best_bid >= book.best_ask:
        return False
    if book.spread is None or book.spread < 0:
        return False
    return bool(book.bids and book.asks)


def validate_order_book(payload: object) -> ValidatedOrderBook:
    if not isinstance(payload, dict):
        return _invalid_book(
            liquidity_state=ForwardLiquidityState.MALFORMED,
            reasons=("payload_not_object",),
            orderbook_ts=None,
            asset_id=None,
            market=None,
        )
    bid_levels, bid_reasons = _parse_levels(payload.get("bids"), side="bids")
    ask_levels, ask_reasons = _parse_levels(payload.get("asks"), side="asks")
    parse_reasons = [*bid_reasons, *ask_reasons]

    orderbook_ts = None
    raw_ts = payload.get("timestamp")
    if raw_ts is not None and raw_ts != "":
        try:
            orderbook_ts = parse_timestamp(raw_ts)
        except (TypeError, ValueError):
            parse_reasons.append("orderbook_timestamp_invalid")

    asset_raw = payload.get("asset_id") or payload.get("assetId")
    market_raw = payload.get("market")
    asset_id = str(asset_raw) if asset_raw is not None else None
    market = str(market_raw) if market_raw is not None else None

    if parse_reasons:
        return _invalid_book(
            liquidity_state=ForwardLiquidityState.MALFORMED,
            reasons=tuple(dict.fromkeys(parse_reasons)),
            orderbook_ts=orderbook_ts,
            asset_id=asset_id,
            market=market,
        )

    # Bids descending (best first), asks ascending (best first).
    bids = tuple(sorted(bid_levels, key=lambda level: level.price, reverse=True))
    asks = tuple(sorted(ask_levels, key=lambda level: level.price))
    best_bid = bids[0].price if bids else None
    best_ask = asks[0].price if asks else None

    if not bids and not asks:
        return _invalid_book(
            liquidity_state=ForwardLiquidityState.EMPTY,
            reasons=("bids_empty", "asks_empty"),
            orderbook_ts=orderbook_ts,
            asset_id=asset_id,
            market=market,
        )

    if best_bid is not None and best_ask is not None and best_bid > best_ask:
        return _invalid_book(
            liquidity_state=ForwardLiquidityState.CROSSED_INVALID,
            reasons=("crossed_book",),
            orderbook_ts=orderbook_ts,
            asset_id=asset_id,
            market=market,
        )

    if bids and not asks:
        return ValidatedOrderBook(
            status="ok",
            liquidity_state=ForwardLiquidityState.BID_ONLY,
            bids=bids,
            asks=(),
            best_bid=best_bid,
            best_ask=None,
            midpoint=None,
            spread=None,
            orderbook_ts=orderbook_ts,
            reasons=(),
            asset_id=asset_id,
            market=market,
        )

    if asks and not bids:
        return ValidatedOrderBook(
            status="ok",
            liquidity_state=ForwardLiquidityState.ASK_ONLY,
            bids=(),
            asks=asks,
            best_bid=None,
            best_ask=best_ask,
            midpoint=None,
            spread=None,
            orderbook_ts=orderbook_ts,
            reasons=(),
            asset_id=asset_id,
            market=market,
        )

    assert best_bid is not None and best_ask is not None
    spread = best_ask - best_bid
    midpoint = (best_bid + best_ask) / 2.0
    return ValidatedOrderBook(
        status="ok",
        liquidity_state=ForwardLiquidityState.TWO_SIDED,
        bids=bids,
        asks=asks,
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint=midpoint,
        spread=spread,
        orderbook_ts=orderbook_ts,
        reasons=(),
        asset_id=asset_id,
        market=market,
    )


def hypothetical_ask_vwap(
    book: ValidatedOrderBook,
    *,
    size: float,
    config: Phase35ForwardConfig | None = None,
) -> HypotheticalAskFill:
    cfg = config or Phase35ForwardConfig()
    if size <= 0:
        raise ValueError("hypothetical size must be positive")
    if book.status != "ok" or not is_executable_two_sided_book(book):
        ask_depth = 0.0
        if book.status == "ok":
            ask_depth = sum(level.size for level in book.asks)
        return HypotheticalAskFill(
            size=size,
            available_ask_depth=ask_depth,
            vwap_entry=None,
            spread_cost=None,
            insufficient_depth=True,
            fee_rate=cfg.fee_rate,
            fee_status=cfg.fee_status,
            assumption_note=(
                "non-executable or invalid book; no paper VWAP computed "
                "(one-sided / empty / crossed / malformed are not executable)"
            ),
        )
    depth = sum(level.size for level in book.asks)
    remaining = size
    notional = 0.0
    filled = 0.0
    for level in book.asks:
        take = min(remaining, level.size)
        notional += take * level.price
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    insufficient = remaining > 1e-12
    vwap = (notional / filled) if filled > 0 else None
    spread_cost = None
    if vwap is not None and book.best_bid is not None:
        spread_cost = vwap - book.best_bid
    return HypotheticalAskFill(
        size=size,
        available_ask_depth=depth,
        vwap_entry=vwap if not insufficient else None,
        spread_cost=None if insufficient else spread_cost,
        insufficient_depth=insufficient,
        fee_rate=cfg.fee_rate,
        fee_status=cfg.fee_status,
        assumption_note=(
            "paper-only offline VWAP over ask levels; fee unknown unless externally sourced; "
            "never submits an order"
        ),
    )


def hypothetical_fills_for_config(
    book: ValidatedOrderBook,
    *,
    config: Phase35ForwardConfig | None = None,
) -> tuple[HypotheticalAskFill, ...]:
    cfg = config or Phase35ForwardConfig()
    return tuple(
        hypothetical_ask_vwap(book, size=size, config=cfg) for size in cfg.hypothetical_sizes
    )
