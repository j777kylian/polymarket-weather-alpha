"""Polymarket research collector (GET-only, no trading)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from weather_alpha.collectors.pagination import paginate_offset, paginate_pages, unique_by
from weather_alpha.collectors.polymarket.client import (
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_SIZE,
    PolymarketReadClient,
)
from weather_alpha.collectors.polymarket.parser import (
    TARGET_CITIES,
    ParsedGammaMarket,
    is_temperature_market_text,
    parse_gamma_market,
    validate_cities,
)
from weather_alpha.collectors.polymarket.trades import public_trade_id
from weather_alpha.config.settings import (
    DEFAULT_MAX_DETAIL_MARKETS,
    bounded_max_detail_markets,
    bounded_max_pages,
    bounded_page_size,
)
from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyResponse
from weather_alpha.models.records import (
    OrderBookLevel,
    OrderBookSnapshot,
    PriceSnapshot,
    Provenance,
    TradeRecord,
)
from weather_alpha.models.timeutil import parse_timestamp, utc_now
from weather_alpha.storage.raw import PersistedPayload, persist_raw_payload
from weather_alpha.storage.repository import WeatherAlphaRepository

SEARCH_QUERIES = tuple(f"highest temperature in {city}" for city in sorted(TARGET_CITIES))


@dataclass(frozen=True, slots=True)
class PolymarketCollectOptions:
    max_pages: int = DEFAULT_MAX_PAGES
    page_size: int = DEFAULT_PAGE_SIZE
    start_date: str | None = None
    end_date: str | None = None
    collect_prices: bool = True
    collect_trades: bool = True
    collect_current_books: bool = True
    max_detail_markets: int = DEFAULT_MAX_DETAIL_MARKETS
    dry_run: bool = False
    cities: tuple[str, ...] = tuple(sorted(TARGET_CITIES))


@dataclass
class CollectReport:
    dry_run: bool
    intended_queries: tuple[str, ...]
    intended_pages: int
    markets_seen: int = 0
    markets_stored: int = 0
    prices_stored: int = 0
    trades_stored: int = 0
    books_stored: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "intended_queries": list(self.intended_queries),
            "intended_pages": self.intended_pages,
            "markets_seen": self.markets_seen,
            "markets_stored": self.markets_stored,
            "prices_stored": self.prices_stored,
            "trades_stored": self.trades_stored,
            "books_stored": self.books_stored,
            "notes": list(self.notes),
        }


class PolymarketCollector:
    def __init__(
        self,
        *,
        http: ReadOnlyHttpClient,
        repository: WeatherAlphaRepository | None,
        raw_root: Path,
    ) -> None:
        self._client = PolymarketReadClient(http)
        self._repo = repository
        self._raw_root = raw_root

    def collect(self, options: PolymarketCollectOptions) -> CollectReport:
        options = _validated_options(options)
        queries = tuple(f"highest temperature in {city}" for city in options.cities)
        report = CollectReport(
            dry_run=options.dry_run,
            intended_queries=queries,
            intended_pages=options.max_pages,
            notes=[
                "Official public CLOB APIs expose the current order book only.",
                "Order-book history begins when this collector records snapshots.",
            ],
        )
        if options.dry_run:
            report.notes.append("dry-run: no HTTP requests and no writes")
            return report
        if self._repo is None:
            raise RuntimeError("repository is required unless dry-run")

        parsed_markets = self._discover_markets(options, report)
        for parsed in parsed_markets:
            self._repo.upsert_market(parsed.market)
            for outcome in parsed.outcomes:
                self._repo.upsert_outcome(outcome)
            report.markets_stored += 1

        detail_targets = [
            parsed
            for parsed in parsed_markets
            if parsed.market.parse_status in {"resolved", "partial"}
        ][: options.max_detail_markets]

        if options.collect_prices:
            report.prices_stored = self._collect_prices(detail_targets, options)
        if options.collect_trades:
            report.trades_stored = self._collect_trades(detail_targets, options, report)
        if options.collect_current_books:
            report.books_stored = self._collect_books(detail_targets, report)
        return report

    def _discover_markets(
        self, options: PolymarketCollectOptions, report: CollectReport
    ) -> list[ParsedGammaMarket]:
        found: list[ParsedGammaMarket] = []
        for query in report.intended_queries:

            def fetch_search_page(page: int, search_query: str = query) -> list[ParsedGammaMarket]:
                response = self._client.public_search(
                    search_query, page=page, limit_per_type=options.page_size
                )
                persisted = self._persist_response(
                    response, source="polymarket/gamma-search", limitations=()
                )
                return [
                    self._parse_and_note(raw, persisted, report)
                    for raw in _markets_from_search(persisted.payload)
                ]

            found.extend(
                paginate_pages(
                    fetch_search_page,
                    max_pages=options.max_pages,
                    key=lambda item: item.market.condition_id,
                )
            )

        for closed_flag in (False, True):

            def fetch_markets(
                offset: int,
                limit: int,
                closed: bool = closed_flag,
            ) -> Sequence[tuple[dict[str, Any], PersistedPayload]]:
                extra: dict[str, Any] = {"closed": str(closed).lower()}
                if options.start_date:
                    extra["end_date_min"] = f"{options.start_date}T00:00:00Z"
                if options.end_date:
                    extra["end_date_max"] = f"{options.end_date}T23:59:59Z"
                response = self._client.list_markets(limit=limit, offset=offset, extra=extra)
                persisted = self._persist_response(
                    response, source="polymarket/gamma-markets", limitations=()
                )
                payload = persisted.payload
                if not isinstance(payload, list):
                    return []
                return [(row, persisted) for row in payload if isinstance(row, dict)]

            for raw, persisted in paginate_offset(
                fetch_markets, page_size=options.page_size, max_pages=options.max_pages
            ):
                question = str(raw.get("question") or "")
                slug = str(raw.get("slug") or "")
                if not is_temperature_market_text(question, slug):
                    continue
                found.append(self._parse_and_note(raw, persisted, report))

        unique = unique_by(found, key=lambda item: item.market.condition_id)
        report.markets_seen = len(unique)
        return unique

    def _parse_and_note(
        self, raw: dict[str, Any], persisted: PersistedPayload, report: CollectReport
    ) -> ParsedGammaMarket:
        parsed = parse_gamma_market(
            raw,
            retrieved_url=persisted.request_url,
            retrieved_at=persisted.retrieved_at,
            raw_path=persisted.raw_path,
            content_sha256=persisted.content_sha256,
        )
        if parsed.market.parse_status == "unresolved":
            report.notes.append(f"unresolved question retained: {parsed.market.question}")
        return parsed

    def _collect_prices(
        self, markets: list[ParsedGammaMarket], options: PolymarketCollectOptions
    ) -> int:
        stored = 0
        start_ts = _date_to_ts(options.start_date, end=False)
        end_ts = _date_to_ts(options.end_date, end=True)
        for parsed in markets:
            for outcome in parsed.outcomes:
                response = self._client.price_history(
                    outcome.token_id, start_ts=start_ts, end_ts=end_ts, fidelity=60
                )
                persisted = self._persist_response(
                    response,
                    source="polymarket/clob-prices-history",
                    limitations=("price history is not a full order book",),
                )
                payload = persisted.payload
                points = payload.get("history", payload) if isinstance(payload, dict) else payload
                if not isinstance(points, list):
                    continue
                provenance = _provenance_from_persisted(
                    persisted,
                    source="clob.polymarket.com",
                    limitations=("CLOB prices-history; not executable without spread/fees",),
                )
                for point in points:
                    if not isinstance(point, dict):
                        continue
                    ts = point.get("t")
                    price = point.get("p")
                    if ts is None or price is None:
                        continue
                    snapshot = PriceSnapshot(
                        token_id=outcome.token_id,
                        observed_at=parse_timestamp(ts),
                        price=float(price),
                        provenance=provenance,
                        condition_id=parsed.market.condition_id,
                    )
                    assert self._repo is not None
                    self._repo.upsert_price_snapshot(snapshot)
                    stored += 1
        return stored

    def _collect_trades(
        self,
        markets: list[ParsedGammaMarket],
        options: PolymarketCollectOptions,
        report: CollectReport,
    ) -> int:
        stored = 0
        start = _date_to_ts(options.start_date, end=False)
        end = _date_to_ts(options.end_date, end=True)

        def fetch(
            offset: int, limit: int, condition_id: str
        ) -> Sequence[tuple[dict[str, Any], PersistedPayload]]:
            response = self._client.trades(
                market=condition_id, limit=limit, offset=offset, start=start, end=end
            )
            persisted = self._persist_response(
                response,
                source="polymarket/data-trades",
                limitations=("public trades tape; taker/maker identity not required",),
            )
            payload = persisted.payload
            if not isinstance(payload, list):
                return []
            return [(row, persisted) for row in payload if isinstance(row, dict)]

        for parsed in markets:
            condition_id = parsed.market.condition_id
            if not condition_id.startswith("0x"):
                continue

            def page_fetch(
                offset: int, limit: int, cid: str = condition_id
            ) -> Sequence[tuple[dict[str, Any], PersistedPayload]]:
                return fetch(offset, limit, cid)

            pairs = unique_by(
                paginate_offset(
                    page_fetch, page_size=min(100, options.page_size), max_pages=options.max_pages
                ),
                key=lambda pair: public_trade_id(pair[0]),
            )
            for row, persisted in pairs:
                traded_at = row.get("timestamp")
                if traded_at is None:
                    report.notes.append("skipped trade: missing timestamp")
                    continue
                token_id = _required_token_id(row.get("asset"))
                price = _required_finite_float(row.get("price"))
                size = _required_finite_float(row.get("size"))
                if token_id is None or price is None or size is None:
                    missing: list[str] = []
                    if token_id is None:
                        missing.append("asset")
                    if price is None:
                        missing.append("price")
                    if size is None:
                        missing.append("size")
                    report.notes.append(
                        "skipped trade: missing or invalid required field(s) " + ", ".join(missing)
                    )
                    continue
                record = TradeRecord(
                    trade_id=public_trade_id(row),
                    token_id=token_id,
                    side=str(row.get("side") or "UNKNOWN"),
                    price=price,
                    size=size,
                    traded_at=parse_timestamp(traded_at),
                    provenance=_provenance_from_persisted(
                        persisted,
                        source="data-api.polymarket.com",
                        limitations=("public trades tape; taker/maker identity not required",),
                    ),
                    transaction_hash=_opt(row.get("transactionHash")),
                    condition_id=str(row.get("conditionId") or condition_id),
                    outcome_label=_opt(row.get("outcome")),
                )
                assert self._repo is not None
                self._repo.upsert_trade(record)
                stored += 1
        return stored

    def _collect_books(self, markets: list[ParsedGammaMarket], report: CollectReport) -> int:
        stored = 0
        for parsed in markets:
            for outcome in parsed.outcomes:
                response = self._client.current_book(outcome.token_id)
                persisted = self._persist_response(
                    response,
                    source="polymarket/clob-book",
                    limitations=(
                        "current CLOB book only; official APIs do not reconstruct history",
                    ),
                )
                payload = persisted.payload
                if not isinstance(payload, dict):
                    continue
                token_id = _required_token_id(payload.get("asset_id")) or _required_token_id(
                    outcome.token_id
                )
                if token_id is None:
                    report.notes.append("skipped order book: missing token id")
                    continue
                observed = payload.get("timestamp")
                if observed in (None, ""):
                    report.notes.append("skipped order book: missing timestamp")
                    continue
                levels: list[OrderBookLevel] = []
                for side in ("bids", "asks"):
                    entries = payload.get(side) or []
                    if not isinstance(entries, list):
                        continue
                    for index, entry in enumerate(entries):
                        if not isinstance(entry, dict):
                            continue
                        price = _required_finite_float(entry.get("price"))
                        size = _required_finite_float(entry.get("size"))
                        if price is None or size is None:
                            report.notes.append(
                                f"skipped order-book {side} level {index}: missing or invalid price/size"
                            )
                            continue
                        levels.append(
                            OrderBookLevel(
                                side="bid" if side == "bids" else "ask",
                                price=price,
                                size=size,
                                level_index=index,
                            )
                        )
                snapshot = OrderBookSnapshot(
                    snapshot_id=f"{token_id}:{observed}",
                    token_id=token_id,
                    observed_at=parse_timestamp(observed),
                    provenance=_provenance_from_persisted(
                        persisted,
                        source="clob.polymarket.com",
                        limitations=(
                            "current book snapshot; history starts at first collector run",
                        ),
                    ),
                    condition_id=parsed.market.condition_id,
                    hash=_opt(payload.get("hash")),
                    tick_size=_opt_float(payload.get("tick_size")),
                    min_order_size=_opt_float(payload.get("min_order_size")),
                    last_trade_price=_opt_float(payload.get("last_trade_price")),
                    levels=tuple(levels),
                )
                assert self._repo is not None
                self._repo.upsert_order_book(snapshot)
                stored += 1
        return stored

    def _persist_response(
        self,
        response: ReadOnlyResponse,
        *,
        source: str,
        limitations: tuple[str, ...],
    ) -> PersistedPayload:
        response.raise_for_status()
        try:
            payload: Any = response.json()
        except ValueError:
            payload = {"raw_text": response.text()}
        retrieved_at = utc_now()
        persisted = persist_raw_payload(
            self._raw_root,
            source=source,
            url=response.url,
            payload=payload,
            retrieved_at=retrieved_at,
        )
        assert self._repo is not None
        self._repo.record_raw_payload(
            content_sha256=persisted.content_sha256,
            source=source,
            request_url=persisted.request_url,
            retrieved_at=persisted.retrieved_at,
            disk_path=persisted.raw_path,
            limitations=limitations,
        )
        return persisted


def _validated_options(options: PolymarketCollectOptions) -> PolymarketCollectOptions:
    return replace(
        options,
        max_pages=bounded_max_pages(options.max_pages),
        page_size=bounded_page_size(options.page_size),
        max_detail_markets=bounded_max_detail_markets(options.max_detail_markets),
        cities=validate_cities(options.cities),
    )


def _markets_from_search(payload: Any) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        events = payload.get("events") or []
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue
                nested = event.get("markets") or []
                if not isinstance(nested, list):
                    continue
                event_id = event.get("id")
                event_slug = event.get("slug")
                neg_risk = event.get("negRiskMarketID") or event.get("neg_risk_market_id")
                for row in nested:
                    if not isinstance(row, dict):
                        continue
                    enriched = dict(row)
                    if event_id is not None and enriched.get("eventId") is None:
                        enriched["eventId"] = event_id
                    if event_slug is not None and enriched.get("event_slug") is None:
                        enriched["event_slug"] = event_slug
                    if neg_risk is not None and enriched.get("negRiskMarketID") is None:
                        enriched["negRiskMarketID"] = neg_risk
                    markets.append(enriched)
        direct = payload.get("markets") or []
        if isinstance(direct, list):
            markets.extend(row for row in direct if isinstance(row, dict))
    return unique_by(markets, key=lambda row: str(row.get("conditionId") or row.get("id")))


def _provenance_from_persisted(
    persisted: PersistedPayload, *, source: str, limitations: tuple[str, ...]
) -> Provenance:
    return Provenance(
        source=source,
        retrieved_at=persisted.retrieved_at,
        request_url=persisted.request_url,
        raw_path=persisted.raw_path,
        content_sha256=persisted.content_sha256,
        limitations=limitations,
    )


def _date_to_ts(value: str | None, *, end: bool) -> int | None:
    if not value:
        return None
    stamp = parse_timestamp(
        value if "T" in value else f"{value}T23:59:59Z" if end else f"{value}T00:00:00Z"
    )
    return int(stamp.timestamp())


def _opt(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _required_finite_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _required_token_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
