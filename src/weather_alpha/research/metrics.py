"""Calibration and scoring metrics. No sklearn."""

from __future__ import annotations

from math import log


def multiclass_brier(
    predicted: tuple[tuple[float, ...], ...] | list[tuple[float, ...]],
    outcomes: tuple[int, ...] | list[int],
) -> float:
    if len(predicted) != len(outcomes) or not predicted:
        raise ValueError("predicted and outcomes must be non-empty and aligned")
    total = 0.0
    for probs, outcome in zip(predicted, outcomes, strict=True):
        if outcome < 0 or outcome >= len(probs):
            raise ValueError("outcome index out of range")
        for index, prob in enumerate(probs):
            target = 1.0 if index == outcome else 0.0
            total += (prob - target) ** 2
    return total / len(predicted)


def clipped_log_loss(
    predicted: tuple[tuple[float, ...], ...] | list[tuple[float, ...]],
    outcomes: tuple[int, ...] | list[int],
    *,
    clip: float = 1e-15,
) -> float:
    if len(predicted) != len(outcomes) or not predicted:
        raise ValueError("predicted and outcomes must be non-empty and aligned")
    total = 0.0
    for probs, outcome in zip(predicted, outcomes, strict=True):
        p = min(1.0 - clip, max(clip, probs[outcome]))
        total += -log(p)
    return total / len(predicted)


def expected_calibration_error(
    probabilities: tuple[float, ...] | list[float],
    outcomes: tuple[int, ...] | list[int],
    *,
    n_bins: int = 10,
) -> float:
    if len(probabilities) != len(outcomes) or not probabilities:
        raise ValueError("probabilities and outcomes must be non-empty and aligned")
    if n_bins <= 0:
        raise ValueError("n_bins must be positive")
    bins: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for prob, outcome in zip(probabilities, outcomes, strict=True):
        index = min(n_bins - 1, max(0, int(prob * n_bins)))
        bins[index].append((prob, outcome))
    ece = 0.0
    n = len(probabilities)
    for bucket in bins:
        if not bucket:
            continue
        avg_p = sum(item[0] for item in bucket) / len(bucket)
        avg_y = sum(item[1] for item in bucket) / len(bucket)
        ece += (len(bucket) / n) * abs(avg_p - avg_y)
    return ece
