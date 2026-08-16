"""Target event-date coverage classification for schema-valid weather payloads.

Structural schema validators remain structural. This module answers whether a
schema-valid (or valid-empty) payload has usable Phase 3 coverage for a target
local event date — without inventing forecast maxima.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

NO_USABLE_EVENT_COVERAGE = "NO_USABLE_EVENT_COVERAGE"

CoverageStatus = Literal["eligible", "ineligible", "empty"]


@dataclass(frozen=True, slots=True)
class EventCoverageResult:
    status: CoverageStatus
    reason: str | None = None
    detail: str | None = None

    @property
    def usable(self) -> bool:
        return self.status == "eligible"

    @property
    def phase3_eligibility(self) -> Literal["eligible", "ineligible", "not_applicable"]:
        if self.status == "eligible":
            return "eligible"
        if self.status == "empty":
            return "not_applicable"
        return "ineligible"


def evaluate_single_run_event_coverage(
    payload: object,
    *,
    event_date: str,
) -> EventCoverageResult:
    """Provider-local hourly temps: ≥1 non-null on target date ⇒ eligible."""
    if not isinstance(payload, dict):
        return EventCoverageResult(
            status="ineligible",
            reason=NO_USABLE_EVENT_COVERAGE,
            detail="single_run_payload_not_object",
        )
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        return EventCoverageResult(
            status="ineligible",
            reason=NO_USABLE_EVENT_COVERAGE,
            detail="single_run_hourly_missing",
        )
    times = hourly.get("time")
    temps = hourly.get("temperature_2m")
    if not isinstance(times, list) or not isinstance(temps, list):
        return EventCoverageResult(
            status="ineligible",
            reason=NO_USABLE_EVENT_COVERAGE,
            detail="single_run_time_temp_not_lists",
        )
    if not times:
        return EventCoverageResult(
            status="empty",
            reason=NO_USABLE_EVENT_COVERAGE,
            detail="single_run_aligned_empty",
        )
    usable = False
    saw_target = False
    for index, stamp in enumerate(times):
        local = _local_date_token(stamp)
        if local != event_date:
            continue
        saw_target = True
        value = temps[index] if index < len(temps) else None
        if _is_finite_number(value):
            usable = True
            break
    if usable:
        return EventCoverageResult(status="eligible")
    detail = (
        "single_run_target_date_absent" if not saw_target else "single_run_target_date_all_null"
    )
    return EventCoverageResult(
        status="ineligible",
        reason=NO_USABLE_EVENT_COVERAGE,
        detail=detail,
    )


def evaluate_archive_event_coverage(
    payload: object,
    *,
    event_date: str,
) -> EventCoverageResult:
    """Archive daily exact target date with non-null max ⇒ eligible."""
    if not isinstance(payload, dict):
        return EventCoverageResult(
            status="ineligible",
            reason=NO_USABLE_EVENT_COVERAGE,
            detail="archive_payload_not_object",
        )
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        return EventCoverageResult(
            status="ineligible",
            reason=NO_USABLE_EVENT_COVERAGE,
            detail="archive_daily_missing",
        )
    times = daily.get("time")
    temps = daily.get("temperature_2m_max")
    if not isinstance(times, list) or not isinstance(temps, list):
        return EventCoverageResult(
            status="ineligible",
            reason=NO_USABLE_EVENT_COVERAGE,
            detail="archive_time_temp_not_lists",
        )
    if not times:
        return EventCoverageResult(
            status="empty",
            reason=NO_USABLE_EVENT_COVERAGE,
            detail="archive_aligned_empty",
        )
    for index, stamp in enumerate(times):
        if not isinstance(stamp, str) or stamp != event_date:
            continue
        value = temps[index] if index < len(temps) else None
        if _is_finite_number(value):
            return EventCoverageResult(status="eligible")
        return EventCoverageResult(
            status="ineligible",
            reason=NO_USABLE_EVENT_COVERAGE,
            detail="archive_target_date_null_max",
        )
    return EventCoverageResult(
        status="ineligible",
        reason=NO_USABLE_EVENT_COVERAGE,
        detail="archive_target_date_absent",
    )


def _local_date_token(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _is_finite_number(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return math.isfinite(float(value))
    if isinstance(value, str):
        try:
            return math.isfinite(float(value.strip()))
        except ValueError:
            return False
    return False


def coverage_quarantine_detail(
    result: EventCoverageResult,
    *,
    provider: str,
    raw_path: str | None = None,
    request_url: str | None = None,
    content_hash: str | None = None,
) -> str:
    parts = [
        NO_USABLE_EVENT_COVERAGE,
        f"provider={provider}",
        f"status={result.status}",
    ]
    if result.detail:
        parts.append(f"detail={result.detail}")
    if raw_path:
        parts.append(f"raw={raw_path}")
    if request_url:
        parts.append(f"url={request_url}")
    if content_hash:
        parts.append(f"hash={content_hash}")
    return "; ".join(parts)
