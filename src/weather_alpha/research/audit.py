"""Dataset audit: coverage, missingness, duplicates, timestamp checks."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from weather_alpha.research.types import QuarantineRecord, ResearchSnapshot, snapshot_dedup_key


@dataclass(frozen=True, slots=True)
class DatasetAudit:
    markets: int
    snapshots: int
    field_missingness: dict[str, int]
    exclusions: dict[str, int]
    city_coverage: dict[str, int]
    station_coverage: dict[str, int]
    date_coverage: dict[str, int]
    duplicates: int
    timestamp_violations: tuple[str, ...]
    notes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "markets": self.markets,
            "snapshots": self.snapshots,
            "field_missingness": dict(sorted(self.field_missingness.items())),
            "exclusions": dict(sorted(self.exclusions.items())),
            "city_coverage": dict(sorted(self.city_coverage.items())),
            "station_coverage": dict(sorted(self.station_coverage.items())),
            "date_coverage": dict(sorted(self.date_coverage.items())),
            "duplicates": self.duplicates,
            "timestamp_violations": list(self.timestamp_violations),
            "notes": list(self.notes),
        }


TRACKED_FIELDS = (
    "market_probability",
    "forecast_daily_max_c",
    "settlement_label",
    "diagnostic_actual_max_c",
    "best_ask",
    "executable_entry_price",
    "observation_max_so_far_c",
    "dew_point_c",
    "humidity_pct",
    "cloud_cover_pct",
    "wind_speed",
    "precipitation",
    "surface_pressure",
)


def audit_dataset(
    snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot],
    *,
    quarantined: tuple[QuarantineRecord, ...] | list[QuarantineRecord] = (),
) -> DatasetAudit:
    missing: dict[str, int] = {name: 0 for name in TRACKED_FIELDS}
    cities: Counter[str] = Counter()
    stations: Counter[str] = Counter()
    dates: Counter[str] = Counter()
    markets = {snap.condition_id for snap in snapshots}
    keys: Counter[str] = Counter()
    violations: list[str] = []
    for snapshot in snapshots:
        keys[snapshot_dedup_key(snapshot)] += 1
        cities[snapshot.city or "unknown"] += 1
        stations[snapshot.station_icao or "unknown"] += 1
        dates[snapshot.event_date] += 1
        for name in TRACKED_FIELDS:
            if getattr(snapshot, name) is None:
                missing[name] += 1
        if (
            snapshot.weather_available_at is not None
            and snapshot.weather_available_at > snapshot.decision_ts
        ):
            violations.append(f"{snapshot.token_id}: available_at after decision")
        if (
            snapshot.weather_issued_at is not None
            and snapshot.weather_issued_at > snapshot.decision_ts
        ):
            violations.append(f"{snapshot.token_id}: issued_at after decision")
        if (
            snapshot.observation_as_of is not None
            and snapshot.observation_as_of > snapshot.decision_ts
        ):
            violations.append(f"{snapshot.token_id}: observation after decision")
        if (
            snapshot.market_price_observed_at is not None
            and snapshot.market_price_observed_at > snapshot.decision_ts
        ):
            violations.append(f"{snapshot.token_id}: market_price_observed_at after decision")
        if snapshot.best_ask is not None or snapshot.executable_entry_price is not None:
            violations.append(
                f"{snapshot.token_id}: unexpected executable price from price-history-only sources"
            )
    exclusions: Counter[str] = Counter(row.reason for row in quarantined)
    duplicates = sum(1 for count in keys.values() if count > 1)
    notes = (
        "best_ask/executable_entry_price missingness is expected: historical books are unavailable.",
        "diagnostic_actual_max_c is Open-Meteo Archive, not settlement.",
        "Settlement labels are not decision-time features.",
        "Open-Meteo Archive hourly rows are not decision-time observation features.",
    )
    return DatasetAudit(
        markets=len(markets),
        snapshots=len(snapshots),
        field_missingness=dict(missing),
        exclusions=dict(exclusions),
        city_coverage=dict(cities),
        station_coverage=dict(stations),
        date_coverage=dict(dates),
        duplicates=duplicates,
        timestamp_violations=tuple(sorted(violations)),
        notes=notes,
    )
