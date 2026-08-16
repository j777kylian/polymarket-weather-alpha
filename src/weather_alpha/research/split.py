"""Chronological train/validation/test split by unique event_date."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SplitDates:
    train: tuple[str, ...]
    validation: tuple[str, ...]
    test: tuple[str, ...]


def chronological_split(
    event_dates: tuple[str, ...] | list[str],
    *,
    train_frac: float = 0.6,
    val_frac: float = 0.2,
    test_frac: float = 0.2,
) -> SplitDates:
    total_frac = train_frac + val_frac + test_frac
    if abs(total_frac - 1.0) > 1e-12:
        raise ValueError(
            f"split fractions must sum to 1.0; got {train_frac}+{val_frac}+{test_frac}={total_frac}"
        )
    if min(train_frac, val_frac, test_frac) < 0:
        raise ValueError("split fractions must be non-negative")
    unique = tuple(sorted(dict.fromkeys(event_dates)))
    n = len(unique)
    if n == 0:
        return SplitDates(train=(), validation=(), test=())
    n_train = int(n * train_frac)
    n_val = int(n * val_frac)
    # Remainder goes to test so partitions cover every unique date.
    if n_train + n_val >= n:
        n_val = max(0, n - n_train - 1)
    n_test = n - n_train - n_val
    train = unique[:n_train]
    validation = unique[n_train : n_train + n_val]
    test = unique[n_train + n_val :]
    if train and validation and max(train) >= min(validation):
        raise ValueError("train/validation boundaries are not disjoint and ordered")
    if validation and test and max(validation) >= min(test):
        raise ValueError("validation/test boundaries are not disjoint and ordered")
    if train and test and max(train) >= min(test):
        raise ValueError("train/test boundaries are not disjoint and ordered")
    if len(test) != n_test:
        raise ValueError("chronological split failed to cover unique dates")
    return SplitDates(train=train, validation=validation, test=test)
