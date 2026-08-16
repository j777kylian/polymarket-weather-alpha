"""Descriptive tail-price analysis. No fabricated jackpot PnL."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from weather_alpha.research.mispricing import raw_edge
from weather_alpha.research.types import ResearchSnapshot

TAIL_BANDS: tuple[tuple[str, float, float], ...] = (
    ("<1c", 0.0, 0.01),
    ("1-3c", 0.01, 0.03),
    ("3-5c", 0.03, 0.05),
)


class _UnsetType:
    """Sentinel type so omitted executable_survival is distinct from explicit None."""


_UNSET = _UnsetType()


class ExecutableSurvivalState(Enum):
    """Validated executable-survival encoding; reject all other combinations."""

    UNKNOWN = "UNKNOWN"
    MEASURED_SURVIVED = "MEASURED_SURVIVED"
    MEASURED_DID_NOT_SURVIVE = "MEASURED_DID_NOT_SURVIVE"

    @property
    def survival(self) -> bool | None:
        return _STATE_ENCODING[self][0]

    @property
    def status(self) -> str:
        return _STATE_ENCODING[self][1]


_STATE_ENCODING: dict[ExecutableSurvivalState, tuple[bool | None, str]] = {
    ExecutableSurvivalState.UNKNOWN: (None, "unknown_no_historical_asks"),
    ExecutableSurvivalState.MEASURED_SURVIVED: (True, "measured_survived"),
    ExecutableSurvivalState.MEASURED_DID_NOT_SURVIVE: (False, "measured_did_not_survive"),
}


def build_executable_survival(
    survival: bool | None,
    status: str | None,
) -> ExecutableSurvivalState:
    pair = (survival, status)
    for state, encoding in _STATE_ENCODING.items():
        if encoding == pair:
            return state
    raise ValueError(
        "invalid executable survival combination: "
        f"survival={survival!r} status={status!r}; "
        "allowed: UNKNOWN(None, unknown_no_historical_asks), "
        "MEASURED_SURVIVED(True, measured_survived), "
        "MEASURED_DID_NOT_SURVIVE(False, measured_did_not_survive)"
    )


@dataclass(frozen=True, slots=True)
class TailBandStats:
    band: str
    n: int
    settled_yes_fraction: float | None
    mean_model_probability: float | None
    mean_raw_edge: float | None
    settled_yes_count: int = 0


@dataclass(frozen=True, slots=True)
class TailAnalysis:
    split_name: str
    bands: tuple[TailBandStats, ...]
    executable_survival: bool | None
    executable_survival_status: str
    jackpot_concentration: float | None
    max_band_settled_yes_share: float | None
    pnl: float | None
    roi: float | None
    max_drawdown: float | None
    profit_factor: float | None
    robustness_remove_largest_1_pnl: float | None
    robustness_remove_largest_3_pnl: float | None
    robustness_remove_largest_5_pnl: float | None
    notes: tuple[str, ...]


def classify_tail_band(price: float | None) -> str | None:
    if price is None:
        return None
    for name, lo, hi in TAIL_BANDS:
        if lo <= price < hi:
            return name
    return None


def analyze_tails(
    *,
    snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot],
    model_probabilities: dict[str, float],
    split_name: str,
    executable_survival: bool | _UnsetType | None = _UNSET,
    executable_survival_status: str | None = None,
) -> TailAnalysis:
    grouped: dict[str, list[tuple[ResearchSnapshot, float, float | None]]] = {
        name: [] for name, _lo, _hi in TAIL_BANDS
    }
    for snapshot in snapshots:
        band = classify_tail_band(snapshot.market_probability)
        if band is None:
            continue
        model_p = model_probabilities.get(snapshot.token_id)
        if model_p is None:
            continue
        edge = raw_edge(model_probability=model_p, market_probability=snapshot.market_probability)
        grouped[band].append((snapshot, model_p, edge))
    bands: list[TailBandStats] = []
    yes_counts: list[int] = []
    for name, _lo, _hi in TAIL_BANDS:
        rows = grouped[name]
        n = len(rows)
        if n == 0:
            bands.append(
                TailBandStats(
                    band=name,
                    n=0,
                    settled_yes_fraction=None,
                    mean_model_probability=None,
                    mean_raw_edge=None,
                    settled_yes_count=0,
                )
            )
            yes_counts.append(0)
            continue
        yes = sum(1 for snap, _p, _e in rows if (snap.settlement_label or "").lower() == "yes")
        yes_counts.append(yes)
        model_mean = sum(p for _s, p, _e in rows) / n
        edges = [e for _s, _p, e in rows if e is not None]
        edge_mean = sum(edges) / len(edges) if edges else None
        bands.append(
            TailBandStats(
                band=name,
                n=n,
                settled_yes_fraction=yes / n,
                mean_model_probability=model_mean,
                mean_raw_edge=edge_mean,
                settled_yes_count=yes,
            )
        )
    total_yes = sum(yes_counts)
    max_share = None if total_yes == 0 else max(yes_counts) / total_yes

    if executable_survival is _UNSET:
        state = ExecutableSurvivalState.UNKNOWN
    else:
        survival: bool | None
        if executable_survival is True:
            survival = True
        elif executable_survival is False:
            survival = False
        else:
            survival = None
        if executable_survival_status is not None:
            status = executable_survival_status
        elif survival is True:
            status = ExecutableSurvivalState.MEASURED_SURVIVED.status
        elif survival is False:
            status = ExecutableSurvivalState.MEASURED_DID_NOT_SURVIVE.status
        else:
            status = ExecutableSurvivalState.UNKNOWN.status
        state = build_executable_survival(survival, status)

    if state is ExecutableSurvivalState.UNKNOWN:
        notes = (
            "Historical asks are unavailable; executable_survival is null "
            f"(status={state.status}).",
            "Largest 1/3/5 removal robustness returns null PnL because there are no fills.",
            "jackpot_concentration is null: max-band settled YES share is a count statistic, "
            "not return concentration.",
            "pnl/roi/max_drawdown/profit_factor remain null without executable fills.",
        )
    elif state is ExecutableSurvivalState.MEASURED_SURVIVED:
        notes = (
            f"Executable survival measured_survived (status={state.status}).",
            "Largest 1/3/5 removal robustness returns null PnL because fills are not reconstructed.",
            "jackpot_concentration is null: max-band settled YES share is a count statistic, "
            "not return concentration.",
            "pnl/roi/max_drawdown/profit_factor remain null without reconstructed fill economics.",
        )
    else:
        notes = (
            f"Executable survival measured_did_not_survive (status={state.status}).",
            "Largest 1/3/5 removal robustness returns null PnL because fills are not reconstructed.",
            "jackpot_concentration is null: max-band settled YES share is a count statistic, "
            "not return concentration.",
            "pnl/roi/max_drawdown/profit_factor remain null without reconstructed fill economics.",
        )

    return TailAnalysis(
        split_name=split_name,
        bands=tuple(bands),
        executable_survival=state.survival,
        executable_survival_status=state.status,
        jackpot_concentration=None,
        max_band_settled_yes_share=max_share,
        pnl=None,
        roi=None,
        max_drawdown=None,
        profit_factor=None,
        robustness_remove_largest_1_pnl=None,
        robustness_remove_largest_3_pnl=None,
        robustness_remove_largest_5_pnl=None,
        notes=notes,
    )
