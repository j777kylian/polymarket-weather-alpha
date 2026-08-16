"""Chronological split: ordered unique event_date, no shuffle, disjoint."""

from __future__ import annotations

import pytest

from weather_alpha.research.split import chronological_split


def test_chronological_split_60_20_20_is_disjoint_and_ordered() -> None:
    dates = tuple(f"2026-07-{day:02d}" for day in range(1, 11))
    split = chronological_split(dates, train_frac=0.6, val_frac=0.2, test_frac=0.2)
    assert split.train == (
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
        "2026-07-04",
        "2026-07-05",
        "2026-07-06",
    )
    assert split.validation == ("2026-07-07", "2026-07-08")
    assert split.test == ("2026-07-09", "2026-07-10")
    assert set(split.train).isdisjoint(split.validation)
    assert set(split.train).isdisjoint(split.test)
    assert set(split.validation).isdisjoint(split.test)
    assert max(split.train) < min(split.validation) < min(split.test)
    assert max(split.validation) < min(split.test)


def test_split_dedupes_and_sorts_event_dates() -> None:
    dates = ("2026-07-03", "2026-07-01", "2026-07-03", "2026-07-02", "2026-07-05", "2026-07-04")
    split = chronological_split(dates)
    assert split.train[0] == "2026-07-01"
    assert len(split.train) + len(split.validation) + len(split.test) == 5
    assert sorted((*split.train, *split.validation, *split.test)) == list(
        (*split.train, *split.validation, *split.test)
    )


def test_split_rejects_fractions_that_do_not_sum_to_one() -> None:
    with pytest.raises(ValueError):
        chronological_split(
            ("2026-07-01", "2026-07-02"), train_frac=0.5, val_frac=0.5, test_frac=0.5
        )
