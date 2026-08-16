"""Event-level blocked bootstrap and robustness utilities for Phase 3.5.

Canonical event group is the independent inference/blocking unit.
No unsupported alpha conclusions.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from weather_alpha.phase35.bands import calendar_month
from weather_alpha.phase35.config import Phase35AcceptanceThresholds
from weather_alpha.research.backtest import classify_season

AlphaClaimStatus = Literal[
    "insufficient_sample", "no_alpha_conclusion", "thresholds_met_descriptive"
]


@dataclass(frozen=True, slots=True)
class EventGroupMetric:
    """One inference block (canonical event group), not a single bucket market."""

    canonical_event_key: tuple[str, ...]
    event_date: str
    city: str | None
    station: str | None
    lead_hours: int
    metric: float
    favorable: bool = False
    is_tail_winner: bool = False


@dataclass(frozen=True, slots=True)
class AcceptanceAssessment:
    status: AlphaClaimStatus
    held_out_event_groups: int
    held_out_by_lead: dict[str, int]
    cities: tuple[str, ...]
    seasons: tuple[str, ...]
    thresholds: Phase35AcceptanceThresholds
    reasons: tuple[str, ...]
    alpha_claimed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "alpha_claimed": self.alpha_claimed,
            "cities": list(self.cities),
            "held_out_by_lead": dict(sorted(self.held_out_by_lead.items())),
            "held_out_event_groups": self.held_out_event_groups,
            "reasons": list(self.reasons),
            "seasons": list(self.seasons),
            "status": self.status,
            "thresholds": {
                "min_cities": self.thresholds.min_cities,
                "min_held_out_event_groups": self.thresholds.min_held_out_event_groups,
                "min_held_out_per_lead": self.thresholds.min_held_out_per_lead,
                "min_seasons": self.thresholds.min_seasons,
            },
        }


def assess_acceptance_thresholds(
    groups: Sequence[EventGroupMetric],
    *,
    thresholds: Phase35AcceptanceThresholds | None = None,
    interpreted_leads: Sequence[int] | None = None,
) -> AcceptanceAssessment:
    cfg = thresholds or Phase35AcceptanceThresholds()
    leads = tuple(interpreted_leads) if interpreted_leads is not None else ()
    by_lead: dict[str, int] = defaultdict(int)
    cities: set[str] = set()
    seasons: set[str] = set()
    keys: set[tuple[str, ...]] = set()
    for row in groups:
        keys.add(row.canonical_event_key)
        by_lead[f"{row.lead_hours}h"] += 1
        if row.city:
            cities.add(row.city)
        seasons.add(classify_season(row.event_date))

    reasons: list[str] = []
    n = len(keys)
    if n < cfg.min_held_out_event_groups:
        reasons.append(f"held_out_event_groups={n} < required {cfg.min_held_out_event_groups}")
    if len(cities) < cfg.min_cities:
        reasons.append(f"cities={len(cities)} < required {cfg.min_cities}")
    if len(seasons) < cfg.min_seasons:
        reasons.append(f"seasons={len(seasons)} < required {cfg.min_seasons}")
    for lead in leads:
        label = f"{lead}h"
        count = by_lead.get(label, 0)
        if count < cfg.min_held_out_per_lead:
            reasons.append(f"lead {label} held_out={count} < required {cfg.min_held_out_per_lead}")

    if reasons:
        status: AlphaClaimStatus = "insufficient_sample"
    else:
        status = "thresholds_met_descriptive"
    return AcceptanceAssessment(
        status=status,
        held_out_event_groups=n,
        held_out_by_lead=dict(by_lead),
        cities=tuple(sorted(cities)),
        seasons=tuple(sorted(seasons)),
        thresholds=cfg,
        reasons=tuple(reasons),
        alpha_claimed=False,
    )


def blocked_bootstrap_mean_ci(
    groups: Sequence[EventGroupMetric],
    *,
    n_boot: int = 200,
    seed: int = 0,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Event-group blocked bootstrap over group metrics. Deterministic LCG seed."""
    values = [row.metric for row in groups]
    n = len(values)
    if n == 0:
        return {
            "ci_high": None,
            "ci_low": None,
            "mean": None,
            "n_boot": n_boot,
            "n_groups": 0,
            "status": "empty",
        }
    observed = sum(values) / n
    state = seed & 0xFFFFFFFF

    def _rand() -> float:
        nonlocal state
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        return state / 0x100000000

    samples: list[float] = []
    for _ in range(n_boot):
        draw = [values[int(_rand() * n) % n] for _ in range(n)]
        samples.append(sum(draw) / n)
    samples.sort()
    lo_idx = max(0, int(alpha / 2 * n_boot))
    hi_idx = min(n_boot - 1, int((1 - alpha / 2) * n_boot))
    return {
        "ci_high": samples[hi_idx],
        "ci_low": samples[lo_idx],
        "mean": observed,
        "n_boot": n_boot,
        "n_groups": n,
        "status": "ok",
    }


def chronological_walk_forward(
    groups: Sequence[EventGroupMetric],
    *,
    min_train_groups: int = 10,
) -> tuple[dict[str, Any], ...]:
    ordered = sorted(groups, key=lambda row: (row.event_date, row.canonical_event_key))
    folds: list[dict[str, Any]] = []
    for i in range(min_train_groups, len(ordered)):
        train = ordered[:i]
        test = ordered[i]
        train_mean = sum(row.metric for row in train) / len(train)
        folds.append(
            {
                "test_event_date": test.event_date,
                "test_key": list(test.canonical_event_key),
                "test_metric": test.metric,
                "train_groups": len(train),
                "train_mean_metric": train_mean,
            }
        )
    return tuple(folds)


def slice_metrics(
    groups: Sequence[EventGroupMetric],
    *,
    key_fn: Callable[[EventGroupMetric], str],
) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for row in groups:
        buckets[key_fn(row)].append(row.metric)
    return {
        name: {
            "count": len(vals),
            "mean": (sum(vals) / len(vals)) if vals else None,
        }
        for name, vals in sorted(buckets.items())
    }


def robustness_report(groups: Sequence[EventGroupMetric]) -> dict[str, Any]:
    ordered = sorted(groups, key=lambda row: row.metric, reverse=True)
    favorable = [row for row in ordered if row.favorable]
    tails = [row for row in ordered if row.is_tail_winner]

    def _without(drop: Sequence[EventGroupMetric]) -> float | None:
        remaining = [row for row in groups if row not in set(drop)]
        if not remaining:
            return None
        return sum(row.metric for row in remaining) / len(remaining)

    return {
        "by_city": slice_metrics(groups, key_fn=lambda row: row.city or "unknown"),
        "by_lead": slice_metrics(groups, key_fn=lambda row: f"{row.lead_hours}h"),
        "by_month": slice_metrics(groups, key_fn=lambda row: calendar_month(row.event_date)),
        "by_season": slice_metrics(groups, key_fn=lambda row: classify_season(row.event_date)),
        "by_station": slice_metrics(groups, key_fn=lambda row: row.station or "unknown"),
        "chronological_walk_forward_folds": list(
            chronological_walk_forward(groups, min_train_groups=max(1, min(5, len(groups) // 2)))
        ),
        "remove_largest_1_favorable_mean": _without(favorable[:1]),
        "remove_largest_3_favorable_mean": _without(favorable[:3]),
        "remove_largest_5_favorable_mean": _without(favorable[:5]),
        "remove_largest_event_mean": _without(ordered[:1]),
        "tail_winner_sensitivity_mean_without": _without(tails),
        "alpha_conclusion": "no_alpha_conclusion",
    }
