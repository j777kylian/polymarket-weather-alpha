"""Phase 3 bounded GET-only collection into a research dataset."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from weather_alpha.collectors.pagination import paginate_pages, unique_by
from weather_alpha.collectors.polymarket.client import PolymarketReadClient
from weather_alpha.collectors.polymarket.collector import _markets_from_search
from weather_alpha.collectors.polymarket.parser import (
    TARGET_CITIES,
    ParsedGammaMarket,
    is_temperature_market_text,
    parse_gamma_market,
    validate_cities,
)
from weather_alpha.collectors.weather.adapters import OpenMeteoArchiveAdapter
from weather_alpha.collectors.weather.parser import parse_open_meteo_response
from weather_alpha.config.settings import (
    DEFAULT_FORECAST_LEAD_HOURS,
    DEFAULT_PAGE_SIZE,
    DEFAULT_PHASE3_MAX_DATE_SPAN_DAYS,
    DEFAULT_PRICE_FIDELITY_MINUTES,
    MAX_FORECAST_LEAD_HOURS,
    MAX_PRICE_FIDELITY_MINUTES,
    bounded_max_pages,
    bounded_page_size,
    bounded_positive_float,
    bounded_positive_int,
    validate_date_range,
)
from weather_alpha.config.stations import Station, load_stations
from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyHttpError, ReadOnlyResponse
from weather_alpha.models.timeutil import ensure_utc, utc_now
from weather_alpha.research.dataset import (
    LookaheadError,
    assemble_snapshot,
    dedupe_snapshots,
    snapshot_to_row,
    write_snapshots_parquet,
)
from weather_alpha.research.event_coverage import (
    coverage_quarantine_detail,
    evaluate_archive_event_coverage,
    evaluate_single_run_event_coverage,
)
from weather_alpha.research.event_groups import accept_event_groups
from weather_alpha.research.observations import DisabledObservationProvider
from weather_alpha.research.prices import (
    PricePoint,
    parse_price_history_points,
    select_price_at_or_before,
)
from weather_alpha.research.provider_schema import (
    ProviderSchemaError,
    validate_archive_payload,
    validate_gamma_search_payload,
    validate_prices_history_payload,
    validate_single_run_payload,
)
from weather_alpha.research.settlement import parse_settlement_label
from weather_alpha.research.single_run import (
    HourlyForecastPoint,
    OpenMeteoSingleRunAdapter,
    choose_ecmwf_run,
    parse_single_run_forecast,
    predicted_daily_max,
)
from weather_alpha.research.stations import resolve_research_station
from weather_alpha.research.types import (
    EVENT_IDENTITY_AMBIGUOUS,
    HourlyForecastState,
    QuarantineRecord,
    ResearchSnapshot,
    build_canonical_event_identity,
)
from weather_alpha.storage.raw import PersistedPayload, persist_raw_payload

# Point-in-time CLOB /prices-history window. Gamma startDate/endDate are not used.
PRICE_HISTORY_LOOKBACK_DAYS = 7
PRICE_HTTP_ERROR_LIMITATION = (
    "CLOB prices-history HTTP error; market_probability left null (not treated as empty history)."
)
PRICE_HISTORY_EMPTY_LIMITATION = (
    "CLOB prices-history HTTP 200 with empty history; market_probability left null "
    "(not an HTTP error)."
)
PRICE_SCHEMA_ERROR_LIMITATION = (
    "CLOB prices-history HTTP 200 payload failed schema validation; market_probability left null "
    "(malformed/source-drift, not treated as empty history)."
)


@dataclass(frozen=True, slots=True)
class _PriceHistoryResult:
    points: tuple[PricePoint, ...]
    persisted: PersistedPayload | None
    limitations: tuple[str, ...] = ()
    http_error: bool = False
    empty_history: bool = False
    schema_error: bool = False


@dataclass(frozen=True, slots=True)
class Phase3CollectOptions:
    start_date: str
    end_date: str
    output_root: Path
    max_search_pages: int
    price_fidelity_minutes: int = DEFAULT_PRICE_FIDELITY_MINUTES
    forecast_lead_hours: float = DEFAULT_FORECAST_LEAD_HOURS
    cities: tuple[str, ...] = tuple(sorted(TARGET_CITIES))
    dry_run: bool = False
    stations_file: Path | None = None
    search_limit_per_type: int = DEFAULT_PAGE_SIZE


@dataclass
class Phase3CollectReport:
    dry_run: bool
    markets_seen: int = 0
    snapshots_written: int = 0
    quarantined: int = 0
    discovered_outside_range: int = 0
    price_http_errors: int = 0
    price_history_empty: int = 0
    price_schema_errors: int = 0
    gamma_schema_errors: int = 0
    single_run_schema_errors: int = 0
    archive_schema_errors: int = 0
    single_run_no_usable_event_coverage: int = 0
    archive_no_usable_event_coverage: int = 0
    notes: list[str] = field(default_factory=list)
    parquet_path: str | None = None
    manifest_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "markets_seen": self.markets_seen,
            "snapshots_written": self.snapshots_written,
            "quarantined": self.quarantined,
            "discovered_outside_range": self.discovered_outside_range,
            "price_http_errors": self.price_http_errors,
            "price_history_empty": self.price_history_empty,
            "price_schema_errors": self.price_schema_errors,
            "gamma_schema_errors": self.gamma_schema_errors,
            "single_run_schema_errors": self.single_run_schema_errors,
            "archive_schema_errors": self.archive_schema_errors,
            "single_run_no_usable_event_coverage": self.single_run_no_usable_event_coverage,
            "archive_no_usable_event_coverage": self.archive_no_usable_event_coverage,
            "notes": list(self.notes),
            "parquet_path": self.parquet_path,
            "manifest_path": self.manifest_path,
        }


def validate_phase3_collect_options(options: Phase3CollectOptions) -> Phase3CollectOptions:
    validate_date_range(
        options.start_date,
        options.end_date,
        max_span_days=DEFAULT_PHASE3_MAX_DATE_SPAN_DAYS,
    )
    pages = bounded_max_pages(options.max_search_pages)
    fidelity = bounded_positive_int(
        "price-fidelity-minutes",
        options.price_fidelity_minutes,
        absolute_max=MAX_PRICE_FIDELITY_MINUTES,
    )
    lead = bounded_positive_float(
        "forecast-lead-hours",
        options.forecast_lead_hours,
        absolute_max=MAX_FORECAST_LEAD_HOURS,
    )
    cities = validate_cities(options.cities)
    search_limit = bounded_page_size(options.search_limit_per_type)
    return Phase3CollectOptions(
        start_date=options.start_date,
        end_date=options.end_date,
        output_root=options.output_root,
        max_search_pages=pages,
        price_fidelity_minutes=fidelity,
        forecast_lead_hours=lead,
        cities=cities,
        dry_run=options.dry_run,
        stations_file=options.stations_file,
        search_limit_per_type=search_limit,
    )


class Phase3Collector:
    def __init__(
        self,
        *,
        http: ReadOnlyHttpClient,
        retrieved_at: datetime | None = None,
    ) -> None:
        self._http = http
        self._client = PolymarketReadClient(http)
        self._single = OpenMeteoSingleRunAdapter(http)
        self._archive = OpenMeteoArchiveAdapter(http)
        self._observations = DisabledObservationProvider()
        self._retrieved_at = retrieved_at

    def collect(self, options: Phase3CollectOptions) -> Phase3CollectReport:
        options = validate_phase3_collect_options(options)
        report = Phase3CollectReport(dry_run=options.dry_run)
        if options.dry_run:
            report.notes.append("dry-run: no HTTP requests and no writes")
            report.notes.append(
                f"intended cities={list(options.cities)} pages={options.max_search_pages} "
                f"limit_per_type={options.search_limit_per_type} "
                f"dates={options.start_date}..{options.end_date}"
            )
            return report
        stations = load_stations(options.stations_file)
        raw_root = options.output_root / "raw"
        snapshots: list[ResearchSnapshot] = []
        quarantined: list[QuarantineRecord] = []
        parsed_markets = self._discover(options, raw_root, report)
        weather_cache: dict[tuple[str, str], dict[str, Any]] = {}
        for parsed in parsed_markets:
            built = self._market_to_snapshots(
                parsed,
                options=options,
                stations=stations,
                raw_root=raw_root,
                weather_cache=weather_cache,
                report=report,
            )
            snapshots.extend(built[0])
            quarantined.extend(built[1])
        unique, dupes = dedupe_snapshots(snapshots)
        quarantined.extend(dupes)
        unique, group_quarantine = accept_event_groups(unique)
        quarantined.extend(group_quarantine)
        unique = tuple(
            sorted(unique, key=lambda item: (item.event_date, item.token_id, item.condition_id))
        )
        parquet_path = options.output_root / "phase3_snapshots.parquet"
        write_snapshots_parquet(parquet_path, unique)
        jsonl_path = options.output_root / "phase3_snapshots.jsonl"
        _write_jsonl(jsonl_path, [snapshot_to_row(item) for item in unique])
        quarantine_path = options.output_root / "phase3_quarantine.json"
        _write_json(
            quarantine_path,
            [asdict(record) for record in quarantined],
        )
        usable_dates = sorted({item.event_date for item in unique})
        manifest = {
            "start_date": options.start_date,
            "end_date": options.end_date,
            "usable_event_dates": usable_dates,
            "cities": list(options.cities),
            "max_search_pages": options.max_search_pages,
            "search_limit_per_type": options.search_limit_per_type,
            "price_fidelity_minutes": options.price_fidelity_minutes,
            "forecast_lead_hours": options.forecast_lead_hours,
            "providers": [
                "polymarket-gamma-public-search",
                "polymarket-clob-prices-history",
                "open-meteo-single-runs",
                "open-meteo-archive",
            ],
            "markets_seen": report.markets_seen,
            "discovered_outside_range": report.discovered_outside_range,
            "price_http_errors": report.price_http_errors,
            "price_history_empty": report.price_history_empty,
            "price_schema_errors": report.price_schema_errors,
            "gamma_schema_errors": report.gamma_schema_errors,
            "single_run_schema_errors": report.single_run_schema_errors,
            "archive_schema_errors": report.archive_schema_errors,
            "single_run_no_usable_event_coverage": report.single_run_no_usable_event_coverage,
            "archive_no_usable_event_coverage": report.archive_no_usable_event_coverage,
            "snapshots": len(unique),
            "quarantined": len(quarantined),
            "parquet": str(parquet_path),
            "limitations": [
                "GET-only public APIs; no historical order-book reconstruction.",
                "Open-Meteo Historical Forecast is not used as point-in-time model input.",
                "Archive maxima are diagnostic, not settlement observations.",
                "Open-Meteo Archive is not a decision-time observation feature.",
                "Gamma startDate/endDate are not trusted for point-in-time CLOB price windows.",
                (
                    "CLOB prices-history uses a "
                    f"{PRICE_HISTORY_LOOKBACK_DAYS}-day lookback ending at decision_ts; "
                    "select_price_at_or_before is defense in depth."
                ),
                "HTTP 200 does not imply valid schema; malformed payloads are counted separately.",
            ],
        }
        manifest_path = options.output_root / "phase3_source_manifest.json"
        _write_json(manifest_path, manifest)
        report.snapshots_written = len(unique)
        report.quarantined = len(quarantined)
        report.parquet_path = str(parquet_path)
        report.manifest_path = str(manifest_path)
        report.notes.append(
            f"discovered_outside_range={report.discovered_outside_range}; "
            "counted in the collect report/manifest, not as per-market quarantine rows"
        )
        report.notes.append(
            f"price_http_errors={report.price_http_errors}; "
            f"price_history_empty={report.price_history_empty}; "
            f"price_schema_errors={report.price_schema_errors}"
        )
        return report

    def _discover(
        self,
        options: Phase3CollectOptions,
        raw_root: Path,
        report: Phase3CollectReport,
    ) -> list[ParsedGammaMarket]:
        found: list[ParsedGammaMarket] = []
        queries = []
        for city in options.cities:
            queries.append(f"highest temperature in {city}")
            queries.append(f"will the highest temperature in {city}")
        for query in queries:

            def fetch_page(page: int, search_query: str = query) -> list[ParsedGammaMarket]:
                response = self._client.public_search(
                    search_query,
                    page=page,
                    limit_per_type=options.search_limit_per_type,
                    keep_closed_markets=1,
                )
                payload = _successful_json(response)
                schema = validate_gamma_search_payload(payload)
                persisted = persist_raw_payload(
                    raw_root,
                    source="polymarket/gamma-search",
                    url=response.url,
                    payload=payload,
                    retrieved_at=self._now(),
                )
                if schema.status not in {"ok", "empty"}:
                    report.gamma_schema_errors += 1
                    report.notes.append(
                        f"gamma schema {schema.status}: {schema.detail or schema.provider} "
                        f"raw={persisted.raw_path} url={persisted.request_url} "
                        f"hash={persisted.content_sha256}"
                    )
                    return []
                if schema.status == "empty":
                    return []
                markets = []
                for raw in _markets_from_search(persisted.payload):
                    parsed = parse_gamma_market(
                        raw,
                        retrieved_url=persisted.request_url,
                        retrieved_at=persisted.retrieved_at,
                        raw_path=persisted.raw_path,
                        content_sha256=persisted.content_sha256,
                    )
                    markets.append(parsed)
                return markets

            found.extend(
                paginate_pages(
                    fetch_page,
                    max_pages=options.max_search_pages,
                    key=lambda item: item.market.condition_id,
                )
            )
        unique = unique_by(found, key=lambda item: item.market.condition_id)
        report.markets_seen = len(unique)
        in_range: list[ParsedGammaMarket] = []
        outside = 0
        for parsed in unique:
            event_date = parsed.market.event_date
            if event_date is not None and (
                event_date < options.start_date or event_date > options.end_date
            ):
                outside += 1
                continue
            in_range.append(parsed)
        report.discovered_outside_range = outside
        return in_range

    def _market_to_snapshots(
        self,
        parsed: ParsedGammaMarket,
        *,
        options: Phase3CollectOptions,
        stations: tuple[Station, ...],
        raw_root: Path,
        weather_cache: dict[tuple[str, str], dict[str, Any]],
        report: Phase3CollectReport,
    ) -> tuple[list[ResearchSnapshot], list[QuarantineRecord]]:
        snapshots: list[ResearchSnapshot] = []
        quarantined: list[QuarantineRecord] = []
        market = parsed.market
        if not is_temperature_market_text(market.question, market.slug):
            quarantined.append(_q(parsed, "not a temperature market"))
            return snapshots, quarantined
        if market.event_date is None:
            quarantined.append(_q(parsed, "unknown/ambiguous event date"))
            return snapshots, quarantined
        resolved_flag = parsed.raw.get("resolved")
        settlement = parse_settlement_label(
            closed=market.closed,
            resolved=resolved_flag if isinstance(resolved_flag, bool) else None,
            outcomes=parsed.raw.get("outcomes"),
            outcome_prices=parsed.raw.get("outcomePrices") or parsed.raw.get("outcome_prices"),
        )
        if settlement.label is None:
            quarantined.append(_q(parsed, settlement.quarantine_reason or "settlement unavailable"))
            return snapshots, quarantined
        station, station_reason = resolve_research_station(market.station_icao, stations)
        if station is None:
            quarantined.append(_q(parsed, station_reason or "unknown station"))
            return snapshots, quarantined
        yes_outcomes = [row for row in parsed.outcomes if row.outcome_label.lower() == "yes"]
        if not yes_outcomes:
            quarantined.append(_q(parsed, "YES token missing"))
            return snapshots, quarantined
        for outcome in yes_outcomes:
            if outcome.bucket_kind is None:
                quarantined.append(
                    _q(parsed, "unknown/ambiguous temperature bucket", token=outcome.token_id)
                )
                continue
            weather = self._weather_for(
                station,
                event_date=market.event_date,
                lead_hours=options.forecast_lead_hours,
                raw_root=raw_root,
                cache=weather_cache,
                report=report,
            )
            if weather.get("quarantine"):
                if weather.get("schema_error"):
                    detail = str(weather["quarantine"])
                    if detail.startswith("forecast schema"):
                        report.single_run_schema_errors += 1
                    elif detail.startswith("archive schema"):
                        report.archive_schema_errors += 1
                quarantined.append(_q(parsed, str(weather["quarantine"]), token=outcome.token_id))
                continue
            decision_ts: datetime = weather["decision_ts"]
            price_fetch = self._price_history(
                outcome.token_id,
                decision_ts=decision_ts,
                options=options,
                raw_root=raw_root,
            )
            if price_fetch.http_error:
                report.price_http_errors += 1
            elif price_fetch.schema_error:
                report.price_schema_errors += 1
            elif price_fetch.empty_history:
                report.price_history_empty += 1
            selected = select_price_at_or_before(price_fetch.points, decision_ts)
            price_raw = price_fetch.persisted
            identity = build_canonical_event_identity(
                ResearchSnapshot(
                    condition_id=market.condition_id,
                    market_id=market.market_id,
                    token_id=outcome.token_id,
                    city=market.city,
                    station_icao=station.station_id,
                    event_date=market.event_date,
                    bucket_label=outcome.group_item_title or outcome.outcome_label,
                    bucket_kind=outcome.bucket_kind,
                    temperature_celsius_min=outcome.temperature_celsius_min,
                    temperature_celsius_max=outcome.temperature_celsius_max,
                    decision_ts=decision_ts,
                    market_probability=None,
                    executable_entry_price=None,
                    best_bid=None,
                    best_ask=None,
                    midpoint=None,
                    spread=None,
                    volume=None,
                    liquidity=None,
                    weather_issued_at=None,
                    weather_available_at=None,
                    forecast_daily_max_c=None,
                    observation_max_so_far_c=None,
                    observation_as_of=None,
                    settlement_label=settlement.label,
                    diagnostic_actual_max_c=None,
                    provenance_urls=(),
                    raw_paths=(),
                    content_hashes=(),
                    limitations=(),
                    event_id=market.event_id,
                    question=market.question,
                    group_item_title=outcome.group_item_title,
                    slug=market.slug,
                    event_slug=market.event_slug,
                    neg_risk_market_id=market.neg_risk_market_id,
                    temperature_unit=outcome.temperature_unit,
                    temperature_native_min=outcome.temperature_native_min,
                    temperature_native_max=outcome.temperature_native_max,
                )
            )
            if identity.ambiguous:
                quarantined.append(
                    _q(
                        parsed,
                        identity.quarantine_reason or EVENT_IDENTITY_AMBIGUOUS,
                        token=outcome.token_id,
                    )
                )
                continue
            try:
                snap = assemble_snapshot(
                    condition_id=market.condition_id,
                    market_id=market.market_id,
                    token_id=outcome.token_id,
                    city=market.city,
                    station_icao=station.station_id,
                    event_date=market.event_date,
                    bucket_label=outcome.group_item_title or outcome.outcome_label,
                    bucket_kind=outcome.bucket_kind,
                    temperature_celsius_min=outcome.temperature_celsius_min,
                    temperature_celsius_max=outcome.temperature_celsius_max,
                    decision_ts=decision_ts,
                    settlement_label=settlement.label,
                    diagnostic_actual_max_c=weather.get("diagnostic_actual_max_c"),
                    provenance_urls=tuple(
                        url
                        for url in (
                            market.provenance.request_url,
                            weather.get("forecast_url"),
                            weather.get("archive_url"),
                            None if price_raw is None else price_raw.request_url,
                        )
                        if isinstance(url, str)
                    ),
                    raw_paths=tuple(
                        path
                        for path in (
                            market.provenance.raw_path,
                            weather.get("forecast_raw_path"),
                            weather.get("archive_raw_path"),
                            None if price_raw is None else price_raw.raw_path,
                        )
                        if isinstance(path, str)
                    ),
                    content_hashes=tuple(
                        digest
                        for digest in (
                            market.provenance.content_sha256,
                            weather.get("forecast_hash"),
                            weather.get("archive_hash"),
                            None if price_raw is None else price_raw.content_sha256,
                        )
                        if isinstance(digest, str)
                    ),
                    weather_issued_at=weather.get("issued_at"),
                    weather_available_at=weather.get("available_at"),
                    forecast_daily_max_c=weather.get("forecast_daily_max_c"),
                    price=selected,
                    observation_max_so_far_c=weather.get("observation_max_so_far_c"),
                    observation_as_of=weather.get("observation_as_of"),
                    event_id=market.event_id,
                    question=market.question,
                    group_item_title=outcome.group_item_title,
                    slug=market.slug,
                    event_slug=market.event_slug,
                    neg_risk_market_id=market.neg_risk_market_id,
                    temperature_unit=outcome.temperature_unit,
                    temperature_native_min=outcome.temperature_native_min,
                    temperature_native_max=outcome.temperature_native_max,
                    weather_valid_times=tuple(weather.get("valid_times") or ()),
                    dew_point_c=weather.get("dew_point_c"),
                    humidity_pct=weather.get("humidity_pct"),
                    cloud_cover_pct=weather.get("cloud_cover_pct"),
                    wind_speed=weather.get("wind_speed"),
                    wind_direction_deg=weather.get("wind_direction_deg"),
                    precipitation=weather.get("precipitation"),
                    surface_pressure=weather.get("surface_pressure"),
                    forecast_lead_hours=options.forecast_lead_hours,
                    extra_limitations=(
                        *tuple(weather.get("limitations") or ()),
                        *price_fetch.limitations,
                    ),
                    price_request_url=None if price_raw is None else price_raw.request_url,
                    price_raw_path=None if price_raw is None else price_raw.raw_path,
                    price_content_sha256=None if price_raw is None else price_raw.content_sha256,
                    forecast_hourly=tuple(weather.get("forecast_hourly") or ()),
                    canonical_event_key=identity.canonical_event_key,
                    canonical_event_source=identity.source,
                    canonical_event_evidence=identity.evidence_fields,
                    canonical_event_ambiguous=identity.ambiguous,
                    canonical_event_quarantine_reason=identity.quarantine_reason,
                )
            except LookaheadError as exc:
                quarantined.append(_q(parsed, f"lookahead rejected: {exc}", token=outcome.token_id))
                continue
            snapshots.append(snap)
        return snapshots, quarantined

    def _weather_for(
        self,
        station: Station,
        *,
        event_date: str,
        lead_hours: float,
        raw_root: Path,
        cache: dict[tuple[str, str], dict[str, Any]],
        report: Phase3CollectReport,
    ) -> dict[str, Any]:
        key = (station.station_id, event_date)
        if key in cache:
            return cache[key]
        decision = _decision_timestamp(event_date, station.timezone_name, lead_hours)
        run = choose_ecmwf_run(decision_ts=decision, event_date=event_date, station=station)
        if run is None:
            cache[key] = {"quarantine": "no ECMWF run with available_at at or before decision"}
            return cache[key]
        start = event_date
        end = event_date
        try:
            forecast_resp = self._single.fetch(
                station, start_date=start, end_date=end, run=run.run_param
            )
            forecast_payload = _successful_json(forecast_resp)
            forecast_schema = validate_single_run_payload(forecast_payload)
            forecast_persisted = persist_raw_payload(
                raw_root,
                source="weather/open-meteo-single-run",
                url=forecast_resp.url,
                payload=forecast_payload,
                retrieved_at=self._now(),
            )
            if forecast_schema.status not in {"ok", "empty"}:
                cache[key] = {
                    "quarantine": (
                        f"forecast schema {forecast_schema.status}: "
                        f"{forecast_schema.detail or forecast_schema.provider}; "
                        f"raw={forecast_persisted.raw_path} "
                        f"url={forecast_persisted.request_url} "
                        f"hash={forecast_persisted.content_sha256}"
                    ),
                    "schema_error": True,
                }
                return cache[key]
            forecast_coverage = evaluate_single_run_event_coverage(
                forecast_payload, event_date=event_date
            )
            if not forecast_coverage.usable:
                report.single_run_no_usable_event_coverage += 1
                cache[key] = {
                    "quarantine": coverage_quarantine_detail(
                        forecast_coverage,
                        provider="open-meteo-single-run",
                        raw_path=forecast_persisted.raw_path,
                        request_url=forecast_persisted.request_url,
                        content_hash=forecast_persisted.content_sha256,
                    ),
                    "coverage_ineligible": True,
                }
                return cache[key]
        except ReadOnlyHttpError as exc:
            cache[key] = {"quarantine": f"forecast HTTP error: {exc}"}
            return cache[key]
        parsed = parse_single_run_forecast(
            forecast_payload if isinstance(forecast_payload, dict) else {},
            station=station,
            issued_at=run.issued_at,
            request_url=forecast_persisted.request_url,
        )
        daily = predicted_daily_max(parsed, event_date=event_date)
        if daily is None:
            # Coverage gate should have rejected this; never invent a forecast max.
            report.single_run_no_usable_event_coverage += 1
            cache[key] = {
                "quarantine": coverage_quarantine_detail(
                    evaluate_single_run_event_coverage(forecast_payload, event_date=event_date),
                    provider="open-meteo-single-run",
                    raw_path=forecast_persisted.raw_path,
                    request_url=forecast_persisted.request_url,
                    content_hash=forecast_persisted.content_sha256,
                ),
                "coverage_ineligible": True,
            }
            return cache[key]
        event_hours = [row for row in parsed.hourly if row.local_date == event_date]
        forecast_hourly = tuple(_hourly_state(row) for row in event_hours)
        archive_url = None
        archive_raw_path = None
        archive_hash = None
        diagnostic = None
        try:
            archive_resp = self._archive.fetch(station, start_date=event_date, end_date=event_date)
            archive_payload = _successful_json(archive_resp)
            archive_schema = validate_archive_payload(archive_payload)
            archive_persisted = persist_raw_payload(
                raw_root,
                source="weather/open-meteo-archive",
                url=archive_resp.url,
                payload=archive_payload,
                retrieved_at=self._now(),
            )
            if archive_schema.status not in {"ok", "empty"}:
                cache[key] = {
                    "quarantine": (
                        f"archive schema {archive_schema.status}: "
                        f"{archive_schema.detail or archive_schema.provider}; "
                        f"raw={archive_persisted.raw_path} "
                        f"url={archive_persisted.request_url} "
                        f"hash={archive_persisted.content_sha256}"
                    ),
                    "schema_error": True,
                }
                return cache[key]
            archive_coverage = evaluate_archive_event_coverage(
                archive_payload, event_date=event_date
            )
            if not archive_coverage.usable:
                report.archive_no_usable_event_coverage += 1
                cache[key] = {
                    "quarantine": coverage_quarantine_detail(
                        archive_coverage,
                        provider="open-meteo-archive",
                        raw_path=archive_persisted.raw_path,
                        request_url=archive_persisted.request_url,
                        content_hash=archive_persisted.content_sha256,
                    ),
                    "coverage_ineligible": True,
                }
                return cache[key]
            archive_parsed = parse_open_meteo_response(
                archive_payload if isinstance(archive_payload, dict) else {},
                station_id=station.station_id,
                provider="open-meteo-archive",
                request_url=archive_persisted.request_url,
                retrieved_at=archive_persisted.retrieved_at,
                raw_path=archive_persisted.raw_path,
                content_sha256=archive_persisted.content_sha256,
            )
            archive_url = archive_persisted.request_url
            archive_raw_path = archive_persisted.raw_path
            archive_hash = archive_persisted.content_sha256
            for row in archive_parsed.daily_maxima:
                if row.local_date == event_date:
                    diagnostic = row.temperature_celsius
                    break
        except ReadOnlyHttpError:
            archive_url = None
        observation = self._observations.observation_max_so_far(
            station=station, event_date=event_date, decision_ts=decision
        )
        cache[key] = {
            "decision_ts": decision,
            "issued_at": parsed.issued_at,
            "available_at": parsed.available_at,
            "forecast_daily_max_c": daily.value_c,
            "valid_times": list(daily.valid_times),
            "forecast_hourly": forecast_hourly,
            "dew_point_c": _mean([row.dew_point_c for row in event_hours]),
            "humidity_pct": _mean([row.humidity_pct for row in event_hours]),
            "cloud_cover_pct": _mean([row.cloud_cover_pct for row in event_hours]),
            "wind_speed": _mean([row.wind_speed for row in event_hours]),
            "wind_direction_deg": _mean([row.wind_direction_deg for row in event_hours]),
            "precipitation": _mean([row.precipitation for row in event_hours]),
            "surface_pressure": _mean([row.surface_pressure for row in event_hours]),
            "diagnostic_actual_max_c": diagnostic,
            "observation_max_so_far_c": observation.max_so_far_c,
            "observation_as_of": observation.as_of,
            "forecast_url": forecast_persisted.request_url,
            "archive_url": archive_url,
            "forecast_raw_path": forecast_persisted.raw_path,
            "archive_raw_path": archive_raw_path,
            "forecast_hash": forecast_persisted.content_sha256,
            "archive_hash": archive_hash,
            "limitations": list(observation.limitations),
        }
        return cache[key]

    def _price_history(
        self,
        token_id: str,
        *,
        decision_ts: datetime,
        options: Phase3CollectOptions,
        raw_root: Path,
    ) -> _PriceHistoryResult:
        end_ts = int(ensure_utc(decision_ts).timestamp())
        start_ts = end_ts - PRICE_HISTORY_LOOKBACK_DAYS * 86400
        response = self._client.price_history(
            token_id,
            start_ts=start_ts,
            end_ts=end_ts,
            fidelity=options.price_fidelity_minutes,
        )
        try:
            payload = _successful_json(response)
        except ReadOnlyHttpError as exc:
            return _PriceHistoryResult(
                points=(),
                persisted=None,
                limitations=(f"{PRICE_HTTP_ERROR_LIMITATION} {exc}",),
                http_error=True,
            )
        schema = validate_prices_history_payload(payload)
        persisted = persist_raw_payload(
            raw_root,
            source="polymarket/clob-prices-history",
            url=response.url,
            payload=payload,
            retrieved_at=self._now(),
        )
        if schema.status not in {"ok", "empty"}:
            return _PriceHistoryResult(
                points=(),
                persisted=persisted,
                limitations=(f"{PRICE_SCHEMA_ERROR_LIMITATION} {schema.detail}",),
                schema_error=True,
            )
        try:
            points = parse_price_history_points(payload)
        except ProviderSchemaError as exc:
            return _PriceHistoryResult(
                points=(),
                persisted=persisted,
                limitations=(f"{PRICE_SCHEMA_ERROR_LIMITATION} {exc}",),
                schema_error=True,
            )
        if points:
            return _PriceHistoryResult(points=points, persisted=persisted)
        return _PriceHistoryResult(
            points=(),
            persisted=persisted,
            limitations=(PRICE_HISTORY_EMPTY_LIMITATION,),
            empty_history=True,
        )

    def _now(self) -> datetime:
        return self._retrieved_at or utc_now()


def _decision_timestamp(event_date: str, timezone_name: str, lead_hours: float) -> datetime:
    year, month, day = (int(part) for part in event_date.split("-"))
    local_start = datetime(year, month, day, 0, 0, tzinfo=ZoneInfo(timezone_name))
    return (local_start - timedelta(hours=lead_hours)).astimezone(UTC)


def _successful_json(response: ReadOnlyResponse) -> Any:
    response.raise_for_status()
    return response.json()


def _hourly_state(point: HourlyForecastPoint) -> HourlyForecastState:
    return HourlyForecastState(
        valid_time_utc=point.valid_time_utc,
        temperature_c=point.temperature_c,
        dew_point_c=point.dew_point_c,
        humidity_pct=point.humidity_pct,
        cloud_cover_pct=point.cloud_cover_pct,
        wind_speed=point.wind_speed,
        wind_direction_deg=point.wind_direction_deg,
        precipitation=point.precipitation,
        surface_pressure=point.surface_pressure,
    )


def _q(
    parsed: ParsedGammaMarket,
    reason: str,
    *,
    token: str | None = None,
) -> QuarantineRecord:
    return QuarantineRecord(
        reason=reason,
        condition_id=parsed.market.condition_id,
        market_id=parsed.market.market_id,
        token_id=token,
        city=parsed.market.city,
        station_icao=parsed.market.station_icao,
        event_date=parsed.market.event_date,
        details=parsed.market.question,
    )


def _mean(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present) / len(present)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
        for row in rows
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
