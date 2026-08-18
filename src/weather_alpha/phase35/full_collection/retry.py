"""GET-only retry classification. Distinct transient vs provider-no-data."""

from __future__ import annotations

import ssl
from typing import Any

import httpx

from weather_alpha.phase35.full_collection.ledger import (
    TRANSIENT_CLASSES,
    ResultClassification,
)
from weather_alpha.phase35.full_collection.policy import (
    INTER_ATTEMPT_DELAY_SECONDS,
    MAX_ATTEMPTS_PER_IDENTITY,
    RETRY_AFTER_CAP_SECONDS,
    TRANSIENT_5XX,
)


def classify_exception(exc: BaseException) -> ResultClassification:
    if _is_timeout(exc):
        return ResultClassification.TIMEOUT
    if _is_tls(exc):
        return ResultClassification.TLS_FAILURE
    if _is_transient_transport(exc):
        return ResultClassification.TRANSIENT_TRANSPORT_FAILURE
    return ResultClassification.HTTP_FAILURE


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return True
    cause = exc.__cause__ or exc.__context__
    return cause is not None and cause is not exc and _is_timeout(cause)


def _is_tls(exc: BaseException) -> bool:
    if isinstance(exc, ssl.SSLError):
        return True
    text = str(exc).lower()
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout) and (
        "ssl" in text or "tls" in text or "certificate" in text
    ):
        return True
    cause = exc.__cause__ or exc.__context__
    return cause is not None and cause is not exc and _is_tls(cause)


def _is_transient_transport(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException | ssl.SSLError):
        return False
    if isinstance(exc, httpx.ConnectError) and _is_tls(exc):
        return False
    if isinstance(exc, httpx.TransportError | ConnectionError):
        return True
    cause = exc.__cause__ or exc.__context__
    return cause is not None and cause is not exc and _is_transient_transport(cause)


def classify_http_outcome(
    *,
    status_code: int,
    payload: Any,
    schema_ok: bool | None = None,
) -> ResultClassification:
    if status_code == 429:
        return ResultClassification.RATE_LIMITED
    if status_code in TRANSIENT_5XX:
        return ResultClassification.TRANSIENT_5XX
    if status_code >= 400:
        return ResultClassification.HTTP_FAILURE
    if schema_ok is False:
        return ResultClassification.SCHEMA_ERROR
    if _explicit_provider_no_data(payload):
        return ResultClassification.PROVIDER_NO_DATA
    if _valid_empty(payload):
        return ResultClassification.VALID_EMPTY
    return ResultClassification.SUCCESS


def _explicit_provider_no_data(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    reason = str(payload.get("reason") or payload.get("error_reason") or "").lower()
    if "no data" in reason or reason in {"no_data", "nodata", "not_available"}:
        return True
    return payload.get("provider_no_data") is True


def _valid_empty(payload: Any) -> bool:
    if payload in ({}, [], None):
        return True
    if isinstance(payload, dict):
        if payload.get("history") == []:
            return True
        if (payload.get("hourly") == {} or payload.get("hourly") is None) and (
            payload.get("reason") or payload.get("error")
        ):
            return False
        events = payload.get("events")
        markets = payload.get("markets")
        if events == [] and markets == []:
            return True
    return False


def is_retryable(classification: ResultClassification, *, http_status: int | None = None) -> bool:
    del http_status
    return classification in TRANSIENT_CLASSES


def attempts_exhausted(attempt_number: int) -> bool:
    return attempt_number >= MAX_ATTEMPTS_PER_IDENTITY


def retry_delay_seconds(retry_after: float | None) -> float:
    if retry_after is None:
        return float(INTER_ATTEMPT_DELAY_SECONDS)
    return float(min(max(retry_after, 0.0), RETRY_AFTER_CAP_SECONDS))
