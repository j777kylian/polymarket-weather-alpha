"""Descriptive/non-executable backtest. Historical asks are not reconstructed."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeAlias

from weather_alpha.research.mispricing import executable_edge, raw_edge
from weather_alpha.research.types import ResearchSnapshot

EDGE_THRESHOLDS: tuple[float, ...] = (0.05, 0.10, 0.15, 0.20)
FIXED_SIZE = 1.0

ValidRow: TypeAlias = tuple[ResearchSnapshot, float, float, float]
BucketKeyFn: TypeAlias = Callable[[ResearchSnapshot, float, float, float], str | None]
GroupKeyFn: TypeAlias = Callable[[ResearchSnapshot, float, float, float], str]

RAW_EDGE_BUCKETS: tuple[str, ...] = (
    "<=0%",
    "0-5%",
    "5-10%",
    "10-15%",
    "15-20%",
    ">20%",
)
ENTRY_PRICE_BUCKETS: tuple[str, ...] = (
    "<1c",
    "1-3c",
    "3-5c",
    "5-10c",
    "10-25c",
    ">25c",
)
LEAD_TIME_BUCKETS: tuple[str, ...] = (
    ">48h",
    "24-48h",
    "12-24h",
    "6-12h",
    "1-6h",
    "<1h",
)


@dataclass(frozen=True, slots=True)
class DescriptiveBucketStats:
    bucket: str
    n: int
    mean_model_probability: float | None
    mean_market_probability: float | None
    mean_raw_edge: float | None
    settled_yes_count: int
    settled_yes_fraction: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "n": self.n,
            "mean_model_probability": self.mean_model_probability,
            "mean_market_probability": self.mean_market_probability,
            "mean_raw_edge": self.mean_raw_edge,
            "settled_yes_count": self.settled_yes_count,
            "settled_yes_fraction": self.settled_yes_fraction,
        }


@dataclass(frozen=True, slots=True)
class DescriptiveMispricingAnalysis:
    """Descriptive test/validation summaries only; not executable results."""

    split_name: str
    valid_candidates: int
    excluded_missing_model_or_market: int
    missing_lead_hours_count: int
    average_model_probability: float | None
    average_market_probability: float | None
    average_raw_edge: float | None
    raw_edge_buckets: tuple[DescriptiveBucketStats, ...]
    entry_price_buckets: tuple[DescriptiveBucketStats, ...]
    lead_time_buckets: tuple[DescriptiveBucketStats, ...]
    by_city: tuple[DescriptiveBucketStats, ...]
    by_station_icao: tuple[DescriptiveBucketStats, ...]
    by_event_month: tuple[DescriptiveBucketStats, ...]
    by_season: tuple[DescriptiveBucketStats, ...]
    by_bucket_region: tuple[DescriptiveBucketStats, ...]
    notes: tuple[str, ...] = (
        "Descriptive validation/test summaries only; not executable fills or alpha.",
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "split_name": self.split_name,
            "valid_candidates": self.valid_candidates,
            "excluded_missing_model_or_market": self.excluded_missing_model_or_market,
            "missing_lead_hours_count": self.missing_lead_hours_count,
            "average_model_probability": self.average_model_probability,
            "average_market_probability": self.average_market_probability,
            "average_raw_edge": self.average_raw_edge,
            "raw_edge_buckets": [row.as_dict() for row in self.raw_edge_buckets],
            "entry_price_buckets": [row.as_dict() for row in self.entry_price_buckets],
            "lead_time_buckets": [row.as_dict() for row in self.lead_time_buckets],
            "by_city": [row.as_dict() for row in self.by_city],
            "by_station_icao": [row.as_dict() for row in self.by_station_icao],
            "by_event_month": [row.as_dict() for row in self.by_event_month],
            "by_season": [row.as_dict() for row in self.by_season],
            "by_bucket_region": [row.as_dict() for row in self.by_bucket_region],
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class BacktestEvaluation:
    status: str
    reason: str
    split_name: str
    candidates: int
    executable_trades: int
    pnl: float | None
    roi: float | None
    max_drawdown: float | None
    profit_factor: float | None
    fees_mode: str
    thresholds: tuple[float, ...]
    selected_threshold: float | None = None
    descriptive_edges: tuple[float, ...] = ()
    threshold_selection_reason: str | None = None
    candidates_by_threshold: dict[float, int] | None = None
    average_descriptive_market_probability: float | None = None
    average_model_probability: float | None = None
    average_raw_edge: float | None = None
    average_executable_entry_price: float | None = None
    win_rate: float | None = None
    descriptive_analysis: DescriptiveMispricingAnalysis | None = None


def classify_raw_edge_bucket(edge: float) -> str:
    """Boundaries: <=0; (0,0.05]; (0.05,0.10]; (0.10,0.15]; (0.15,0.20]; >0.20."""
    if edge <= 0.0:
        return "<=0%"
    if edge <= 0.05:
        return "0-5%"
    if edge <= 0.10:
        return "5-10%"
    if edge <= 0.15:
        return "10-15%"
    if edge <= 0.20:
        return "15-20%"
    return ">20%"


def classify_entry_price_bucket(price: float | None) -> str | None:
    """Descriptive market p buckets. 1c/3c/5c/10c begin their bands; 25c in 10-25c."""
    if price is None:
        return None
    if price < 0.01:
        return "<1c"
    if price < 0.03:
        return "1-3c"
    if price < 0.05:
        return "3-5c"
    if price < 0.10:
        return "5-10c"
    if price <= 0.25:
        return "10-25c"
    return ">25c"


def classify_lead_time_bucket(hours: float | None) -> str | None:
    """Lead-time bands. Exact 1h is 1-6h; exact 6/12/24/48 stay in the lower label."""
    if hours is None:
        return None
    if hours > 48.0:
        return ">48h"
    if hours > 24.0:
        return "24-48h"
    if hours > 12.0:
        return "12-24h"
    if hours > 6.0:
        return "6-12h"
    if hours >= 1.0:
        return "1-6h"
    return "<1h"


def classify_bucket_region(kind: str | None) -> str:
    if kind in {"below", "above"}:
        return "tail"
    if kind in {"exact", "range"}:
        return "center"
    return "unknown"


def classify_season(event_date: str) -> str:
    month = int(event_date[5:7])
    if month in {12, 1, 2}:
        return "winter"
    if month in {3, 4, 5}:
        return "spring"
    if month in {6, 7, 8}:
        return "summer"
    return "autumn"


def analyze_descriptive_mispricing(
    *,
    snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot],
    model_probabilities: dict[str, float],
    split_name: str,
) -> DescriptiveMispricingAnalysis:
    valid: list[ValidRow] = []
    excluded = 0
    missing_lead = 0
    for snapshot in snapshots:
        model_p = model_probabilities.get(snapshot.token_id)
        if model_p is None or snapshot.market_probability is None:
            excluded += 1
            continue
        edge = raw_edge(model_probability=model_p, market_probability=snapshot.market_probability)
        if edge is None:
            excluded += 1
            continue
        if snapshot.forecast_lead_hours is None:
            missing_lead += 1
        valid.append((snapshot, model_p, snapshot.market_probability, edge))

    n_valid = len(valid)
    if n_valid == 0:
        avg_model: float | None = None
        avg_market: float | None = None
        avg_edge: float | None = None
    else:
        avg_model = sum(row[1] for row in valid) / n_valid
        avg_market = sum(row[2] for row in valid) / n_valid
        avg_edge = sum(row[3] for row in valid) / n_valid

    return DescriptiveMispricingAnalysis(
        split_name=split_name,
        valid_candidates=n_valid,
        excluded_missing_model_or_market=excluded,
        missing_lead_hours_count=missing_lead,
        average_model_probability=avg_model,
        average_market_probability=avg_market,
        average_raw_edge=avg_edge,
        raw_edge_buckets=_fixed_bucket_stats(
            labels=RAW_EDGE_BUCKETS,
            rows=valid,
            key=lambda _snap, _model, _market, edge: classify_raw_edge_bucket(edge),
        ),
        entry_price_buckets=_fixed_bucket_stats(
            labels=ENTRY_PRICE_BUCKETS,
            rows=valid,
            key=lambda _snap, _model, market, _edge: classify_entry_price_bucket(market),
        ),
        lead_time_buckets=_fixed_bucket_stats(
            labels=LEAD_TIME_BUCKETS,
            rows=valid,
            key=lambda snap, _model, _market, _edge: classify_lead_time_bucket(
                snap.forecast_lead_hours
            ),
        ),
        by_city=_group_stats(
            rows=valid,
            key=lambda snap, _m, _p, _e: snap.city or "unknown",
        ),
        by_station_icao=_group_stats(
            rows=valid,
            key=lambda snap, _m, _p, _e: snap.station_icao or "unknown",
        ),
        by_event_month=_group_stats(
            rows=valid,
            key=lambda snap, _m, _p, _e: snap.event_date[:7],
        ),
        by_season=_group_stats(
            rows=valid,
            key=lambda snap, _m, _p, _e: classify_season(snap.event_date),
        ),
        by_bucket_region=_group_stats(
            rows=valid,
            key=lambda snap, _m, _p, _e: classify_bucket_region(snap.bucket_kind),
        ),
    )


class DescriptiveBacktester:
    def evaluate(
        self,
        *,
        snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot],
        model_probabilities: dict[str, float],
        thresholds: tuple[float, ...] = EDGE_THRESHOLDS,
        split_name: str,
        selected_threshold: float | None = None,
    ) -> BacktestEvaluation:
        candidates = 0
        descriptive: list[float] = []
        executable = 0
        for snapshot in snapshots:
            model_p = model_probabilities.get(snapshot.token_id)
            if model_p is None or snapshot.market_probability is None:
                continue
            edge = raw_edge(
                model_probability=model_p, market_probability=snapshot.market_probability
            )
            if edge is None:
                continue
            candidates += 1
            descriptive.append(edge)
            exec_edge = executable_edge(model_probability=model_p, executable_ask=snapshot.best_ask)
            if exec_edge is None or snapshot.best_ask is None:
                continue
            if selected_threshold is not None and exec_edge < selected_threshold:
                continue
            executable += 1
        counts = {
            threshold: sum(1 for edge in descriptive if edge >= threshold)
            for threshold in thresholds
        }
        analysis = analyze_descriptive_mispricing(
            snapshots=snapshots,
            model_probabilities=model_probabilities,
            split_name=split_name,
        )
        threshold_reason = (
            "Historical asks are absent; selected_threshold remains null; "
            "validation threshold selection did not occur. Candidate counts by "
            "predeclared threshold are descriptive only."
        )
        if executable == 0:
            return BacktestEvaluation(
                status="non_executable",
                reason=(
                    "CLOB prices-history does not reconstruct historical asks; "
                    "no executable trades; PnL/ROI/drawdown/profit factor remain null"
                ),
                split_name=split_name,
                candidates=candidates,
                executable_trades=0,
                pnl=None,
                roi=None,
                max_drawdown=None,
                profit_factor=None,
                fees_mode="not_applied_non_executable",
                thresholds=thresholds,
                selected_threshold=None,
                descriptive_edges=tuple(descriptive),
                threshold_selection_reason=threshold_reason,
                candidates_by_threshold=counts,
                average_descriptive_market_probability=analysis.average_market_probability,
                average_model_probability=analysis.average_model_probability,
                average_raw_edge=analysis.average_raw_edge,
                average_executable_entry_price=None,
                win_rate=None,
                descriptive_analysis=analysis,
            )
        return BacktestEvaluation(
            status="insufficient_data",
            reason="executable asks were present but fill simulation is out of scope",
            split_name=split_name,
            candidates=candidates,
            executable_trades=executable,
            pnl=None,
            roi=None,
            max_drawdown=None,
            profit_factor=None,
            fees_mode="not_applied_non_executable",
            thresholds=thresholds,
            selected_threshold=None,
            descriptive_edges=tuple(descriptive),
            threshold_selection_reason=(
                "Executable asks were present but fill simulation is out of scope; "
                "selected_threshold remains null."
            ),
            candidates_by_threshold=counts,
            average_descriptive_market_probability=analysis.average_market_probability,
            average_model_probability=analysis.average_model_probability,
            average_raw_edge=analysis.average_raw_edge,
            average_executable_entry_price=None,
            win_rate=None,
            descriptive_analysis=analysis,
        )


def _empty_bucket(label: str) -> DescriptiveBucketStats:
    return DescriptiveBucketStats(
        bucket=label,
        n=0,
        mean_model_probability=None,
        mean_market_probability=None,
        mean_raw_edge=None,
        settled_yes_count=0,
        settled_yes_fraction=None,
    )


def _stats_for_rows(
    label: str,
    rows: list[ValidRow],
) -> DescriptiveBucketStats:
    n = len(rows)
    if n == 0:
        return _empty_bucket(label)
    yes = sum(1 for snap, _m, _p, _e in rows if (snap.settlement_label or "").lower() == "yes")
    return DescriptiveBucketStats(
        bucket=label,
        n=n,
        mean_model_probability=sum(model for _s, model, _p, _e in rows) / n,
        mean_market_probability=sum(market for _s, _m, market, _e in rows) / n,
        mean_raw_edge=sum(edge for _s, _m, _p, edge in rows) / n,
        settled_yes_count=yes,
        settled_yes_fraction=yes / n,
    )


def _fixed_bucket_stats(
    *,
    labels: tuple[str, ...],
    rows: list[ValidRow],
    key: BucketKeyFn,
) -> tuple[DescriptiveBucketStats, ...]:
    grouped: dict[str, list[ValidRow]] = {label: [] for label in labels}
    for row in rows:
        label = key(*row)
        if label is None:
            continue
        grouped[label].append(row)
    return tuple(_stats_for_rows(label, grouped[label]) for label in labels)


def _group_stats(
    *,
    rows: list[ValidRow],
    key: GroupKeyFn,
) -> tuple[DescriptiveBucketStats, ...]:
    grouped: dict[str, list[ValidRow]] = {}
    for row in rows:
        label = key(*row)
        grouped.setdefault(label, []).append(row)
    return tuple(_stats_for_rows(label, grouped[label]) for label in sorted(grouped))
