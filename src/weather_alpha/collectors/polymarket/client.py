"""GET-only Polymarket public API client.

Official public endpoints used:
- Gamma GET /events, GET /markets, GET /public-search
- CLOB GET /prices-history, GET /book (current book only)
- Data API GET /trades

Official public APIs do not provide arbitrary historical order-book
reconstruction. Order-book history in this project begins when snapshots
are recorded by the collector.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyResponse

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"

# Cloudflare IP limits (docs.polymarket.com/api-reference/rate-limits).
# Defaults stay well below documented ceilings.
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 5


class PolymarketReadClient:
    def __init__(self, http: ReadOnlyHttpClient) -> None:
        self._http = http

    def list_events(
        self,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        closed: bool | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> ReadOnlyResponse:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if closed is not None:
            params["closed"] = str(closed).lower()
        if extra:
            params.update(dict(extra))
        return self._http.get(f"{GAMMA_BASE}/events", params=params)

    def list_markets(
        self,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        closed: bool | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> ReadOnlyResponse:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if closed is not None:
            params["closed"] = str(closed).lower()
        if extra:
            params.update(dict(extra))
        return self._http.get(f"{GAMMA_BASE}/markets", params=params)

    def public_search(
        self,
        query: str,
        *,
        page: int = 1,
        limit_per_type: int = 20,
        keep_closed_markets: int = 1,
    ) -> ReadOnlyResponse:
        return self._http.get(
            f"{GAMMA_BASE}/public-search",
            params={
                "q": query,
                "page": page,
                "limit_per_type": limit_per_type,
                "keep_closed_markets": keep_closed_markets,
            },
        )

    def price_history(
        self,
        token_id: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
        interval: str | None = None,
        fidelity: int | None = 60,
    ) -> ReadOnlyResponse:
        params: dict[str, Any] = {"market": token_id}
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts
        if interval is not None:
            params["interval"] = interval
        if fidelity is not None:
            params["fidelity"] = fidelity
        return self._http.get(f"{CLOB_BASE}/prices-history", params=params)

    def current_book(self, token_id: str) -> ReadOnlyResponse:
        return self._http.get(f"{CLOB_BASE}/book", params={"token_id": token_id})

    def trades(
        self,
        *,
        market: str | None = None,
        limit: int = 100,
        offset: int = 0,
        start: int | None = None,
        end: int | None = None,
        taker_only: bool = False,
    ) -> ReadOnlyResponse:
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "takerOnly": str(taker_only).lower(),
        }
        if market is not None:
            params["market"] = market
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        return self._http.get(f"{DATA_BASE}/trades", params=params)
