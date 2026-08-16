"""Runtime settings. No API keys are required or accepted for trading."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from weather_alpha.models.timeutil import parse_timestamp

DEFAULT_MAX_PAGES = 5
DEFAULT_MAX_DATE_SPAN_DAYS = 31
DEFAULT_PHASE3_MAX_DATE_SPAN_DAYS = 62
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
DEFAULT_MAX_DETAIL_MARKETS = 20
MAX_DETAIL_MARKETS = 100
DEFAULT_FORECAST_LEAD_HOURS = 24.0
DEFAULT_PRICE_FIDELITY_MINUTES = 60
MAX_PRICE_FIDELITY_MINUTES = 24 * 60
MAX_FORECAST_LEAD_HOURS = 240.0


@dataclass(frozen=True, slots=True)
class Paths:
    db_path: Path
    polymarket_raw: Path
    weather_raw: Path
    processed: Path


def default_paths(root: Path | None = None) -> Paths:
    base = root or Path.cwd()
    return Paths(
        db_path=base / "data" / "processed" / "weather_alpha.sqlite",
        polymarket_raw=base / "data" / "polymarket",
        weather_raw=base / "data" / "weather",
        processed=base / "data" / "processed",
    )


def parse_iso_date(value: str) -> date:
    parsed = parse_timestamp(value if "T" in value else f"{value}T00:00:00Z")
    return parsed.date()


def validate_date_range(
    start: str,
    end: str,
    *,
    max_span_days: int = DEFAULT_MAX_DATE_SPAN_DAYS,
) -> tuple[date, date]:
    start_date = parse_iso_date(start)
    end_date = parse_iso_date(end)
    if end_date < start_date:
        raise ValueError("end-date must be on or after start-date")
    span = (end_date - start_date).days + 1
    if span > max_span_days:
        raise ValueError(f"date range spans {span} days; maximum allowed is {max_span_days}")
    return start_date, end_date


def bounded_max_pages(value: int, *, absolute_max: int = 50) -> int:
    if value <= 0:
        raise ValueError("max-pages must be positive")
    if value > absolute_max:
        raise ValueError(f"max-pages {value} exceeds absolute bound {absolute_max}")
    return value


def bounded_page_size(value: int, *, absolute_max: int = MAX_PAGE_SIZE) -> int:
    if value <= 0:
        raise ValueError("page_size must be positive")
    if value > absolute_max:
        raise ValueError(f"page_size {value} exceeds absolute bound {absolute_max}")
    return value


def bounded_max_detail_markets(value: int, *, absolute_max: int = MAX_DETAIL_MARKETS) -> int:
    if value <= 0:
        raise ValueError("max_detail_markets must be positive")
    if value > absolute_max:
        raise ValueError(f"max_detail_markets {value} exceeds absolute bound {absolute_max}")
    return value


def bounded_positive_int(name: str, value: int, *, absolute_max: int) -> int:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    if value > absolute_max:
        raise ValueError(f"{name} {value} exceeds absolute bound {absolute_max}")
    return value


def bounded_positive_float(name: str, value: float, *, absolute_max: float) -> float:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    if value > absolute_max:
        raise ValueError(f"{name} {value} exceeds absolute bound {absolute_max}")
    return value
