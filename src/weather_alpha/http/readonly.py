"""Strict read-only HTTP: GET is the only supported method.

This module exists so collectors cannot accidentally POST orders, cancel
orders, or otherwise mutate remote state. The client never exposes
post/put/patch/delete and rejects non-GET request() calls.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

FORBIDDEN_HTTP_METHODS = frozenset(
    {"POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)


class ReadOnlyHttpError(RuntimeError):
    """Raised when a non-GET method is attempted or a response is invalid."""


class RetryExhaustedError(ReadOnlyHttpError):
    """Raised after GET retries are exhausted."""


@dataclass(frozen=True)
class ReadOnlyResponse:
    status_code: int
    url: str
    headers: Mapping[str, str]
    content: bytes
    elapsed_s: float | None = None

    def json(self) -> Any:
        return httpx.Response(self.status_code, content=self.content).json()

    def text(self) -> str:
        return self.content.decode("utf-8")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise ReadOnlyHttpError(f"HTTP {self.status_code} for GET {self.url}")


class ReadOnlyTransport(Protocol):
    """Injectable GET-only transport used by collectors and tests."""

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> ReadOnlyResponse: ...


class HttpxGetTransport:
    """httpx adapter that only calls Client.get / GET requests."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._timeout = timeout
        self._transport = transport
        self._headers = dict(headers or {})
        self._headers.setdefault(
            "User-Agent",
            "weather-alpha-research/0.1 (+research-only; GET-only; no-trading)",
        )

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> ReadOnlyResponse:
        merged = {**self._headers, **(headers or {})}
        with httpx.Client(
            timeout=timeout if timeout is not None else self._timeout,
            transport=self._transport,
            headers=merged,
        ) as client:
            response = client.request("GET", url, params=params)
        return ReadOnlyResponse(
            status_code=response.status_code,
            url=str(response.url),
            headers=dict(response.headers),
            content=response.content,
            elapsed_s=response.elapsed.total_seconds() if response.elapsed else None,
        )


class ReadOnlyHttpClient:
    """Public collector HTTP surface: get() and GET-only request()."""

    def __init__(
        self,
        transport: ReadOnlyTransport | None = None,
        *,
        max_retries: int = 4,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 8.0,
        retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504}),
        sleeper: Any = None,
    ) -> None:
        self._transport: ReadOnlyTransport = transport or HttpxGetTransport()
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._retry_statuses = retry_statuses
        self._sleeper = sleeper or _default_sleep

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> ReadOnlyResponse:
        last_error: Exception | None = None
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                response = self._transport.get(url, params=params, headers=headers, timeout=timeout)
            except (httpx.TransportError, OSError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                self._sleeper(self._backoff_seconds(attempt, retry_after=None))
                continue
            if response.status_code not in self._retry_statuses:
                return response
            last_error = ReadOnlyHttpError(f"HTTP {response.status_code} for GET {response.url}")
            if attempt >= self._max_retries:
                break
            retry_after = _parse_retry_after(response.headers)
            self._sleeper(self._backoff_seconds(attempt, retry_after=retry_after))
        raise RetryExhaustedError(f"GET retries exhausted for {url}: {last_error}") from last_error

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> ReadOnlyResponse:
        normalized = method.upper()
        if normalized != "GET":
            raise ReadOnlyHttpError(
                f"HTTP method {method!r} is blocked; this client is GET-only research access"
            )
        return self.get(url, params=params, headers=headers, timeout=timeout)

    def _backoff_seconds(self, attempt: int, *, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(max(retry_after, 0.0), self._backoff_max_s)
        delay = float(self._backoff_base_s * (2**attempt))
        return float(min(delay, self._backoff_max_s))

    def __getattr__(self, name: str) -> Any:
        if name.upper() in FORBIDDEN_HTTP_METHODS or name.lower() in {
            m.lower() for m in FORBIDDEN_HTTP_METHODS
        }:
            raise AttributeError(
                f"{name!r} is unavailable on ReadOnlyHttpClient (GET-only, no trading)"
            )
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) - {m.lower() for m in FORBIDDEN_HTTP_METHODS})


def _default_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
