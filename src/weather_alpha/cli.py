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
    DEFAULT_FORECAST_LEAD_HOURS,
    DEFAULT_MAX_DATE_SPAN_DAYS,
    DEFAULT_MAX_DETAIL_MARKETS,
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_SIZE,
    DEFAULT_PHASE3_MAX_DATE_SPAN_DAYS,
    DEFAULT_PRICE_FIDELITY_MINUTES,
    MAX_FORECAST_LEAD_HOURS,
    MAX_PRICE_FIDELITY_MINUTES,
    bounded_max_detail_markets,
    bounded_max_pages,
    bounded_page_size,
    bounded_positive_float,
    bounded_positive_int,
    default_paths,
    validate_date_range,
)
from weather_alpha.http.readonly import ReadOnlyHttpClient
from weather_alpha.phase35.full_collection.plan import (
    refuse_historical_collection,
    run_offline_dataset_acceptance,
    validate_full_collection_plan,
)
from weather_alpha.phase35.full_collection.policy import (
    END_DATE,
    REQUEST_BUDGET_REDESIGN_REQUIRED,
    START_DATE,
)
from weather_alpha.phase35.readiness import run_offline_readiness
from weather_alpha.research.collect import Phase3CollectOptions, Phase3Collector
from weather_alpha.research.run import load_quarantine, load_snapshots_from_jsonl, run_phase3
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


@main.command("phase3-collect")
@click.option("--start-date", type=str, required=True)
@click.option("--end-date", type=str, required=True)
@click.option("--output-root", type=click.Path(path_type=Path), required=True)
@click.option("--max-search-pages", type=int, default=DEFAULT_MAX_PAGES, show_default=True)
@click.option(
    "--search-limit-per-type",
    type=int,
    default=DEFAULT_PAGE_SIZE,
    show_default=True,
)
@click.option(
    "--price-fidelity-minutes",
    type=int,
    default=DEFAULT_PRICE_FIDELITY_MINUTES,
    show_default=True,
)
@click.option(
    "--forecast-lead-hours",
    type=float,
    default=DEFAULT_FORECAST_LEAD_HOURS,
    show_default=True,
)
@click.option("--city", "cities", multiple=True)
@click.option("--stations-file", type=click.Path(path_type=Path), default=None)
@click.option("--dry-run", is_flag=True, default=False)
def phase3_collect(
    start_date: str,
    end_date: str,
    output_root: Path,
    max_search_pages: int,
    search_limit_per_type: int,
    price_fidelity_minutes: int,
    forecast_lead_hours: float,
    cities: tuple[str, ...],
    stations_file: Path | None,
    dry_run: bool,
) -> None:
    try:
        validate_date_range(start_date, end_date, max_span_days=DEFAULT_PHASE3_MAX_DATE_SPAN_DAYS)
        pages = bounded_max_pages(max_search_pages)
        search_limit = bounded_page_size(search_limit_per_type)
        fidelity = bounded_positive_int(
            "price-fidelity-minutes",
            price_fidelity_minutes,
            absolute_max=MAX_PRICE_FIDELITY_MINUTES,
        )
        lead = bounded_positive_float(
            "forecast-lead-hours",
            forecast_lead_hours,
            absolute_max=MAX_FORECAST_LEAD_HOURS,
        )
        selected = validate_cities(cities) if cities else tuple(sorted(TARGET_CITIES))
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    options = Phase3CollectOptions(
        start_date=start_date,
        end_date=end_date,
        output_root=output_root,
        max_search_pages=pages,
        search_limit_per_type=search_limit,
        price_fidelity_minutes=fidelity,
        forecast_lead_hours=lead,
        cities=selected,
        dry_run=dry_run,
        stations_file=stations_file,
    )
    collector = Phase3Collector(http=ReadOnlyHttpClient())
    report = collector.collect(options)
    click.echo(json.dumps(report.as_dict(), indent=2))


@main.command("phase3-run")
@click.option("--input-root", type=click.Path(path_type=Path), required=True)
@click.option("--output-root", type=click.Path(path_type=Path), required=True)
def phase3_run(input_root: Path, output_root: Path) -> None:
    jsonl = input_root / "phase3_snapshots.jsonl"
    if not jsonl.is_file():
        raise click.UsageError(f"missing normalized snapshots: {jsonl}")
    snapshots = load_snapshots_from_jsonl(jsonl)
    quarantined = load_quarantine(input_root / "phase3_quarantine.json")
    manifest_path = input_root / "phase3_source_manifest.json"
    collect_manifest = None
    if manifest_path.is_file():
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        collect_manifest = loaded if isinstance(loaded, dict) else None
    result = run_phase3(
        snapshots,
        output_dir=output_root,
        quarantined=quarantined,
        collect_manifest=collect_manifest,
    )
    click.echo(
        json.dumps(
            {
                "snapshots": result.audit.snapshots,
                "markets": result.audit.markets,
                "backtest_test_status": result.backtest_test.status,
                "executable_trades": result.backtest_test.executable_trades,
                "reports": str(output_root / "reports"),
            },
            indent=2,
            sort_keys=True,
        )
    )


@main.command("phase35-readiness")
@click.option("--output-root", type=click.Path(path_type=Path), required=True)
def phase35_readiness(output_root: Path) -> None:
    """Offline Phase 3.5 collection-readiness (fixtures only; no full collection)."""
    result = run_offline_readiness(output_dir=output_root)
    click.echo(json.dumps(result.as_dict(), indent=2, sort_keys=True))


@main.command("phase35-full-collection-plan")
@click.option("--start-date", type=str, default=START_DATE, show_default=True)
@click.option("--end-date", type=str, default=END_DATE, show_default=True)
@click.option(
    "--manifest",
    type=click.Path(path_type=Path),
    required=True,
    help="Plan/preflight artifact path. Authorized collection manifest is not written when blocked.",
)
def phase35_full_collection_plan(start_date: str, end_date: str, manifest: Path) -> None:
    """Validate the immutable full-collection contract. No provider requests."""
    if start_date != START_DATE or end_date != END_DATE:
        raise click.UsageError(
            f"window is frozen to {START_DATE}..{END_DATE}; got {start_date}..{end_date}"
        )
    result = validate_full_collection_plan(manifest_path=manifest)
    click.echo(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    if result.status == REQUEST_BUDGET_REDESIGN_REQUIRED:
        raise SystemExit(2)


@main.command("phase35-dataset-acceptance")
@click.option("--manifest", type=click.Path(path_type=Path), required=True)
@click.option("--output-root", type=click.Path(path_type=Path), required=True)
def phase35_dataset_acceptance(manifest: Path, output_root: Path) -> None:
    """Offline dataset audit. No provider requests. Does not collect."""
    del manifest
    audit = run_offline_dataset_acceptance(output_dir=output_root)
    click.echo(json.dumps(audit.as_dict(), indent=2, sort_keys=True))
    if not audit.phase35_dataset_ready:
        raise SystemExit(2)


@main.command("phase35-collect-historical")
@click.option("--manifest", type=click.Path(path_type=Path), required=True)
@click.option("--output-root", type=click.Path(path_type=Path), required=True)
@click.option("--resume", is_flag=True, default=False)
def phase35_collect_historical(manifest: Path, output_root: Path, resume: bool) -> None:
    """Refuse live collection until final review; not an execution grant."""
    del manifest, output_root, resume
    payload = refuse_historical_collection()
    click.echo(json.dumps(payload, indent=2, sort_keys=True))
    raise SystemExit(2)
