from typing import Any, NoReturn

import httpx
import pytest


@pytest.fixture(autouse=True)
def _block_live_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(
        self: httpx.Client,
        method: str,
        url: httpx.URL | str,
        *args: Any,
        **kwargs: Any,
    ) -> NoReturn:
        raise AssertionError(f"live HTTP is forbidden in the default suite: {method} {url}")

    monkeypatch.setattr(httpx.Client, "request", blocked)
