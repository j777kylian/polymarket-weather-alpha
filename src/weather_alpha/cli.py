"""Research-only CLI. No trading commands exist.

WARNING: This tool never places orders, signs transactions, or sends
POST/PUT/PATCH/DELETE HTTP requests.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from weather_alpha.collectors.polymarket.collector import (
    PolymarketCollectOptions,
    PolymarketCollector,
)
from weather_alpha.collectors.polymarket.parser import TARGET_CITIES, validate_cities
from weather_alpha.collectors.weather.collector import WeatherCollectOptions, WeatherCollector
from weather_alpha.config.settings import (
    DEFAULT_MAX_DATE_SPAN_DAYS,
    DEFAULT_MAX_DETAIL_MARKETS,
    DEFAULT_MAX_PAGES,
    bounded_max_detail_markets,
    bounded_max_pages,
    bounded_page_size,
    default_paths,
    validate_date_range,
)
from weather_alpha.http.readonly import ReadOnlyHttpClient
from weather_alpha.storage.repository import WeatherAlphaRepository

NO_TRADING_BANNER = (
    "RESEARCH ONLY: weather-alpha never places orders, signs wallets, "
    "or sends non-GET HTTP requests."
)


@click.group()
def main() -> None:
    """Polymarket weather-market research collector (read-only)."""
    click.echo(NO_TRADING_BANNER)


@main.command("init-db")
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=None)
def init_db(db_path: Path | None) -> None:
    path = db_path or default_paths().db_path
    repo = WeatherAlphaRepository(path)
    repo.init_schema()
    click.echo(f"initialized schema v{repo.schema_version()} at {path}")


@main.command("inspect-db")
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=None)
def inspect_db(db_path: Path | None) -> None:
    path = db_path or default_paths().db_path
    repo = WeatherAlphaRepository(path)
    counts = repo.inspect_counts()
    click.echo(json.dumps({"db": str(path), "tables": counts}, indent=2))


@main.command("collect-polymarket")
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=None)
@click.option("--raw-root", type=click.Path(path_type=Path), default=None)
@click.option("--max-pages", type=int, default=DEFAULT_MAX_PAGES, show_default=True)
@click.option("--page-size", type=int, default=50, show_default=True)
@click.option(
    "--max-detail-markets",
    type=int,
    default=DEFAULT_MAX_DETAIL_MARKETS,
    show_default=True,
)
@click.option("--start-date", type=str, default=None)
@click.option("--end-date", type=str, default=None)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--city", "cities", multiple=True)
def collect_polymarket(
    db_path: Path | None,
    raw_root: Path | None,
    max_pages: int,
    page_size: int,
    max_detail_markets: int,
    start_date: str | None,
    end_date: str | None,
    dry_run: bool,
    cities: tuple[str, ...],
) -> None:
    try:
        pages = bounded_max_pages(max_pages)
        size = bounded_page_size(page_size)
        detail_markets = bounded_max_detail_markets(max_detail_markets)
        selected_cities = validate_cities(cities) if cities else tuple(sorted(TARGET_CITIES))
        if start_date and end_date:
            validate_date_range(start_date, end_date, max_span_days=DEFAULT_MAX_DATE_SPAN_DAYS)
        elif start_date or end_date:
            raise click.UsageError("start-date and end-date must be provided together")
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    paths = default_paths()
    options = PolymarketCollectOptions(
        max_pages=pages,
        page_size=size,
        start_date=start_date,
        end_date=end_date,
        dry_run=dry_run,
        max_detail_markets=detail_markets,
        cities=selected_cities,
    )
    repo = None if dry_run else WeatherAlphaRepository(db_path or paths.db_path)
    if repo is not None:
        repo.init_schema()
    collector = PolymarketCollector(
        http=ReadOnlyHttpClient(),
        repository=repo,
        raw_root=raw_root or paths.polymarket_raw,
    )
    report = collector.collect(options)
    click.echo(json.dumps(report.as_dict(), indent=2))


@main.command("collect-weather")
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=None)
@click.option("--raw-root", type=click.Path(path_type=Path), default=None)
@click.option("--station", "stations", multiple=True)
@click.option("--start-date", type=str, required=True)
@click.option("--end-date", type=str, required=True)
@click.option("--provider", type=str, default="open-meteo-historical-forecast", show_default=True)
@click.option("--stations-file", type=click.Path(path_type=Path), default=None)
@click.option("--dry-run", is_flag=True, default=False)
def collect_weather(
    db_path: Path | None,
    raw_root: Path | None,
    stations: tuple[str, ...],
    start_date: str,
    end_date: str,
    provider: str,
    stations_file: Path | None,
    dry_run: bool,
) -> None:
    try:
        validate_date_range(start_date, end_date, max_span_days=DEFAULT_MAX_DATE_SPAN_DAYS)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    paths = default_paths()
    options = WeatherCollectOptions(
        start_date=start_date,
        end_date=end_date,
        station_ids=stations or None,
        provider=provider,
        dry_run=dry_run,
        stations_file=stations_file,
    )
    repo = None if dry_run else WeatherAlphaRepository(db_path or paths.db_path)
    if repo is not None:
        repo.init_schema()
    collector = WeatherCollector(
        http=ReadOnlyHttpClient(),
        repository=repo,
        raw_root=raw_root or paths.weather_raw,
    )
    report = collector.collect(options)
    click.echo(json.dumps(report.as_dict(), indent=2))
