from collections.abc import Mapping
from typing import Any

from tests.fakes import RecordingGetTransport
from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyResponse, RetryExhaustedError


class FlakyTransport:
    def __init__(self) -> None:
        self.calls = 0

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> ReadOnlyResponse:
        del params, headers, timeout
        self.calls += 1
        if self.calls < 3:
            return ReadOnlyResponse(
                status_code=429,
                url=url,
                headers={"Retry-After": "0"},
                content=b"{}",
            )
        return ReadOnlyResponse(status_code=200, url=url, headers={}, content=b'{"ok": true}')


def test_retries_on_429_then_succeeds() -> None:
    sleeps: list[float] = []
    transport = FlakyTransport()
    client = ReadOnlyHttpClient(
        transport=transport, max_retries=4, backoff_base_s=0.01, sleeper=sleeps.append
    )
    response = client.get("https://example.test/ok")
    assert response.status_code == 200
    assert transport.calls == 3
    assert sleeps


def test_retry_exhaustion() -> None:
    class Always429:
        def get(
            self,
            url: str,
            *,
            params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None,
            timeout: float | None = None,
        ) -> ReadOnlyResponse:
            del params, headers, timeout
            return ReadOnlyResponse(status_code=429, url=url, headers={}, content=b"{}")

    client = ReadOnlyHttpClient(
        transport=Always429(), max_retries=1, backoff_base_s=0.0, sleeper=lambda _s: None
    )
    try:
        client.get("https://example.test/limited")
    except RetryExhaustedError:
        return
    raise AssertionError("expected RetryExhaustedError")


def test_recording_transport_only_gets() -> None:
    transport = RecordingGetTransport({"/ok": {"ok": True}})
    client = ReadOnlyHttpClient(transport=transport, max_retries=0)
    client.get("https://example.test/ok")
    assert transport.calls == [("GET", "https://example.test/ok")]
