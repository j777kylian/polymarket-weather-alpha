"""Forward GET-only public CLOB order-book collector for Phase 3.5 readiness.

Uses ReadOnlyHttpClient only. Never submits orders. Persists immutable raw
payloads under data/phase35/forward/. Offline VWAP is computed separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from weather_alpha.collectors.polymarket.client import PolymarketReadClient
from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyHttpError
from weather_alpha.models.timeutil import ensure_utc, utc_now
from weather_alpha.phase35.book import is_executable_two_sided_book, validate_order_book
from weather_alpha.phase35.config import Phase35ForwardConfig
from weather_alpha.phase35.contracts import (
    FORWARD_TRACK,
    ForwardExecutableBookSnapshot,
    stable_forward_raw_provenance_path,
    storage_root_for,
)
from weather_alpha.storage.raw import persist_raw_payload


@dataclass(frozen=True, slots=True)
class ForwardCollectTarget:
    canonical_event_id: str
    condition_id: str
    market_id: str | None
    token_id: str
    city: str | None
    station_icao: str | None
    event_date: str
    native_unit: str | None
    bucket_definition: str | None
    decision_ts: datetime
    checkpoint_lead_hours: int
    provider: str = "open-meteo-single-run"
    model: str | None = "ecmwf_ifs"
    forecast_issued_at: datetime | None = None
    forecast_available_at: datetime | None = None
    model_probability: float | None = None
    descriptive_market_probability: float | None = None
    expected_settlement_label: str | None = None


@dataclass(frozen=True, slots=True)
class ForwardCollectResult:
    snapshot: ForwardExecutableBookSnapshot | None
    quarantined: bool
    reasons: tuple[str, ...]
    raw_path: str | None = None
    liquidity_state: str | None = None
    executable_two_sided: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "executable_two_sided": self.executable_two_sided,
            "liquidity_state": self.liquidity_state,
            "quarantined": self.quarantined,
            "raw_path": (
                None if self.raw_path is None else stable_forward_raw_provenance_path(self.raw_path)
            ),
            "reasons": list(self.reasons),
            "snapshot": None if self.snapshot is None else self.snapshot.as_dict(),
        }


class ForwardBookCollector:
    """Bounded forward book snapshot collector. GET /book only."""

    def __init__(
        self,
        http: ReadOnlyHttpClient,
        *,
        output_root: Path | None = None,
        config: Phase35ForwardConfig | None = None,
        retrieved_at: datetime | None = None,
    ) -> None:
        self._client = PolymarketReadClient(http)
        self._output_root = output_root or storage_root_for(FORWARD_TRACK)
        self._config = config or Phase35ForwardConfig()
        self._retrieved_at = retrieved_at

    def collect_one(self, target: ForwardCollectTarget) -> ForwardCollectResult:
        reasons: list[str] = []
        if not target.canonical_event_id or not target.condition_id or not target.token_id:
            reasons.append("event_identity_incomplete")
        if target.checkpoint_lead_hours <= 0:
            reasons.append("invalid_checkpoint")
        decision = ensure_utc(target.decision_ts)
        if (
            target.forecast_available_at is not None
            and ensure_utc(target.forecast_available_at) > decision
        ):
            reasons.append("forecast_available_after_decision")
        if reasons:
            return ForwardCollectResult(snapshot=None, quarantined=True, reasons=tuple(reasons))

        response = self._client.current_book(target.token_id)
        retrieval = self._now()
        try:
            response.raise_for_status()
            payload = response.json()
        except (ReadOnlyHttpError, ValueError, TypeError) as exc:
            return ForwardCollectResult(
                snapshot=None,
                quarantined=True,
                reasons=(f"book_http_or_json_error:{exc}",),
            )

        raw_root = self._output_root / "raw"
        persisted = persist_raw_payload(
            raw_root,
            source="polymarket/clob-book",
            url=response.url,
            payload=payload,
            retrieved_at=retrieval,
        )
        book = validate_order_book(payload)
        if book.status != "ok":
            return ForwardCollectResult(
                snapshot=None,
                quarantined=True,
                reasons=book.reasons,
                raw_path=persisted.raw_path,
                liquidity_state=book.liquidity_state.value,
                executable_two_sided=False,
            )
        if book.asset_id is not None and book.asset_id != target.token_id:
            return ForwardCollectResult(
                snapshot=None,
                quarantined=True,
                reasons=("event_identity_mismatch:asset_id",),
                raw_path=persisted.raw_path,
                liquidity_state=book.liquidity_state.value,
                executable_two_sided=False,
            )
        if book.market is not None and book.market != target.condition_id:
            return ForwardCollectResult(
                snapshot=None,
                quarantined=True,
                reasons=("event_identity_mismatch:condition_id",),
                raw_path=persisted.raw_path,
                liquidity_state=book.liquidity_state.value,
                executable_two_sided=False,
            )

        executable = is_executable_two_sided_book(book)
        snapshot = ForwardExecutableBookSnapshot(
            track=FORWARD_TRACK,
            canonical_event_id=target.canonical_event_id,
            condition_id=target.condition_id,
            market_id=target.market_id,
            token_id=target.token_id,
            city=target.city,
            station_icao=target.station_icao,
            event_date=target.event_date,
            native_unit=target.native_unit,
            bucket_definition=target.bucket_definition,
            decision_ts=decision,
            retrieval_ts=retrieval,
            orderbook_ts=book.orderbook_ts,
            checkpoint_lead_hours=target.checkpoint_lead_hours,
            provider=target.provider,
            model=target.model,
            forecast_issued_at=target.forecast_issued_at,
            forecast_available_at=target.forecast_available_at,
            model_probability=target.model_probability,
            descriptive_market_probability=target.descriptive_market_probability,
            best_bid=book.best_bid,
            best_ask=book.best_ask,
            midpoint=book.midpoint,
            spread=book.spread,
            bids=book.bids,
            asks=book.asks,
            liquidity_state=book.liquidity_state.value,
            raw_payload=payload if isinstance(payload, dict) else {"payload": payload},
            provenance_url=persisted.request_url,
            raw_path=persisted.raw_path,
            content_sha256=persisted.content_sha256,
            fee_rate=self._config.fee_rate,
            fee_status=self._config.fee_status,
            settlement_label=None,
            settlement_retrieved_at=None,
        )
        return ForwardCollectResult(
            snapshot=snapshot,
            quarantined=False,
            reasons=(),
            raw_path=persisted.raw_path,
            liquidity_state=book.liquidity_state.value,
            executable_two_sided=executable,
        )

    def append_settlement(
        self,
        snapshot: ForwardExecutableBookSnapshot,
        *,
        settlement_label: str,
        retrieved_at: datetime | None = None,
        expected_label: str | None = None,
    ) -> ForwardExecutableBookSnapshot:
        """Link settlement without overwriting decision-time book fields."""
        if expected_label is not None and expected_label != settlement_label:
            raise ValueError(
                f"settlement mismatch: expected {expected_label!r} got {settlement_label!r}"
            )
        return ForwardExecutableBookSnapshot(
            track=snapshot.track,
            canonical_event_id=snapshot.canonical_event_id,
            condition_id=snapshot.condition_id,
            market_id=snapshot.market_id,
            token_id=snapshot.token_id,
            city=snapshot.city,
            station_icao=snapshot.station_icao,
            event_date=snapshot.event_date,
            native_unit=snapshot.native_unit,
            bucket_definition=snapshot.bucket_definition,
            decision_ts=snapshot.decision_ts,
            retrieval_ts=snapshot.retrieval_ts,
            orderbook_ts=snapshot.orderbook_ts,
            checkpoint_lead_hours=snapshot.checkpoint_lead_hours,
            provider=snapshot.provider,
            model=snapshot.model,
            forecast_issued_at=snapshot.forecast_issued_at,
            forecast_available_at=snapshot.forecast_available_at,
            model_probability=snapshot.model_probability,
            descriptive_market_probability=snapshot.descriptive_market_probability,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            midpoint=snapshot.midpoint,
            spread=snapshot.spread,
            bids=snapshot.bids,
            asks=snapshot.asks,
            liquidity_state=snapshot.liquidity_state,
            raw_payload=snapshot.raw_payload,
            provenance_url=snapshot.provenance_url,
            raw_path=snapshot.raw_path,
            content_sha256=snapshot.content_sha256,
            fee_rate=snapshot.fee_rate,
            fee_status=snapshot.fee_status,
            settlement_label=settlement_label,
            settlement_retrieved_at=ensure_utc(retrieved_at or self._now()),
        )

    def _now(self) -> datetime:
        return self._retrieved_at or utc_now()
