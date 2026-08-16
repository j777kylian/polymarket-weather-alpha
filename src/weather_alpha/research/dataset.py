"""Assemble and persist Phase 3 research snapshots. No lookahead."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from weather_alpha.models.timeutil import ensure_utc, parse_timestamp
from weather_alpha.research.observations import ARCHIVE_NOT_DECISION_TIME_LIMITATION
from weather_alpha.research.prices import PricePoint
from weather_alpha.research.types import (
    HourlyForecastState,
    QuarantineRecord,
    ResearchSnapshot,
    snapshot_dedup_key,
)

PRICE_HISTORY_LIMITATION = (
    "CLOB GET /prices-history returns {t,p} only; p is descriptive market_probability. "
    "executable_entry_price/best_bid/best_ask/midpoint/spread remain null because official "
    "public APIs do not reconstruct historical order books."
)
ARCHIVE_DIAGNOSTIC_LIMITATION = (
    "Open-Meteo Archive daily max is a diagnostic grid/reanalysis value, not the "
    "Wunderground station print used for settlement."
)
SETTLEMENT_LABEL_LIMITATION = (
    "Gamma resolved outcomePrices are settlement labels only and are not decision-time features."
)
SINGLE_RUN_LIMITATION = (
    "Open-Meteo Single Runs issued_at is run initialization; available_at is run+6h conservatively."
)


class LookaheadError(ValueError):
    """Raised when a candidate snapshot would leak information after decision_ts."""


@dataclass(frozen=True, slots=True)
class DatasetWriteResult:
    parquet_path: Path
    row_count: int


def assemble_snapshot(
    *,
    condition_id: str,
    market_id: str | None,
    token_id: str,
    city: str | None,
    station_icao: str | None,
    event_date: str,
    bucket_label: str | None,
    bucket_kind: str | None,
    temperature_celsius_min: float | None,
    temperature_celsius_max: float | None,
    decision_ts: datetime,
    settlement_label: str | None,
    diagnostic_actual_max_c: float | None,
    provenance_urls: tuple[str, ...],
    raw_paths: tuple[str, ...],
    content_hashes: tuple[str, ...],
    weather_issued_at: datetime | None,
    weather_available_at: datetime | None,
    forecast_daily_max_c: float | None,
    price: PricePoint | None,
    observation_max_so_far_c: float | None = None,
    observation_as_of: datetime | None = None,
    event_id: str | None = None,
    question: str | None = None,
    group_item_title: str | None = None,
    slug: str | None = None,
    event_slug: str | None = None,
    neg_risk_market_id: str | None = None,
    temperature_unit: str | None = None,
    temperature_native_min: float | None = None,
    temperature_native_max: float | None = None,
    weather_valid_times: tuple[datetime, ...] = (),
    dew_point_c: float | None = None,
    humidity_pct: float | None = None,
    cloud_cover_pct: float | None = None,
    wind_speed: float | None = None,
    wind_direction_deg: float | None = None,
    precipitation: float | None = None,
    surface_pressure: float | None = None,
    forecast_lead_hours: float | None = None,
    extra_limitations: tuple[str, ...] = (),
    price_request_url: str | None = None,
    price_raw_path: str | None = None,
    price_content_sha256: str | None = None,
    forecast_hourly: tuple[HourlyForecastState, ...] = (),
    canonical_event_key: tuple[str, ...] | None = None,
    canonical_event_source: str | None = None,
    canonical_event_evidence: tuple[str, ...] = (),
    canonical_event_ambiguous: bool = False,
    canonical_event_quarantine_reason: str | None = None,
) -> ResearchSnapshot:
    decision = ensure_utc(decision_ts)
    issued = None if weather_issued_at is None else ensure_utc(weather_issued_at)
    available = None if weather_available_at is None else ensure_utc(weather_available_at)
    obs_at = None if observation_as_of is None else ensure_utc(observation_as_of)
    if issued is not None and issued > decision:
        raise LookaheadError("weather issued_at is after decision_ts")
    if available is not None and available > decision:
        raise LookaheadError("weather available_at is after decision_ts; run not yet public")
    if issued is not None and available is not None and available < issued:
        raise LookaheadError("weather available_at precedes issued_at")
    if price is not None and price.observed_at > decision:
        raise LookaheadError("price observed_at is after decision_ts")
    if obs_at is not None and obs_at > decision:
        raise LookaheadError("observation_as_of is after decision_ts")
    if observation_max_so_far_c is not None and obs_at is None:
        raise LookaheadError("observation_max_so_far_c requires observation_as_of <= decision_ts")
    market_p = None if price is None else price.price
    price_at = None if price is None else ensure_utc(price.observed_at)
    hourly = tuple(sorted(forecast_hourly, key=lambda item: item.valid_time_utc.isoformat()))
    limitations = (
        PRICE_HISTORY_LIMITATION,
        ARCHIVE_DIAGNOSTIC_LIMITATION,
        ARCHIVE_NOT_DECISION_TIME_LIMITATION,
        SETTLEMENT_LABEL_LIMITATION,
        SINGLE_RUN_LIMITATION,
        *extra_limitations,
    )
    return ResearchSnapshot(
        condition_id=condition_id,
        market_id=market_id,
        token_id=token_id,
        city=city,
        station_icao=station_icao,
        event_date=event_date,
        bucket_label=bucket_label,
        bucket_kind=bucket_kind,
        temperature_celsius_min=temperature_celsius_min,
        temperature_celsius_max=temperature_celsius_max,
        decision_ts=decision,
        market_probability=market_p,
        executable_entry_price=None,
        best_bid=None,
        best_ask=None,
        midpoint=None,
        spread=None,
        volume=None,
        liquidity=None,
        weather_issued_at=issued,
        weather_available_at=available,
        forecast_daily_max_c=forecast_daily_max_c,
        observation_max_so_far_c=observation_max_so_far_c,
        observation_as_of=obs_at,
        settlement_label=settlement_label,
        diagnostic_actual_max_c=diagnostic_actual_max_c,
        provenance_urls=provenance_urls,
        raw_paths=raw_paths,
        content_hashes=content_hashes,
        limitations=limitations,
        event_id=event_id,
        question=question,
        group_item_title=group_item_title,
        slug=slug,
        event_slug=event_slug,
        neg_risk_market_id=neg_risk_market_id,
        temperature_unit=temperature_unit,
        temperature_native_min=temperature_native_min,
        temperature_native_max=temperature_native_max,
        weather_valid_times=tuple(ensure_utc(ts) for ts in weather_valid_times),
        dew_point_c=dew_point_c,
        humidity_pct=humidity_pct,
        cloud_cover_pct=cloud_cover_pct,
        wind_speed=wind_speed,
        wind_direction_deg=wind_direction_deg,
        precipitation=precipitation,
        surface_pressure=surface_pressure,
        forecast_lead_hours=forecast_lead_hours,
        source_station_icao=station_icao,
        market_price_observed_at=price_at,
        price_request_url=price_request_url,
        price_raw_path=price_raw_path,
        price_content_sha256=price_content_sha256,
        forecast_hourly=hourly,
        canonical_event_key=canonical_event_key,
        canonical_event_source=canonical_event_source,
        canonical_event_evidence=canonical_event_evidence,
        canonical_event_ambiguous=canonical_event_ambiguous,
        canonical_event_quarantine_reason=canonical_event_quarantine_reason,
    )


def dedupe_snapshots(
    snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot],
) -> tuple[tuple[ResearchSnapshot, ...], tuple[QuarantineRecord, ...]]:
    seen: set[str] = set()
    unique: list[ResearchSnapshot] = []
    quarantined: list[QuarantineRecord] = []
    for snapshot in snapshots:
        key = snapshot_dedup_key(snapshot)
        if key in seen:
            quarantined.append(
                QuarantineRecord(
                    reason="duplicate_snapshot",
                    condition_id=snapshot.condition_id,
                    market_id=snapshot.market_id,
                    token_id=snapshot.token_id,
                    city=snapshot.city,
                    station_icao=snapshot.station_icao,
                    event_date=snapshot.event_date,
                    details=key,
                )
            )
            continue
        seen.add(key)
        unique.append(snapshot)
    return tuple(unique), tuple(quarantined)


def snapshot_to_row(snapshot: ResearchSnapshot) -> dict[str, Any]:
    row = asdict(snapshot)
    row["decision_ts"] = snapshot.decision_ts.isoformat()
    row["weather_issued_at"] = _iso(snapshot.weather_issued_at)
    row["weather_available_at"] = _iso(snapshot.weather_available_at)
    row["observation_as_of"] = _iso(snapshot.observation_as_of)
    row["market_price_observed_at"] = _iso(snapshot.market_price_observed_at)
    row["weather_valid_times"] = [_iso(ts) for ts in snapshot.weather_valid_times]
    row["forecast_hourly"] = [hour.to_json_obj() for hour in snapshot.forecast_hourly]
    row["provenance_urls"] = list(snapshot.provenance_urls)
    row["raw_paths"] = list(snapshot.raw_paths)
    row["content_hashes"] = list(snapshot.content_hashes)
    row["limitations"] = list(snapshot.limitations)
    row["canonical_event_key"] = (
        None if snapshot.canonical_event_key is None else list(snapshot.canonical_event_key)
    )
    row["canonical_event_evidence"] = list(snapshot.canonical_event_evidence)
    row["dedup_key"] = snapshot_dedup_key(snapshot)
    return row


def write_snapshots_parquet(
    path: Path,
    snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot],
) -> DatasetWriteResult:
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [snapshot_to_row(item) for item in snapshots]
    rows.sort(key=lambda row: str(row["dedup_key"]))
    con = duckdb.connect(database=":memory:")
    try:
        con.execute("CREATE TABLE snapshots (payload JSON)")
        if rows:
            import json

            encoded = [(json.dumps(row, sort_keys=True, separators=(",", ":")),) for row in rows]
            con.executemany("INSERT INTO snapshots VALUES (?)", encoded)
            con.execute(
                f"""
                COPY (
                    SELECT payload FROM snapshots ORDER BY payload
                ) TO '{path.as_posix()}' (FORMAT PARQUET, COMPRESSION 'uncompressed')
                """
            )
        else:
            con.execute("CREATE TABLE empty_snapshots (dedup_key VARCHAR)")
            con.execute(
                f"COPY empty_snapshots TO '{path.as_posix()}' "
                "(FORMAT PARQUET, COMPRESSION 'uncompressed')"
            )
    finally:
        con.close()
    return DatasetWriteResult(parquet_path=path, row_count=len(rows))


def read_snapshots_parquet(path: Path) -> tuple[dict[str, Any], ...]:
    import json

    import duckdb

    con = duckdb.connect(database=":memory:")
    try:
        relation = con.execute(
            f"SELECT payload FROM read_parquet('{path.as_posix()}') ORDER BY payload"
        )
        fetched = relation.fetchall()
    finally:
        con.close()
    rows: list[dict[str, Any]] = []
    for (payload,) in fetched:
        if isinstance(payload, dict):
            rows.append(payload)
        else:
            rows.append(json.loads(payload))
    return tuple(rows)


def row_to_snapshot(row: dict[str, Any]) -> ResearchSnapshot:
    hourly_raw = row.get("forecast_hourly") or ()
    hourly = tuple(HourlyForecastState.from_json_obj(item) for item in hourly_raw)
    valid_raw = row.get("weather_valid_times") or ()
    return ResearchSnapshot(
        condition_id=str(row["condition_id"]),
        market_id=_opt_str(row.get("market_id")),
        token_id=str(row["token_id"]),
        city=_opt_str(row.get("city")),
        station_icao=_opt_str(row.get("station_icao")),
        event_date=str(row["event_date"]),
        bucket_label=_opt_str(row.get("bucket_label")),
        bucket_kind=_opt_str(row.get("bucket_kind")),
        temperature_celsius_min=_opt_float(row.get("temperature_celsius_min")),
        temperature_celsius_max=_opt_float(row.get("temperature_celsius_max")),
        decision_ts=parse_timestamp(row["decision_ts"]),
        market_probability=_opt_float(row.get("market_probability")),
        executable_entry_price=_opt_float(row.get("executable_entry_price")),
        best_bid=_opt_float(row.get("best_bid")),
        best_ask=_opt_float(row.get("best_ask")),
        midpoint=_opt_float(row.get("midpoint")),
        spread=_opt_float(row.get("spread")),
        volume=_opt_float(row.get("volume")),
        liquidity=_opt_float(row.get("liquidity")),
        weather_issued_at=_opt_ts(row.get("weather_issued_at")),
        weather_available_at=_opt_ts(row.get("weather_available_at")),
        forecast_daily_max_c=_opt_float(row.get("forecast_daily_max_c")),
        observation_max_so_far_c=_opt_float(row.get("observation_max_so_far_c")),
        observation_as_of=_opt_ts(row.get("observation_as_of")),
        settlement_label=_opt_str(row.get("settlement_label")),
        diagnostic_actual_max_c=_opt_float(row.get("diagnostic_actual_max_c")),
        provenance_urls=tuple(row.get("provenance_urls") or ()),
        raw_paths=tuple(row.get("raw_paths") or ()),
        content_hashes=tuple(row.get("content_hashes") or ()),
        limitations=tuple(row.get("limitations") or ()),
        event_id=_opt_str(row.get("event_id")),
        question=_opt_str(row.get("question")),
        group_item_title=_opt_str(row.get("group_item_title")),
        slug=_opt_str(row.get("slug")),
        event_slug=_opt_str(row.get("event_slug")),
        neg_risk_market_id=_opt_str(row.get("neg_risk_market_id")),
        temperature_unit=_opt_str(row.get("temperature_unit")),
        temperature_native_min=_opt_float(row.get("temperature_native_min")),
        temperature_native_max=_opt_float(row.get("temperature_native_max")),
        weather_valid_times=tuple(parse_timestamp(ts) for ts in valid_raw if ts),
        dew_point_c=_opt_float(row.get("dew_point_c")),
        humidity_pct=_opt_float(row.get("humidity_pct")),
        cloud_cover_pct=_opt_float(row.get("cloud_cover_pct")),
        wind_speed=_opt_float(row.get("wind_speed")),
        wind_direction_deg=_opt_float(row.get("wind_direction_deg")),
        precipitation=_opt_float(row.get("precipitation")),
        surface_pressure=_opt_float(row.get("surface_pressure")),
        forecast_lead_hours=_opt_float(row.get("forecast_lead_hours")),
        source_station_icao=_opt_str(row.get("source_station_icao")),
        market_price_observed_at=_opt_ts(row.get("market_price_observed_at")),
        price_request_url=_opt_str(row.get("price_request_url")),
        price_raw_path=_opt_str(row.get("price_raw_path")),
        price_content_sha256=_opt_str(row.get("price_content_sha256")),
        forecast_hourly=hourly,
        canonical_event_key=_opt_str_tuple(row.get("canonical_event_key")),
        canonical_event_source=_opt_str(row.get("canonical_event_source")),
        canonical_event_evidence=tuple(row.get("canonical_event_evidence") or ()),
        canonical_event_ambiguous=bool(row.get("canonical_event_ambiguous") or False),
        canonical_event_quarantine_reason=_opt_str(row.get("canonical_event_quarantine_reason")),
    )


def _iso(value: datetime | None) -> str | None:
    return None if value is None else ensure_utc(value).isoformat()


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_str_tuple(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return None


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _opt_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return parse_timestamp(value)
