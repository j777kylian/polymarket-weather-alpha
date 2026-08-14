from datetime import UTC, datetime
from pathlib import Path

from weather_alpha.storage.raw import (
    PersistedPayload,
    canonical_request_url,
    deterministic_raw_path,
    persist_raw_payload,
)


def test_raw_path_is_content_addressed_by_url_and_payload_digest(tmp_path: Path) -> None:
    url = canonical_request_url("https://clob.polymarket.com/book", {"token_id": "abc"})
    first = {"bids": [{"price": "0.40"}], "timestamp": "1"}
    second = {"bids": [{"price": "0.41"}], "timestamp": "2"}
    path_a = persist_raw_payload(
        tmp_path,
        source="polymarket/clob-book",
        url=url,
        payload=first,
        retrieved_at=datetime(2024, 7, 15, 12, 0, tzinfo=UTC),
    )
    path_a_again = persist_raw_payload(
        tmp_path,
        source="polymarket/clob-book",
        url=url,
        payload=first,
        retrieved_at=datetime(2024, 7, 15, 12, 5, tzinfo=UTC),
    )
    path_b = persist_raw_payload(
        tmp_path,
        source="polymarket/clob-book",
        url=url,
        payload=second,
        retrieved_at=datetime(2024, 7, 15, 12, 6, tzinfo=UTC),
    )
    assert isinstance(path_a, PersistedPayload)
    assert path_a.raw_path == path_a_again.raw_path
    assert path_a.content_sha256 == path_a_again.content_sha256
    assert path_a.request_url == url
    assert path_b.raw_path != path_a.raw_path
    assert path_b.content_sha256 != path_a.content_sha256
    assert Path(path_a.raw_path).is_file()
    assert Path(path_b.raw_path).is_file()
    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 2


def test_deterministic_path_requires_payload_digest() -> None:
    url = "https://example.test/book"
    path_one = deterministic_raw_path(
        Path("/tmp/raw"),
        source="clob-book",
        url=url,
        payload_digest="aaa",
    )
    path_two = deterministic_raw_path(
        Path("/tmp/raw"),
        source="clob-book",
        url=url,
        payload_digest="bbb",
    )
    assert path_one != path_two
