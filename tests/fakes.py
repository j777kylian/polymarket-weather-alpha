from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from weather_alpha.http.readonly import ReadOnlyResponse


class RecordingGetTransport:
    """In-memory GET transport for tests. Raises if a caller tries to mutate."""

    def __init__(self, routes: Mapping[str, Any]) -> None:
        self.routes = dict(routes)
        self.calls: list[tuple[str, str]] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> ReadOnlyResponse:
        del headers, timeout
        full = _url_with_params(url, params)
        self.calls.append(("GET", full))
        payload = _lookup(self.routes, url, params)
        if isinstance(payload, ReadOnlyResponse):
            return payload
        if payload is None:
            return ReadOnlyResponse(status_code=404, url=full, headers={}, content=b"{}")
        import json

        body = json.dumps(payload).encode("utf-8")
        return ReadOnlyResponse(status_code=200, url=full, headers={}, content=body)


class ForbiddenNetworkTransport:
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> ReadOnlyResponse:
        raise AssertionError(f"network was invoked: GET {url} params={params}")


def _url_with_params(url: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return url
    split = urlsplit(url)
    items = parse_qsl(split.query, keep_blank_values=True)
    for key, value in params.items():
        items.append((key, str(value)))
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(items), ""))


def _lookup(routes: Mapping[str, Any], url: str, params: Mapping[str, Any] | None) -> Any | None:
    if url in routes:
        return routes[url]
    path = urlsplit(url).path
    for key, value in routes.items():
        if path.endswith(key) or key in url:
            if callable(value):
                return value(params or {})
            return value
    return None
