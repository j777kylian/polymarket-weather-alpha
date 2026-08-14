from __future__ import annotations

import json
from pathlib import Path

from tests.fakes import ForbiddenNetworkTransport, RecordingGetTransport
from weather_alpha.collectors.polymarket.collector import (
    PolymarketCollectOptions,
    PolymarketCollector,
)
from weather_alpha.http.readonly import ReadOnlyHttpClient
from weather_alpha.storage.repository import WeatherAlphaRepository

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_dry_run_has_no_network_or_writes(tmp_path: Path) -> None:
    db = tmp_path / "research.sqlite"
    repo = WeatherAlphaRepository(db)
    http = ReadOnlyHttpClient(transport=ForbiddenNetworkTransport())
    collector = PolymarketCollector(http=http, repository=repo, raw_root=tmp_path / "raw")
    report = collector.collect(PolymarketCollectOptions(dry_run=True, max_pages=2))
    assert report.dry_run is True
    assert "highest temperature in paris" in report.intended_queries
    assert report.markets_stored == 0
    assert not db.exists()
    assert not (tmp_path / "raw").exists()


def test_fixture_collection_stores_markets_prices_trades_and_current_book(tmp_path: Path) -> None:
    market = _load("gamma_market_paris.json")
    book = _load("clob_book.json")
    assert isinstance(book, dict)

    def book_for(params: dict[str, object]) -> dict[str, object]:
        token = str(params.get("token_id") or book.get("asset_id"))
        payload = dict(book)
        payload["asset_id"] = token
        payload["hash"] = f"hash-{token}"
        return payload

    routes = {
        "/public-search": {"events": [{"markets": [market]}], "markets": []},
        "/markets": [],
        "/prices-history": _load("clob_prices_history.json"),
        "/book": book_for,
        "/trades": _load("data_api_trades.json"),
    }
    transport = RecordingGetTransport(routes)
    http = ReadOnlyHttpClient(transport=transport, max_retries=0)
    repo = WeatherAlphaRepository(tmp_path / "research.sqlite")
    repo.init_schema()
    collector = PolymarketCollector(http=http, repository=repo, raw_root=tmp_path / "raw")
    report = collector.collect(
        PolymarketCollectOptions(
            max_pages=1,
            page_size=20,
            cities=("paris",),
            collect_prices=True,
            collect_trades=True,
            collect_current_books=True,
        )
    )
    assert all(method == "GET" for method, _url in transport.calls)
    assert report.markets_stored >= 1
    assert repo.count("markets") == 1
    assert repo.count("price_snapshots") == 4
    assert repo.count("trades") == 1
    assert repo.count("order_book_snapshots") == 2  # yes and no tokens
    assert repo.count("raw_payloads") >= 1
    raw_files = list((tmp_path / "raw").rglob("*.json"))
    assert raw_files
    with repo.connect() as conn:
        for table in ("markets", "outcomes", "price_snapshots", "trades", "order_book_snapshots"):
            row = conn.execute(
                f"SELECT raw_path, content_sha256, request_url, retrieved_at FROM {table}"
            ).fetchone()
            assert row is not None
            assert row["raw_path"]
            assert row["content_sha256"]
            assert row["request_url"]
            assert row["retrieved_at"]
            assert Path(row["raw_path"]).is_file()
        market_ts = conn.execute("SELECT retrieved_at FROM markets").fetchone()["retrieved_at"]
        outcome_ts = conn.execute("SELECT retrieved_at FROM outcomes").fetchone()["retrieved_at"]
        assert market_ts == outcome_ts


def test_public_search_paginates_until_empty_or_max_pages(tmp_path: Path) -> None:
    market_a = _load("gamma_market_paris.json")
    assert isinstance(market_a, dict)
    market_b = dict(market_a)
    market_b["id"] = "124"
    market_b["conditionId"] = "0x" + "cd" * 32
    market_b["clobTokenIds"] = '["token-yes-b", "token-no-b"]'

    def search(params: dict[str, object]) -> dict[str, object]:
        page = int(str(params.get("page") or "1"))
        if page == 1:
            return {"events": [{"markets": [market_a]}], "markets": []}
        if page == 2:
            return {"events": [{"markets": [market_b]}], "markets": []}
        return {"events": [], "markets": []}

    transport = RecordingGetTransport({"/public-search": search, "/markets": []})
    repo = WeatherAlphaRepository(tmp_path / "research.sqlite")
    repo.init_schema()
    collector = PolymarketCollector(
        http=ReadOnlyHttpClient(transport=transport, max_retries=0),
        repository=repo,
        raw_root=tmp_path / "raw",
    )
    report = collector.collect(
        PolymarketCollectOptions(
            max_pages=5,
            page_size=20,
            cities=("paris",),
            collect_prices=False,
            collect_trades=False,
            collect_current_books=False,
        )
    )
    search_calls = [url for method, url in transport.calls if "/public-search" in url]
    assert len(search_calls) == 3  # pages 1, 2, then empty 3
    assert report.markets_stored == 2
    assert repo.count("markets") == 2


def test_public_search_stops_when_page_has_no_new_markets(tmp_path: Path) -> None:
    market = _load("gamma_market_paris.json")

    def search(params: dict[str, object]) -> dict[str, object]:
        del params
        return {"events": [{"markets": [market]}], "markets": []}

    transport = RecordingGetTransport({"/public-search": search, "/markets": []})
    repo = WeatherAlphaRepository(tmp_path / "research.sqlite")
    repo.init_schema()
    collector = PolymarketCollector(
        http=ReadOnlyHttpClient(transport=transport, max_retries=0),
        repository=repo,
        raw_root=tmp_path / "raw",
    )
    collector.collect(
        PolymarketCollectOptions(
            max_pages=8,
            page_size=20,
            cities=("paris",),
            collect_prices=False,
            collect_trades=False,
            collect_current_books=False,
        )
    )
    search_calls = [url for _method, url in transport.calls if "/public-search" in url]
    assert len(search_calls) == 2


def test_duplicate_transaction_hash_with_distinct_fills_both_persist(tmp_path: Path) -> None:
    market = _load("gamma_market_paris.json")
    trades = [
        {
            "transactionHash": "0xabc123",
            "side": "BUY",
            "asset": "token-yes-31c",
            "conditionId": "0xabababababababababababababababababababababababababababababababab",
            "size": 10.0,
            "price": 0.41,
            "timestamp": 1720000000,
            "outcome": "Yes",
            "outcomeIndex": 0,
        },
        {
            "transactionHash": "0xabc123",
            "side": "SELL",
            "asset": "token-no-31c",
            "conditionId": "0xabababababababababababababababababababababababababababababababab",
            "size": 10.0,
            "price": 0.59,
            "timestamp": 1720000000,
            "outcome": "No",
            "outcomeIndex": 1,
        },
        {
            "transactionHash": "0xabc123",
            "side": "BUY",
            "asset": "token-yes-31c",
            "conditionId": "0xabababababababababababababababababababababababababababababababab",
            "size": 10.0,
            "price": 0.41,
            "timestamp": 1720000000,
            "outcome": "Yes",
            "outcomeIndex": 0,
        },
    ]
    transport = RecordingGetTransport(
        {
            "/public-search": {"events": [{"markets": [market]}], "markets": []},
            "/markets": [],
            "/trades": trades,
        }
    )
    repo = WeatherAlphaRepository(tmp_path / "research.sqlite")
    repo.init_schema()
    collector = PolymarketCollector(
        http=ReadOnlyHttpClient(transport=transport, max_retries=0),
        repository=repo,
        raw_root=tmp_path / "raw",
    )
    collector.collect(
        PolymarketCollectOptions(
            max_pages=1,
            page_size=20,
            cities=("paris",),
            collect_prices=False,
            collect_trades=True,
            collect_current_books=False,
        )
    )
    assert repo.count("trades") == 2


def test_http_4xx_does_not_persist_or_parse(tmp_path: Path) -> None:
    from weather_alpha.http.readonly import ReadOnlyHttpError, ReadOnlyResponse

    transport = RecordingGetTransport(
        {
            "/public-search": ReadOnlyResponse(
                status_code=404,
                url="https://gamma-api.polymarket.com/public-search",
                headers={},
                content=b"{}",
            )
        }
    )
    repo = WeatherAlphaRepository(tmp_path / "research.sqlite")
    repo.init_schema()
    collector = PolymarketCollector(
        http=ReadOnlyHttpClient(transport=transport, max_retries=0),
        repository=repo,
        raw_root=tmp_path / "raw",
    )
    try:
        collector.collect(
            PolymarketCollectOptions(
                max_pages=1,
                cities=("paris",),
                collect_prices=False,
                collect_trades=False,
                collect_current_books=False,
            )
        )
    except ReadOnlyHttpError:
        pass
    else:
        raise AssertionError("expected ReadOnlyHttpError for 404")
    assert list((tmp_path / "raw").rglob("*.json")) == []
    assert repo.count("markets") == 0


def test_http_5xx_does_not_persist(tmp_path: Path) -> None:
    from weather_alpha.http.readonly import ReadOnlyResponse, RetryExhaustedError

    transport = RecordingGetTransport(
        {
            "/public-search": ReadOnlyResponse(
                status_code=503,
                url="https://gamma-api.polymarket.com/public-search",
                headers={},
                content=b"{}",
            )
        }
    )
    repo = WeatherAlphaRepository(tmp_path / "research.sqlite")
    repo.init_schema()
    collector = PolymarketCollector(
        http=ReadOnlyHttpClient(transport=transport, max_retries=0),
        repository=repo,
        raw_root=tmp_path / "raw",
    )
    try:
        collector.collect(
            PolymarketCollectOptions(
                max_pages=1,
                cities=("paris",),
                collect_prices=False,
                collect_trades=False,
                collect_current_books=False,
            )
        )
    except RetryExhaustedError:
        pass
    else:
        raise AssertionError("expected RetryExhaustedError for 503")
    assert list((tmp_path / "raw").rglob("*.json")) == []


def test_collect_options_reject_unbounded_or_unknown_values(tmp_path: Path) -> None:
    import pytest

    collector = PolymarketCollector(
        http=ReadOnlyHttpClient(transport=ForbiddenNetworkTransport()),
        repository=None,
        raw_root=tmp_path / "raw",
    )
    with pytest.raises(ValueError, match=r"page-size|page_size"):
        collector.collect(PolymarketCollectOptions(dry_run=True, page_size=0))
    with pytest.raises(ValueError, match=r"page-size|page_size"):
        collector.collect(PolymarketCollectOptions(dry_run=True, page_size=10_000))
    with pytest.raises(ValueError, match="detail"):
        collector.collect(PolymarketCollectOptions(dry_run=True, max_detail_markets=0))
    with pytest.raises(ValueError, match="city"):
        collector.collect(PolymarketCollectOptions(dry_run=True, cities=("atlantis",)))


def test_malformed_trades_and_book_levels_are_skipped_not_zero_filled(tmp_path: Path) -> None:
    market = _load("gamma_market_paris.json")
    book = _load("clob_book_malformed.json")
    assert isinstance(book, dict)

    def book_for(params: dict[str, object]) -> dict[str, object]:
        payload = dict(book)
        payload["asset_id"] = str(params.get("token_id") or "")
        return payload

    transport = RecordingGetTransport(
        {
            "/public-search": {"events": [{"markets": [market]}], "markets": []},
            "/markets": [],
            "/trades": _load("data_api_trades_malformed.json"),
            "/book": book_for,
        }
    )
    repo = WeatherAlphaRepository(tmp_path / "research.sqlite")
    repo.init_schema()
    collector = PolymarketCollector(
        http=ReadOnlyHttpClient(transport=transport, max_retries=0),
        repository=repo,
        raw_root=tmp_path / "raw",
    )
    report = collector.collect(
        PolymarketCollectOptions(
            max_pages=1,
            page_size=20,
            cities=("paris",),
            collect_prices=False,
            collect_trades=True,
            collect_current_books=True,
        )
    )
    assert repo.count("trades") == 1
    with repo.connect() as conn:
        trade = conn.execute("SELECT token_id, price, size FROM trades").fetchone()
        assert trade["token_id"] == "token-yes-31c"
        assert trade["price"] == 0.0
        assert trade["size"] == 0.0
        level_rows = conn.execute(
            "SELECT price, size FROM order_book_levels ORDER BY price, size"
        ).fetchall()
        pairs = [(row["price"], row["size"]) for row in level_rows]
    assert (0.0, 0.0) in pairs
    assert (0.45, 100.0) in pairs
    assert (0.46, 150.0) in pairs
    assert all(pair in {(0.0, 0.0), (0.45, 100.0), (0.46, 150.0)} for pair in pairs)
    notes = " ".join(report.notes).lower()
    assert "skip" in notes
    assert "price" in notes or "size" in notes or "asset" in notes
