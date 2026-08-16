"""Descriptive mispricing. Executable edge requires a sourced ask."""

from __future__ import annotations


def raw_edge(*, model_probability: float, market_probability: float | None) -> float | None:
    if market_probability is None:
        return None
    return model_probability - market_probability


def executable_edge(
    *,
    model_probability: float,
    executable_ask: float | None,
) -> float | None:
    if executable_ask is None:
        return None
    return model_probability - executable_ask
