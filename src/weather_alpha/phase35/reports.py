"""Self-contained Phase 3.5 JSON report machinery.

Each report distinguishes MEASURED DATA, MODEL OUTPUT, ASSUMPTIONS, MISSING DATA,
INFERENCES, LIMITATIONS and keeps descriptive vs executable separation explicit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from weather_alpha.phase35.book import HypotheticalAskFill, ValidatedOrderBook
from weather_alpha.phase35.bootstrap import AcceptanceAssessment
from weather_alpha.phase35.config import PRE_REGISTERED_CHECKPOINT_HOURS
from weather_alpha.phase35.contracts import ForwardExecutableBookSnapshot
from weather_alpha.phase35.coverage import CoverageAuditResult, RealSourceCoverageAuditResult
from weather_alpha.research.reports import research_contract, write_report_pair

REPORT_NAMES = (
    "phase35_historical_coverage",
    "phase35_historical_calibration",
    "phase35_historical_tail_analysis",
    "phase35_forward_collection_audit",
    "phase35_forward_executability",
    "phase35_readiness",
)


class _ReadinessDimensionsLike(Protocol):
    def as_dict(self) -> dict[str, Any]: ...


def _write(name: str, output_dir: Path, payload: dict[str, Any], markdown: str) -> Path:
    reports = output_dir / "reports"
    path_json = reports / f"{name}.json"
    path_md = reports / f"{name}.md"
    write_report_pair(path_md, path_json, markdown, payload)
    return path_json


def build_historical_coverage_report(
    audit: CoverageAuditResult,
    *,
    output_dir: Path,
    real_source_audit: RealSourceCoverageAuditResult | None = None,
) -> Path:
    real_schema = real_source_audit or audit.to_real_source_audit_schema(
        audit_mode="fixture_derived"
    )
    payload = research_contract(
        measured_data={
            "coverage_audit": audit.as_dict(),
            "descriptive_only": True,
            "historical_source_ready": audit.historical_source_ready,
            "real_source_coverage_audit": real_schema.as_dict(),
        },
        model_output={
            "status": audit.status,
            "maximum_defensible_span_days": audit.maximum_defensible_span_days,
            "requested_historical_window": real_schema.requested_historical_window.as_dict(),
            "maximum_defensible_contiguous_window": (
                real_schema.maximum_defensible_contiguous_window.as_dict()
            ),
        },
        assumptions={
            "ecmwf_single_runs_only": True,
            "no_stitched_historical_forecast_substitution": True,
            "descriptive_clob_p_not_executable": True,
        },
        missing_data={
            "blocked_reasons": list(audit.blocked_reasons),
            "unsupported_fields": audit.evidence_summary.get("field_failure_days"),
            "known_gaps": [gap.as_dict() for gap in real_schema.known_gaps],
        },
        inferences={
            "full_collection_authorized": False,
            "twelve_month_supported": audit.status == "full_collection_supported",
            "historical_source_ready": audit.historical_source_ready,
            "no_alpha_claim": True,
        },
        limitations={
            "descriptive_only": True,
            "survivorship_limitations": list(real_schema.survivorship_limitations),
            "point_in_time_integrity": real_schema.point_in_time_integrity.as_dict(),
            "notes": [
                "Coverage audit is readiness evidence only; it does not launch collection.",
                "Historical CLOB prices-history p remains descriptive, never executable.",
                "Partial fixture coverage must not set HISTORICAL_SOURCE_READY=true.",
            ],
        },
    )
    md = "\n".join(
        [
            "# phase35_historical_coverage",
            "",
            "## MEASURED DATA",
            f"- status={audit.status}",
            f"- proposed={audit.proposed_start}..{audit.proposed_end}",
            f"- max_defensible_span_days={audit.maximum_defensible_span_days}",
            f"- historical_source_ready={audit.historical_source_ready}",
            "",
            "## MODEL OUTPUT",
            f"- maximum_defensible={audit.maximum_defensible_contiguous_start}"
            f"..{audit.maximum_defensible_contiguous_end}",
            "",
            "## ASSUMPTIONS",
            "- ECMWF Single Runs only; no stitched Historical Forecast substitution",
            "",
            "## MISSING DATA",
            f"- blocked_reasons={list(audit.blocked_reasons)}",
            f"- known_gaps={len(real_schema.known_gaps)}",
            "",
            "## INFERENCES",
            "- full_collection_authorized=false (readiness pass)",
            f"- historical_source_ready={audit.historical_source_ready}",
            "- no alpha claim",
            "",
            "## LIMITATIONS",
            "- descriptive_only=true",
            "",
        ]
    )
    return _write("phase35_historical_coverage", output_dir, payload, md)


def build_historical_calibration_report(
    *,
    acceptance: AcceptanceAssessment,
    bootstrap: dict[str, Any],
    output_dir: Path,
    measured: dict[str, Any] | None = None,
) -> Path:
    payload = research_contract(
        measured_data={
            "acceptance": acceptance.as_dict(),
            "bootstrap": bootstrap,
            "checkpoints": list(PRE_REGISTERED_CHECKPOINT_HOURS),
            "descriptive_only": True,
            **(measured or {}),
        },
        model_output={
            "primary_metrics": ["event_level_multiclass_brier", "event_level_log_loss"],
            "note": "Calibration metrics require collected historical sample; readiness only here.",
        },
        assumptions={
            "event_group_is_inference_block": True,
            "descriptive_price_bands_fixed": True,
        },
        missing_data={
            "full_historical_sample": True,
            "reason": "Phase 3.5 readiness pass does not run 12-month collection",
        },
        inferences={
            "alpha_claimed": False,
            "acceptance_status": acceptance.status,
        },
        limitations={
            "descriptive_only": True,
            "no_unsupported_alpha_conclusion": True,
        },
    )
    md = "\n".join(
        [
            "# phase35_historical_calibration",
            "",
            "## MEASURED DATA",
            f"- acceptance_status={acceptance.status}",
            f"- held_out_event_groups={acceptance.held_out_event_groups}",
            "",
            "## MODEL OUTPUT",
            "- primary metrics reserved for post-collection run",
            "",
            "## ASSUMPTIONS",
            "- canonical event group is the inference block",
            "",
            "## MISSING DATA",
            "- full historical sample not collected in readiness pass",
            "",
            "## INFERENCES",
            "- alpha_claimed=false",
            "",
            "## LIMITATIONS",
            "- descriptive_only=true",
            "",
        ]
    )
    return _write("phase35_historical_calibration", output_dir, payload, md)


def build_historical_tail_analysis_report(
    *,
    robustness: dict[str, Any],
    output_dir: Path,
) -> Path:
    payload = research_contract(
        measured_data={
            "robustness": robustness,
            "descriptive_only": True,
            "price_bands": ["<1c", "1-3c", "3-5c", "5-10c", "10-20c", ">20c"],
        },
        model_output={"tail_analysis": "fixture/readiness scaffolding only"},
        assumptions={"fixed_descriptive_bands": True},
        missing_data={"collected_tail_sample": True},
        inferences={
            "alpha_claimed": False,
            "alpha_conclusion": robustness.get("alpha_conclusion", "no_alpha_conclusion"),
        },
        limitations={"descriptive_only": True},
    )
    md = "\n".join(
        [
            "# phase35_historical_tail_analysis",
            "",
            "## MEASURED DATA",
            "- robustness utilities exercised on fixture groups only",
            "",
            "## MODEL OUTPUT",
            "- none claimed",
            "",
            "## ASSUMPTIONS",
            "- fixed descriptive price bands",
            "",
            "## MISSING DATA",
            "- collected historical tail sample",
            "",
            "## INFERENCES",
            "- no_alpha_conclusion",
            "",
            "## LIMITATIONS",
            "- descriptive_only=true",
            "",
        ]
    )
    return _write("phase35_historical_tail_analysis", output_dir, payload, md)


def build_forward_collection_audit_report(
    *,
    snapshots: tuple[ForwardExecutableBookSnapshot, ...] | list[ForwardExecutableBookSnapshot],
    quarantined: int,
    output_dir: Path,
    validation_mode: str = "fixture_contract",
    liquidity_state_counts: dict[str, int] | None = None,
    executable_two_sided_book_validated: bool = False,
) -> Path:
    counts = dict(liquidity_state_counts or {})
    payload = research_contract(
        measured_data={
            "EXECUTABLE_TWO_SIDED_BOOK_VALIDATED": executable_two_sided_book_validated,
            "FORWARD_LIQUIDITY_STATE_COUNTS": counts,
            "FORWARD_OBSERVATIONS": len(snapshots),
            "http_methods": ["GET"],
            "observed_order_book_facts": True,
            "quarantined": quarantined,
            "snapshot_count": len(snapshots),
            "snapshots": [row.as_dict() for row in snapshots],
            "validation_mode": validation_mode,
        },
        model_output={"collector": "ForwardBookCollector"},
        assumptions={
            "get_only": True,
            "no_order_submission": True,
            "fee_unknown_unless_externally_sourced": True,
            "fixture_contract_is_not_live_smoke": validation_mode == "fixture_contract",
            "one_sided_books_are_valid_observations": True,
            "one_sided_books_are_not_executable": True,
        },
        missing_data={
            "long_running_forward_daemon": True,
            "live_smoke": validation_mode != "live_smoke",
        },
        inferences={
            "trading": False,
            "phase4": False,
            "descriptive_vs_executable_separated": True,
            "forward_validation_mode": validation_mode,
            "executable_two_sided_book_validated": executable_two_sided_book_validated,
        },
        limitations={
            "observed_vs_paper": (
                "raw book levels are observed; VWAP fills are paper assumptions only"
            ),
            "notes": [
                "fixture_contract validation is not a live network smoke.",
                "ASK_ONLY/BID_ONLY observations are retained; they are not executable.",
            ],
        },
        extra={"track": "forward_executable"},
    )
    md = "\n".join(
        [
            "# phase35_forward_collection_audit",
            "",
            "## MEASURED DATA",
            f"- snapshots={len(snapshots)} quarantined={quarantined}",
            f"- validation_mode={validation_mode}",
            f"- FORWARD_OBSERVATIONS={len(snapshots)}",
            f"- FORWARD_LIQUIDITY_STATE_COUNTS={counts}",
            f"- EXECUTABLE_TWO_SIDED_BOOK_VALIDATED={executable_two_sided_book_validated}",
            "",
            "## MODEL OUTPUT",
            "- ForwardBookCollector GET /book",
            "",
            "## ASSUMPTIONS",
            "- GET-only; no order submission",
            "- fixture_contract is not live_smoke",
            "- one-sided books are valid observations, not executable",
            "",
            "## MISSING DATA",
            "- long-running forward daemon not launched",
            "",
            "## INFERENCES",
            "- no trading / no Phase 4",
            "",
            "## LIMITATIONS",
            "- observed book facts vs paper VWAP assumptions remain separate",
            "",
        ]
    )
    return _write("phase35_forward_collection_audit", output_dir, payload, md)


def build_phase35_readiness_report(
    *,
    dimensions: _ReadinessDimensionsLike,
    coverage_status: str,
    maximum_defensible_span_days: int,
    real_source_audit: RealSourceCoverageAuditResult,
    output_dir: Path,
    notes: tuple[str, ...] | list[str] = (),
) -> Path:
    dim_payload = dimensions.as_dict()
    payload = research_contract(
        measured_data={
            "readiness_dimensions": dim_payload,
            "CODE_READY": dim_payload["CODE_READY"],
            "HISTORICAL_SOURCE_READY": dim_payload["HISTORICAL_SOURCE_READY"],
            "HISTORICAL_SOURCE_MODE": dim_payload["HISTORICAL_SOURCE_MODE"],
            "MAX_DEFENSIBLE_HISTORICAL_WINDOW": dim_payload["MAX_DEFENSIBLE_HISTORICAL_WINDOW"],
            "ECMWF_GRID_TOTAL": dim_payload["ECMWF_GRID_TOTAL"],
            "ECMWF_GRID_SUCCESS": dim_payload["ECMWF_GRID_SUCCESS"],
            "ECMWF_GRID_FAILURES": dim_payload["ECMWF_GRID_FAILURES"],
            "CHECKPOINT_SUPPORT": dim_payload["CHECKPOINT_SUPPORT"],
            "GAMMA_SURVIVORSHIP_LIMITATION": dim_payload["GAMMA_SURVIVORSHIP_LIMITATION"],
            "HISTORICAL_UNIVERSE_COMPLETE": dim_payload["HISTORICAL_UNIVERSE_COMPLETE"],
            "GAMMA_REDISCOVERY_RATE": dim_payload["GAMMA_REDISCOVERY_RATE"],
            "CLOB_DESCRIPTIVE_COVERAGE": dim_payload["CLOB_DESCRIPTIVE_COVERAGE"],
            "FORWARD_COLLECTION_READY": dim_payload["FORWARD_COLLECTION_READY"],
            "FORWARD_VALIDATION_MODE": dim_payload["FORWARD_VALIDATION_MODE"],
            "FORWARD_OBSERVATIONS": dim_payload["FORWARD_OBSERVATIONS"],
            "FORWARD_LIQUIDITY_STATE_COUNTS": dim_payload["FORWARD_LIQUIDITY_STATE_COUNTS"],
            "EXECUTABLE_TWO_SIDED_BOOK_VALIDATED": dim_payload[
                "EXECUTABLE_TWO_SIDED_BOOK_VALIDATED"
            ],
            "PHASE35_COLLECTION_READY": dim_payload["PHASE35_COLLECTION_READY"],
            "coverage_status": coverage_status,
            "maximum_defensible_span_days": maximum_defensible_span_days,
            "real_source_coverage_audit": real_source_audit.as_dict(),
        },
        model_output={
            "PHASE35_COLLECTION_READY": dim_payload["PHASE35_COLLECTION_READY"],
            "derivation": ("CODE_READY AND HISTORICAL_SOURCE_READY AND FORWARD_COLLECTION_READY"),
            "executable_excluded_from_collection_ready": True,
        },
        assumptions={
            "unknown_or_false_component_makes_overall_false": True,
            "code_ready_does_not_imply_sources_ready": True,
            "fixture_contract_is_not_live_smoke": True,
            "universe_completeness_not_required_for_historical_ready": True,
            "executable_two_sided_not_required_for_forward_collection_ready": True,
        },
        missing_data={
            "real_provider_probe": real_source_audit.audit_mode != "real_provider",
            "live_forward_smoke": dim_payload.get("FORWARD_VALIDATION_MODE") != "live_smoke",
        },
        inferences={
            "full_collection_authorized": False,
            "forward_daemon_authorized": False,
            "no_alpha_claim": True,
            "collection_ready_not_executable_or_profitable": True,
        },
        limitations={
            "notes": [
                *list(notes),
                (
                    "PHASE35_COLLECTION_READY means historical descriptive collection and "
                    "forward observational collection are ready to begin; it does NOT imply "
                    "strategy executable or strategy profitable."
                ),
            ],
            "survivorship_limitations": list(real_source_audit.survivorship_limitations),
        },
    )
    md = "\n".join(
        [
            "# phase35_readiness",
            "",
            "## MEASURED DATA",
            f"- CODE_READY={dim_payload['CODE_READY']}",
            f"- HISTORICAL_SOURCE_READY={dim_payload['HISTORICAL_SOURCE_READY']}",
            f"- HISTORICAL_SOURCE_MODE={dim_payload['HISTORICAL_SOURCE_MODE']}",
            f"- MAX_DEFENSIBLE_HISTORICAL_WINDOW={dim_payload['MAX_DEFENSIBLE_HISTORICAL_WINDOW']}",
            f"- ECMWF_GRID_TOTAL={dim_payload['ECMWF_GRID_TOTAL']}",
            f"- ECMWF_GRID_SUCCESS={dim_payload['ECMWF_GRID_SUCCESS']}",
            f"- ECMWF_GRID_FAILURES={dim_payload['ECMWF_GRID_FAILURES']}",
            f"- CHECKPOINT_SUPPORT={dim_payload['CHECKPOINT_SUPPORT']}",
            f"- GAMMA_SURVIVORSHIP_LIMITATION={dim_payload['GAMMA_SURVIVORSHIP_LIMITATION']}",
            f"- HISTORICAL_UNIVERSE_COMPLETE={dim_payload['HISTORICAL_UNIVERSE_COMPLETE']}",
            f"- GAMMA_REDISCOVERY_RATE={dim_payload['GAMMA_REDISCOVERY_RATE']}",
            f"- CLOB_DESCRIPTIVE_COVERAGE={dim_payload['CLOB_DESCRIPTIVE_COVERAGE']}",
            f"- FORWARD_COLLECTION_READY={dim_payload['FORWARD_COLLECTION_READY']}",
            f"- FORWARD_VALIDATION_MODE={dim_payload['FORWARD_VALIDATION_MODE']}",
            f"- FORWARD_OBSERVATIONS={dim_payload['FORWARD_OBSERVATIONS']}",
            f"- FORWARD_LIQUIDITY_STATE_COUNTS={dim_payload['FORWARD_LIQUIDITY_STATE_COUNTS']}",
            (
                "- EXECUTABLE_TWO_SIDED_BOOK_VALIDATED="
                f"{dim_payload['EXECUTABLE_TWO_SIDED_BOOK_VALIDATED']}"
            ),
            f"- PHASE35_COLLECTION_READY={dim_payload['PHASE35_COLLECTION_READY']}",
            f"- coverage_status={coverage_status}",
            f"- max_defensible_span_days={maximum_defensible_span_days}",
            "",
            "## MODEL OUTPUT",
            f"- PHASE35_COLLECTION_READY={dim_payload['PHASE35_COLLECTION_READY']}",
            "- executable flag excluded from collection-ready conjunction",
            "",
            "## ASSUMPTIONS",
            "- overall ready is conjunction only; unknown/false components fail closed",
            "- universe completeness not required for survivorship-limited historical ready",
            "",
            "## MISSING DATA",
            f"- real_provider_probe={real_source_audit.audit_mode != 'real_provider'}",
            "",
            "## INFERENCES",
            "- full collection and forward daemon not authorized by readiness",
            "- collection-ready is not executable/profitable",
            "",
            "## LIMITATIONS",
            "- code-ready does not imply historical sources ready",
            "- PHASE35_COLLECTION_READY does not imply strategy executable or profitable",
            "",
        ]
    )
    return _write("phase35_readiness", output_dir, payload, md)


def build_forward_executability_report(
    *,
    book: ValidatedOrderBook,
    fills: tuple[HypotheticalAskFill, ...] | list[HypotheticalAskFill],
    output_dir: Path,
) -> Path:
    payload = research_contract(
        measured_data={
            "observed_book": book.as_dict(),
            "descriptive_probability_separate": True,
        },
        model_output={
            "hypothetical_ask_fills": [row.as_dict() for row in fills],
        },
        assumptions={
            "sizes_fixed_in_config": True,
            "fee_rate": None,
            "fee_status": "unknown",
            "never_submit_order": True,
        },
        missing_data={"externally_sourced_fee_schedule": True},
        inferences={
            "profitability_claimed": False,
            "paper_only": True,
        },
        limitations={
            "observed_order_book_facts_vs_paper_assumptions": True,
            "notes": [
                "VWAP entry/depth/spread_cost are offline paper calculations.",
                "Fee is explicit null/unknown unless externally sourced.",
            ],
        },
        extra={"track": "forward_executable"},
    )
    md = "\n".join(
        [
            "# phase35_forward_executability",
            "",
            "## MEASURED DATA",
            f"- book_status={book.status}",
            "",
            "## MODEL OUTPUT",
            f"- hypothetical_fills={len(fills)}",
            "",
            "## ASSUMPTIONS",
            "- fixed hypothetical sizes; fee unknown",
            "",
            "## MISSING DATA",
            "- externally sourced fee schedule",
            "",
            "## INFERENCES",
            "- profitability_claimed=false",
            "",
            "## LIMITATIONS",
            "- paper assumptions are not observed fills",
            "",
        ]
    )
    return _write("phase35_forward_executability", output_dir, payload, md)
