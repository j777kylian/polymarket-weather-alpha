"""Deterministic Phase 3 markdown/JSON reports."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

REPORT_SECTIONS = (
    "MEASURED DATA",
    "MODEL OUTPUT",
    "ASSUMPTIONS",
    "MISSING DATA",
    "INFERENCES",
)

SHARED_LIMITATIONS = (
    "Providers: Polymarket Gamma public-search, CLOB GET /prices-history, "
    "Open-Meteo Single Runs (https://open-meteo.com/en/docs/single-runs-api), "
    "Open-Meteo Archive. Previous Runs docs: https://open-meteo.com/en/docs/previous-runs-api.",
    "CLOB prices-history p is descriptive market_probability only; historical asks are unavailable.",
    "Open-Meteo Archive maxima are diagnostic grid/reanalysis values, not Wunderground settlement.",
    "Open-Meteo Archive is retrospective and is not a decision-time observation feature.",
    "Gamma public-search is a current search index, not a guaranteed archival universe; "
    "delisted, unindexed, or metadata-mutated markets may be absent, so survivorship bias "
    "is possible and market counts must not be read as complete historical coverage.",
    "Fees, slippage, and leverage are not modeled. No compounding or Kelly sizing.",
    "No alpha is claimed when the executable sample is insufficient.",
)

# Operational minimum for this pipeline only — not a universal statistical law.
OPERATIONAL_MIN_SCORED_EVENTS = 30


def build_common_research_context(
    *,
    providers: tuple[str, ...] | list[str],
    requested_range: dict[str, Any],
    usable_event_dates: Any,
    chronological_split: dict[str, Any],
    counts: dict[str, Any],
    exclusions: dict[str, Any],
    quarantines: dict[str, Any],
    model_identity: dict[str, Any],
    executable_state: dict[str, Any],
    assumptions: dict[str, Any],
    missing_data: dict[str, Any],
    limitations: dict[str, Any],
    metrics: dict[str, Any] | None = None,
    metrics_unknown_reason: str = "metrics not applicable to this report surface",
) -> dict[str, Any]:
    """Deterministic shared research context copied into each primary JSON report."""
    return {
        "assumptions": assumptions,
        "chronological_split": chronological_split,
        "counts": counts,
        "descriptive_price_vs_executable": {
            "descriptive_market_probability": (
                "CLOB prices-history p is descriptive market_probability only"
            ),
            "executable_unavailable": (
                "historical asks/order books absent; executable fills unavailable"
            ),
        },
        "exclusions": exclusions,
        "executable_state": executable_state,
        "limitations": limitations,
        "metrics": metrics
        if metrics is not None
        else {"value": None, "reason": metrics_unknown_reason},
        "missing_data": missing_data,
        "model_identity": model_identity,
        "providers": list(providers),
        "quarantines": quarantines,
        "requested_range": requested_range,
        "standalone_caveats": {
            "asks_order_books_absent": True,
            "descriptive_price_not_executable": True,
            "gamma_survivorship_bias": True,
            "no_alpha_inference": True,
            "point_in_time_metar_absent": True,
            "sample_or_checkpoint_limits": True,
            "wunderground_settlement_caveat": True,
        },
        "usable_event_dates": usable_event_dates,
    }


def assess_sample_sufficiency(
    *,
    scored_events: int,
    operational_minimum: int = OPERATIONAL_MIN_SCORED_EVENTS,
) -> dict[str, Any]:
    if scored_events < operational_minimum:
        return {
            "status": "insufficient",
            "conclusion": "inconclusive",
            "scored_events": scored_events,
            "operational_minimum_scored_events": operational_minimum,
            "reason": (
                f"scored events {scored_events} are below the operational minimum of "
                f"{operational_minimum} unique scored events. This minimum is an assumption "
                "for this research pipeline, not a universal statistical threshold."
            ),
        }
    return {
        "status": "meets_operational_minimum",
        "conclusion": "descriptive_only",
        "scored_events": scored_events,
        "operational_minimum_scored_events": operational_minimum,
        "reason": (
            f"scored events {scored_events} meet the operational minimum of "
            f"{operational_minimum} (assumption, not a universal statistical threshold). "
            "Results remain descriptive; no profitability is claimed."
        ),
    }


def render_markdown(
    *,
    title: str,
    measured: tuple[str, ...],
    model_output: tuple[str, ...],
    assumptions: tuple[str, ...],
    missing: tuple[str, ...],
    inferences: tuple[str, ...],
    extra_limitations: tuple[str, ...] = (),
) -> str:
    blocks = [
        f"# {title}",
        "",
        "## MEASURED DATA",
        *_bullets(measured),
        "",
        "## MODEL OUTPUT",
        *_bullets(model_output),
        "",
        "## ASSUMPTIONS",
        *_bullets(assumptions),
        "",
        "## MISSING DATA",
        *_bullets(missing),
        "",
        "## INFERENCES",
        *_bullets(inferences),
        "",
        "## LIMITATIONS",
        *_bullets((*SHARED_LIMITATIONS, *extra_limitations)),
        "",
    ]
    return "\n".join(blocks)


def write_report_pair(
    path_md: Path, path_json: Path, markdown: str, payload: dict[str, Any]
) -> None:
    path_md.parent.mkdir(parents=True, exist_ok=True)
    path_json.parent.mkdir(parents=True, exist_ok=True)
    path_md.write_text(markdown if markdown.endswith("\n") else markdown + "\n", encoding="utf-8")
    encoded = json.dumps(
        payload, indent=2, sort_keys=True, ensure_ascii=True, default=_json_default
    )
    path_json.write_text(encoded + "\n", encoding="utf-8")


def research_contract(
    *,
    measured_data: dict[str, Any],
    model_output: dict[str, Any],
    assumptions: dict[str, Any],
    missing_data: dict[str, Any],
    inferences: dict[str, Any],
    limitations: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Machine-readable research contract independent of Markdown prose."""
    payload = {
        "assumptions": assumptions,
        "inferences": inferences,
        "limitations": limitations,
        "measured_data": measured_data,
        "missing_data": missing_data,
        "model_output": model_output,
    }
    if extra:
        for key, value in extra.items():
            if key in payload:
                raise ValueError(f"contract section collision: {key}")
            payload[key] = value
    return payload


def _json_default(value: Any) -> Any:
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return str(value)


def _bullets(lines: tuple[str, ...]) -> list[str]:
    if not lines:
        return ["- none"]
    return [f"- {line}" for line in lines]
