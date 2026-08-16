"""Interpretable Phase 3 baselines. No sklearn. Train-date labels only."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from weather_alpha.models.units import celsius_to_fahrenheit
from weather_alpha.probability.interfaces import BucketProbability
from weather_alpha.research.split import SplitDates
from weather_alpha.research.types import ResearchSnapshot


def probabilities_sum_to_one(values: tuple[float, ...], *, tol: float = 1e-9) -> bool:
    if not values:
        return False
    if any(value < -tol for value in values):
        return False
    return abs(sum(values) - 1.0) <= tol


def _normalize(weights: list[float]) -> list[float]:
    total = sum(weights)
    if total <= 0:
        n = len(weights)
        return [1.0 / n for _ in weights] if n else []
    return [value / total for value in weights]


def round_to_settlement_degree(value: float) -> int:
    """Nearest whole degree; halves round away from zero (Wunderground whole-degree prints)."""
    if value >= 0:
        return math.floor(value + 0.5)
    return math.ceil(value - 0.5)


_LABEL_UNIT_RE = re.compile(r"°\s*(?P<unit>[CF])\b|(?P<word>Celsius|Fahrenheit)\b", re.IGNORECASE)


def _family_unit(snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot]) -> str | None:
    units = {snap.temperature_unit for snap in snapshots if snap.temperature_unit}
    if len(units) == 1:
        return next(iter(units))
    inferred: set[str] = set()
    for snap in snapshots:
        label = snap.bucket_label or ""
        match = _LABEL_UNIT_RE.search(label)
        if match is None:
            continue
        token = (match.group("unit") or match.group("word") or "").upper()
        if token.startswith("F"):
            inferred.add("F")
        elif token.startswith("C"):
            inferred.add("C")
    if len(inferred) == 1:
        return next(iter(inferred))
    return None


def _native_bounds(snapshot: ResearchSnapshot) -> tuple[float | None, float | None]:
    if snapshot.temperature_native_min is not None or snapshot.temperature_native_max is not None:
        return snapshot.temperature_native_min, snapshot.temperature_native_max
    label = snapshot.bucket_label or ""
    match = re.search(
        r"(?P<a>[+-]?\d+(?:\.\d+)?)\s*[\-\u2013]\s*(?P<b>[+-]?\d+(?:\.\d+)?)",
        label,
    )
    if match:
        return float(match.group("a")), float(match.group("b"))
    match = re.search(r"(?P<a>[+-]?\d+(?:\.\d+)?)", label)
    if match is None:
        return None, None
    value = float(match.group("a"))
    kind = snapshot.bucket_kind
    if kind == "below":
        return None, value
    if kind == "above":
        return value, None
    return value, value


@dataclass(frozen=True, slots=True)
class ModelFeatures:
    station_icao: str | None
    city: str | None
    event_date: str
    forecast_daily_max_c: float | None
    dew_point_c: float | None
    humidity_pct: float | None
    cloud_cover_pct: float | None
    wind_speed: float | None
    observation_max_so_far_c: float | None
    forecast_lead_hours: float | None
    month: int | None

    @classmethod
    def from_snapshot(cls, snapshot: ResearchSnapshot) -> ModelFeatures:
        month = int(snapshot.event_date[5:7]) if len(snapshot.event_date) >= 7 else None
        return cls(
            station_icao=snapshot.station_icao,
            city=snapshot.city,
            event_date=snapshot.event_date,
            forecast_daily_max_c=snapshot.forecast_daily_max_c,
            dew_point_c=snapshot.dew_point_c,
            humidity_pct=snapshot.humidity_pct,
            cloud_cover_pct=snapshot.cloud_cover_pct,
            wind_speed=snapshot.wind_speed,
            observation_max_so_far_c=snapshot.observation_max_so_far_c,
            forecast_lead_hours=snapshot.forecast_lead_hours,
            month=month,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "station_icao": self.station_icao,
            "city": self.city,
            "event_date": self.event_date,
            "forecast_daily_max_c": self.forecast_daily_max_c,
            "dew_point_c": self.dew_point_c,
            "humidity_pct": self.humidity_pct,
            "cloud_cover_pct": self.cloud_cover_pct,
            "wind_speed": self.wind_speed,
            "observation_max_so_far_c": self.observation_max_so_far_c,
            "forecast_lead_hours": self.forecast_lead_hours,
            "month": self.month,
        }


class HistoricalFrequencyBaseline:
    """P(bucket wins | station, month) with city then global fallback."""

    def __init__(self) -> None:
        self._station_month: dict[tuple[str, int], Counter[str]] = defaultdict(Counter)
        self._station: dict[str, Counter[str]] = defaultdict(Counter)
        self._city: dict[str, Counter[str]] = defaultdict(Counter)
        self._global: Counter[str] = Counter()
        self._fitted = False

    def fit(self, snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot]) -> None:
        self._station_month.clear()
        self._station.clear()
        self._city.clear()
        self._global.clear()
        seen_events: set[tuple[str, str | None, str]] = set()
        for snapshot in snapshots:
            if snapshot.settlement_label is None or snapshot.settlement_label.lower() != "yes":
                continue
            key = (snapshot.event_date, snapshot.station_icao, snapshot.bucket_label or "")
            if key in seen_events:
                continue
            seen_events.add(key)
            label = snapshot.bucket_label or ""
            month = int(snapshot.event_date[5:7]) if len(snapshot.event_date) >= 7 else None
            if snapshot.station_icao and month is not None:
                self._station_month[(snapshot.station_icao, month)][label] += 1
            if snapshot.station_icao:
                self._station[snapshot.station_icao][label] += 1
            if snapshot.city:
                self._city[snapshot.city][label] += 1
            self._global[label] += 1
        self._fitted = True

    def predict_event(
        self, snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot]
    ) -> tuple[BucketProbability, ...]:
        if not snapshots:
            return ()
        labels = [snap.bucket_label or f"bucket-{index}" for index, snap in enumerate(snapshots)]
        first = snapshots[0]
        month = int(first.event_date[5:7]) if len(first.event_date) >= 7 else None
        counts = self._lookup(first.station_icao, first.city, month)
        weights = [float(counts.get(label, 0)) for label in labels]
        if sum(weights) <= 0:
            weights = [1.0 for _ in labels]
        probs = _normalize(weights)
        return tuple(
            BucketProbability(label=label, probability=prob)
            for label, prob in zip(labels, probs, strict=True)
        )

    def _lookup(self, station: str | None, city: str | None, month: int | None) -> Counter[str]:
        if station and month is not None and self._station_month.get((station, month)):
            return self._station_month[(station, month)]
        if station and self._station.get(station):
            return self._station[station]
        if city and self._city.get(city):
            return self._city[city]
        return self._global


class ForecastErrorBucketModel:
    """Empirical forecast-error distribution around predicted daily max."""

    def __init__(self) -> None:
        self._station_errors: dict[str, list[float]] = defaultdict(list)
        self._global_errors: list[float] = []
        self._fitted = False

    def fit(
        self,
        snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot],
        *,
        split: SplitDates | None = None,
    ) -> None:
        train_dates = set(split.train) if split is not None else None
        self._station_errors.clear()
        self._global_errors.clear()
        seen: set[tuple[str, str | None]] = set()
        for snapshot in snapshots:
            if train_dates is not None and snapshot.event_date not in train_dates:
                continue
            if snapshot.forecast_daily_max_c is None or snapshot.diagnostic_actual_max_c is None:
                continue
            event_key = (snapshot.event_date, snapshot.station_icao)
            if event_key in seen:
                continue
            seen.add(event_key)
            error = snapshot.diagnostic_actual_max_c - snapshot.forecast_daily_max_c
            if snapshot.station_icao:
                self._station_errors[snapshot.station_icao].append(error)
            self._global_errors.append(error)
        self._fitted = True

    def predict_event(
        self, snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot]
    ) -> tuple[BucketProbability, ...]:
        if not snapshots:
            return ()
        first = snapshots[0]
        errors = list(self._station_errors.get(first.station_icao or "", []))
        if not errors:
            errors = list(self._global_errors)
        forecast = first.forecast_daily_max_c
        labels = [snap.bucket_label or f"bucket-{index}" for index, snap in enumerate(snapshots)]
        if forecast is None or not errors:
            weights = [1.0 for _ in snapshots]
            probs = _normalize(weights)
            return tuple(
                BucketProbability(label=label, probability=prob)
                for label, prob in zip(labels, probs, strict=True)
            )
        weights = [0.0 for _ in snapshots]
        for error in errors:
            implied = forecast + error
            index = _matching_bucket_index(snapshots, implied)
            if index is not None:
                weights[index] += 1.0
        probs = _normalize(weights)
        return tuple(
            BucketProbability(label=label, probability=prob)
            for label, prob in zip(labels, probs, strict=True)
        )


def match_settlement_bucket(
    snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot],
    value_c: float,
) -> str | None:
    index = _matching_bucket_index(snapshots, value_c)
    if index is None:
        return None
    return snapshots[index].bucket_label


def _matching_bucket_index(
    snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot],
    value_c: float,
) -> int | None:
    unit = _family_unit(snapshots)
    if unit == "F":
        settlement = round_to_settlement_degree(celsius_to_fahrenheit(value_c))
        return _match_native_integer(snapshots, settlement)
    return _match_celsius_continuous(snapshots, value_c)


def _match_native_integer(
    snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot],
    settlement: int,
) -> int | None:
    exact_hits: list[int] = []
    range_hits: list[int] = []
    below_hits: list[int] = []
    above_hits: list[int] = []
    for index, snapshot in enumerate(snapshots):
        kind = snapshot.bucket_kind
        lo, hi = _native_bounds(snapshot)
        if kind == "exact" and lo is not None and settlement == int(round_to_settlement_degree(lo)):
            exact_hits.append(index)
        elif kind == "range" and lo is not None and hi is not None and lo <= settlement <= hi:
            range_hits.append(index)
        elif kind == "below" and hi is not None and settlement <= hi:
            below_hits.append(index)
        elif kind == "above" and lo is not None and settlement >= lo:
            above_hits.append(index)
        elif kind is None and lo is not None and hi is not None and lo <= settlement <= hi:
            range_hits.append(index)
    for group in (exact_hits, range_hits, below_hits, above_hits):
        if len(group) == 1:
            return group[0]
        if len(group) > 1:
            return group[0]
    return None


def _match_celsius_continuous(
    snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot],
    value_c: float,
) -> int | None:
    exact_hits: list[int] = []
    range_hits: list[int] = []
    below_hits: list[int] = []
    above_hits: list[int] = []
    for index, snapshot in enumerate(snapshots):
        kind = snapshot.bucket_kind
        lo = snapshot.temperature_celsius_min
        hi = snapshot.temperature_celsius_max
        if kind == "exact" and lo is not None and abs(value_c - lo) < 0.5:
            exact_hits.append(index)
        elif kind == "range" and lo is not None and hi is not None and lo <= value_c <= hi:
            range_hits.append(index)
        elif kind == "below" and hi is not None and value_c <= hi:
            below_hits.append(index)
        elif kind == "above" and lo is not None and value_c >= lo:
            above_hits.append(index)
        elif kind is None and lo is not None and hi is not None and lo <= value_c <= hi:
            range_hits.append(index)
    for group in (exact_hits, range_hits, below_hits, above_hits):
        if len(group) == 1:
            return group[0]
        if len(group) > 1:
            return group[0]
    return None
