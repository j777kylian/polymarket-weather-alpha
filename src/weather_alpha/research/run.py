"""Phase 3 evaluation run: split, fit, score, backtest, tail, reports."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from weather_alpha.research.audit import DatasetAudit, audit_dataset
from weather_alpha.research.backtest import (
    EDGE_THRESHOLDS,
    BacktestEvaluation,
    DescriptiveBacktester,
)
from weather_alpha.research.dataset import row_to_snapshot
from weather_alpha.research.metrics import (
    clipped_log_loss,
    expected_calibration_error,
    multiclass_brier,
)
from weather_alpha.research.model import (
    ForecastErrorBucketModel,
    HistoricalFrequencyBaseline,
    probabilities_sum_to_one,
)
from weather_alpha.research.reports import (
    SHARED_LIMITATIONS,
    assess_sample_sufficiency,
    build_common_research_context,
    render_markdown,
    research_contract,
    write_report_pair,
)
from weather_alpha.research.split import SplitDates, chronological_split
from weather_alpha.research.tail import TailAnalysis, analyze_tails
from weather_alpha.research.types import QuarantineRecord, ResearchSnapshot, event_group_key


@dataclass(frozen=True, slots=True)
class Phase3RunResult:
    audit: DatasetAudit
    split: SplitDates
    backtest_validation: BacktestEvaluation
    backtest_test: BacktestEvaluation
    tail_test: TailAnalysis
    metrics: dict[str, Any]


def load_snapshots_from_jsonl(path: Path) -> tuple[ResearchSnapshot, ...]:
    rows: list[ResearchSnapshot] = []
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return ()
    for line in text.splitlines():
        payload = json.loads(line)
        rows.append(row_to_snapshot(payload))
    return tuple(rows)


def load_quarantine(path: Path | None) -> tuple[QuarantineRecord, ...]:
    if path is None or not path.is_file():
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return ()
    records: list[QuarantineRecord] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        records.append(
            QuarantineRecord(
                reason=str(item.get("reason") or "unspecified"),
                condition_id=_opt_str(item.get("condition_id")),
                market_id=_opt_str(item.get("market_id")),
                token_id=_opt_str(item.get("token_id")),
                city=_opt_str(item.get("city")),
                station_icao=_opt_str(item.get("station_icao")),
                event_date=_opt_str(item.get("event_date")),
                details=_opt_str(item.get("details")),
            )
        )
    return tuple(records)


def run_phase3(
    snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot],
    *,
    output_dir: Path,
    quarantined: tuple[QuarantineRecord, ...] | list[QuarantineRecord] = (),
    collect_manifest: dict[str, Any] | None = None,
) -> Phase3RunResult:
    ordered = tuple(
        sorted(snapshots, key=lambda item: (item.event_date, item.token_id, item.condition_id))
    )
    audit = audit_dataset(ordered, quarantined=quarantined)
    dates = tuple(item.event_date for item in ordered)
    split = chronological_split(dates)
    train = tuple(item for item in ordered if item.event_date in split.train)
    val = tuple(item for item in ordered if item.event_date in split.validation)
    test = tuple(item for item in ordered if item.event_date in split.test)
    frequency = HistoricalFrequencyBaseline()
    frequency.fit(train)
    error_model = ForecastErrorBucketModel()
    error_model.fit(train, split=split)

    val_probs = _predict_by_event(error_model, val)
    test_probs = _predict_by_event(error_model, test)
    val_freq = _predict_by_event(frequency, val)
    test_freq = _predict_by_event(frequency, test)

    metrics = {
        "validation": _score_split(val, val_probs, val_freq),
        "test": _score_split(test, test_probs, test_freq),
        "split": {
            "train_dates": list(split.train),
            "validation_dates": list(split.validation),
            "test_dates": list(split.test),
        },
    }
    backtester = DescriptiveBacktester()
    backtest_val = backtester.evaluate(
        snapshots=val,
        model_probabilities=val_probs,
        thresholds=EDGE_THRESHOLDS,
        split_name="validation",
    )
    backtest_test = backtester.evaluate(
        snapshots=test,
        model_probabilities=test_probs,
        thresholds=EDGE_THRESHOLDS,
        split_name="test",
        selected_threshold=None,
    )
    tail = analyze_tails(snapshots=test, model_probabilities=test_probs, split_name="test")
    _write_all_reports(
        output_dir=output_dir,
        audit=audit,
        metrics=metrics,
        backtest_val=backtest_val,
        backtest_test=backtest_test,
        tail=tail,
        split=split,
        n_train=len(train),
        n_val=len(val),
        n_test=len(test),
        collect_manifest=collect_manifest,
    )
    return Phase3RunResult(
        audit=audit,
        split=split,
        backtest_validation=backtest_val,
        backtest_test=backtest_test,
        tail_test=tail,
        metrics=metrics,
    )


def _predict_by_event(
    model: HistoricalFrequencyBaseline | ForecastErrorBucketModel,
    snapshots: tuple[ResearchSnapshot, ...],
) -> dict[str, float]:
    grouped: dict[tuple[str, ...], list[ResearchSnapshot]] = {}
    for snapshot in snapshots:
        grouped.setdefault(event_group_key(snapshot), []).append(snapshot)
    out: dict[str, float] = {}
    for rows in grouped.values():
        rows_sorted = sorted(rows, key=lambda item: item.token_id)
        probs = model.predict_event(rows_sorted)
        for snapshot, bucket in zip(rows_sorted, probs, strict=True):
            out[snapshot.token_id] = bucket.probability
    return out


def score_split(
    snapshots: tuple[ResearchSnapshot, ...] | list[ResearchSnapshot],
    model_probs: dict[str, float],
    baseline_probs: dict[str, float],
) -> dict[str, Any]:
    labeled = [row for row in snapshots if row.settlement_label is not None]
    empty = {
        "status": "insufficient_data",
        "reason": "no settlement labels",
        "n": 0,
        "scored_events": 0,
        "skipped_events": 0,
        "skip_reasons": [],
        "multiclass_brier": None,
        "clipped_log_loss": None,
        "ece": None,
        "probability_sum_ok": None,
        "baseline": {
            "multiclass_brier": None,
            "clipped_log_loss": None,
            "ece": None,
            "probability_sum_ok": None,
        },
    }
    if not labeled:
        return empty
    grouped: dict[tuple[str, ...], list[ResearchSnapshot]] = {}
    for snapshot in labeled:
        grouped.setdefault(event_group_key(snapshot), []).append(snapshot)
    pred_rows: list[tuple[float, ...]] = []
    outcomes: list[int] = []
    yes_p: list[float] = []
    yes_y: list[int] = []
    base_rows: list[tuple[float, ...]] = []
    base_outcomes: list[int] = []
    base_yes_p: list[float] = []
    base_yes_y: list[int] = []
    skip_reasons: dict[str, int] = {}
    skipped = 0
    for rows in grouped.values():
        rows_sorted = sorted(rows, key=lambda item: item.token_id)
        yes_indices = [
            index
            for index, item in enumerate(rows_sorted)
            if (item.settlement_label or "").lower() == "yes"
        ]
        if len(yes_indices) == 0:
            skipped += 1
            skip_reasons["zero YES winners"] = skip_reasons.get("zero YES winners", 0) + 1
            continue
        if len(yes_indices) > 1:
            skipped += 1
            skip_reasons["multiple YES winners"] = skip_reasons.get("multiple YES winners", 0) + 1
            continue
        if any(
            item.token_id not in model_probs or item.token_id not in baseline_probs
            for item in rows_sorted
        ):
            skipped += 1
            skip_reasons["missing model or baseline probabilities"] = (
                skip_reasons.get("missing model or baseline probabilities", 0) + 1
            )
            continue
        mprobs = tuple(model_probs[item.token_id] for item in rows_sorted)
        bprobs = tuple(baseline_probs[item.token_id] for item in rows_sorted)
        if not probabilities_sum_to_one(mprobs) or not probabilities_sum_to_one(bprobs):
            skipped += 1
            skip_reasons["probabilities missing or not normalized"] = (
                skip_reasons.get("probabilities missing or not normalized", 0) + 1
            )
            continue
        winner = yes_indices[0]
        pred_rows.append(mprobs)
        outcomes.append(winner)
        base_rows.append(bprobs)
        base_outcomes.append(winner)
        for item, prob, bprob in zip(rows_sorted, mprobs, bprobs, strict=True):
            yes_p.append(prob)
            yes_y.append(1 if (item.settlement_label or "").lower() == "yes" else 0)
            base_yes_p.append(bprob)
            base_yes_y.append(1 if (item.settlement_label or "").lower() == "yes" else 0)
    scored_events = len(pred_rows)
    skip_reason_list = [f"{reason} ({count})" for reason, count in sorted(skip_reasons.items())]
    if not pred_rows:
        return {
            "status": "insufficient_data",
            "reason": "no scorable events after winner/probability checks",
            "n": len(labeled),
            "events": len(grouped),
            "scored_events": 0,
            "skipped_events": skipped,
            "skip_reasons": skip_reason_list,
            "multiclass_brier": None,
            "clipped_log_loss": None,
            "ece": None,
            "probability_sum_ok": None,
            "baseline": {
                "name": "HistoricalFrequencyBaseline",
                "multiclass_brier": None,
                "clipped_log_loss": None,
                "ece": None,
                "probability_sum_ok": None,
            },
            "bucket_coverage": sorted({row.bucket_label or "" for row in labeled}),
        }
    return {
        "status": "ok",
        "n": len(labeled),
        "events": len(grouped),
        "scored_events": scored_events,
        "skipped_events": skipped,
        "skip_reasons": skip_reason_list,
        "multiclass_brier": multiclass_brier(pred_rows, outcomes),
        "clipped_log_loss": clipped_log_loss(pred_rows, outcomes),
        "ece": expected_calibration_error(tuple(yes_p), tuple(yes_y)),
        "probability_sum_ok": all(probabilities_sum_to_one(row) for row in pred_rows),
        "baseline": {
            "name": "HistoricalFrequencyBaseline",
            "multiclass_brier": multiclass_brier(base_rows, base_outcomes),
            "clipped_log_loss": clipped_log_loss(base_rows, base_outcomes),
            "ece": expected_calibration_error(tuple(base_yes_p), tuple(base_yes_y)),
            "probability_sum_ok": all(probabilities_sum_to_one(row) for row in base_rows),
        },
        "bucket_coverage": sorted({row.bucket_label or "" for row in labeled}),
    }


def _score_split(
    snapshots: tuple[ResearchSnapshot, ...],
    model_probs: dict[str, float],
    baseline_probs: dict[str, float],
) -> dict[str, Any]:
    return score_split(snapshots, model_probs, baseline_probs)


def _write_all_reports(
    *,
    output_dir: Path,
    audit: DatasetAudit,
    metrics: dict[str, Any],
    backtest_val: BacktestEvaluation,
    backtest_test: BacktestEvaluation,
    tail: TailAnalysis,
    split: SplitDates,
    n_train: int,
    n_val: int,
    n_test: int,
    collect_manifest: dict[str, Any] | None,
) -> None:
    reports = output_dir / "reports"
    split_line = (
        f"chronological unique event_date split 60/20/20; "
        f"train={list(split.train)}; validation={list(split.validation)}; test={list(split.test)}"
    )
    sample_line = f"snapshots train={n_train} validation={n_val} test={n_test}"
    test_scored = int(metrics.get("test", {}).get("scored_events") or 0)
    val_scored = int(metrics.get("validation", {}).get("scored_events") or 0)
    sample_assessment = assess_sample_sufficiency(scored_events=test_scored)
    metrics["sample_assessment"] = sample_assessment
    providers = (
        "Polymarket Gamma public-search",
        "Polymarket CLOB GET /prices-history",
        "Open-Meteo Single Runs (ecmwf_ifs)",
        "Open-Meteo Archive (diagnostic only)",
    )
    requested_range = "requested date range unavailable"
    usable_range = "usable event dates from snapshots in split"
    collection_options = "collection options unavailable"
    outside_range = "discovered_outside_range unavailable"
    price_fetch_counts = "price_http_errors unavailable; price_history_empty unavailable"
    if collect_manifest:
        requested_range = (
            f"requested_dates={collect_manifest.get('start_date')}.."
            f"{collect_manifest.get('end_date')}"
        )
        usable = collect_manifest.get("usable_event_dates") or []
        usable_range = f"usable_event_dates={usable}"
        collection_options = (
            f"max_search_pages={collect_manifest.get('max_search_pages')} "
            f"search_limit_per_type={collect_manifest.get('search_limit_per_type')} "
            f"cities={collect_manifest.get('cities')}"
        )
        outside_range = (
            f"discovered_outside_range={collect_manifest.get('discovered_outside_range')}"
        )
        price_fetch_counts = (
            f"price_http_errors={collect_manifest.get('price_http_errors')} "
            f"price_history_empty={collect_manifest.get('price_history_empty')}"
        )

    scored_line = (
        f"validation scored_events={val_scored} skipped={metrics.get('validation', {}).get('skipped_events')} "
        f"reasons={metrics.get('validation', {}).get('skip_reasons')}; "
        f"test scored_events={test_scored} skipped={metrics.get('test', {}).get('skipped_events')} "
        f"reasons={metrics.get('test', {}).get('skip_reasons')}"
    )

    limitation_block = {
        "providers_and_sources": list(providers),
        "shared": list(SHARED_LIMITATIONS),
        "survivorship_bias": (
            "Gamma public-search is a current search index, not a guaranteed archival universe."
        ),
        "gamma_survivorship": (
            "Gamma public-search is a current search index, not a guaranteed archival universe; "
            "survivorship bias is possible."
        ),
        "settlement_source": (
            "Open-Meteo Archive maxima are diagnostic; Wunderground settlement prints are absent."
        ),
        "missing_historical_asks": (
            "CLOB prices-history does not reconstruct historical asks/order books."
        ),
        "asks_orderbooks": {
            "value": None,
            "reason": "historical asks/order books unavailable from public CLOB prices-history",
        },
        "point_in_time_observations": (
            "Point-in-time historical METAR/station observations are unavailable; "
            "observation_max_so_far_c remains null."
        ),
        "point_in_time_metar": {
            "value": None,
            "reason": "point-in-time historical METAR/station observations unavailable",
        },
        "sample": sample_line,
        "scored": scored_line,
        "sample_or_checkpoint": sample_assessment,
        "no_alpha_or_executability": (
            "No alpha is claimed; executable fills cannot be reconstructed without historical asks."
        ),
        "chronological_split": {
            "train_dates": list(split.train),
            "validation_dates": list(split.validation),
            "test_dates": list(split.test),
        },
    }

    requested_range_obj = {
        "start_date": None if not collect_manifest else collect_manifest.get("start_date"),
        "end_date": None if not collect_manifest else collect_manifest.get("end_date"),
        "label": requested_range,
    }
    chronological_split_obj = {
        "train_dates": list(split.train),
        "validation_dates": list(split.validation),
        "test_dates": list(split.test),
        "snapshot_counts": {"train": n_train, "validation": n_val, "test": n_test},
    }
    counts_obj = {
        "markets": audit.markets,
        "snapshots": audit.snapshots,
        "quarantined": None if not collect_manifest else collect_manifest.get("quarantined"),
        "discovered_outside_range": None
        if not collect_manifest
        else collect_manifest.get("discovered_outside_range"),
        "price_http_errors": None
        if not collect_manifest
        else collect_manifest.get("price_http_errors"),
        "price_history_empty": None
        if not collect_manifest
        else collect_manifest.get("price_history_empty"),
        "price_schema_errors": None
        if not collect_manifest
        else collect_manifest.get("price_schema_errors"),
        "gamma_schema_errors": None
        if not collect_manifest
        else collect_manifest.get("gamma_schema_errors"),
        "single_run_schema_errors": None
        if not collect_manifest
        else collect_manifest.get("single_run_schema_errors"),
        "archive_schema_errors": None
        if not collect_manifest
        else collect_manifest.get("archive_schema_errors"),
    }
    exclusions_obj = dict(audit.exclusions)
    quarantines_obj = {
        "count": None if not collect_manifest else collect_manifest.get("quarantined"),
        "reasons": dict(audit.exclusions),
        "detail": (None if collect_manifest is not None else {"value": None, "reason": "unknown"}),
    }
    model_identity_obj = {
        "primary": "ForecastErrorBucketModel",
        "baseline": "HistoricalFrequencyBaseline",
        "models": ["ForecastErrorBucketModel", "HistoricalFrequencyBaseline"],
    }
    executable_state_obj = {
        "executable_entry_price": {"value": None, "reason": "historical asks unavailable"},
        "asks_order_books": {"value": None, "reason": "historical asks/order books absent"},
        "executable_trades": 0,
        "descriptive_only": True,
    }
    shared_assumptions = {
        "split": split_line,
        "timestamps_utc": True,
        "date_filters_after_parsing": True,
        "out_of_range_counted_not_quarantined_per_market": True,
        "descriptive_price_not_executable": True,
    }
    shared_missing = {
        "asks_orderbooks": {
            "value": None,
            "reason": "historical asks/order books unavailable",
        },
        "point_in_time_metar": {
            "value": None,
            "reason": "point-in-time METAR unavailable",
        },
        "wunderground_settlement_prints": {
            "value": None,
            "reason": "Wunderground settlement prints absent; archive maxima are diagnostic only",
        },
    }
    common_ctx = build_common_research_context(
        providers=providers,
        requested_range=requested_range_obj,
        usable_event_dates=None
        if not collect_manifest
        else collect_manifest.get("usable_event_dates"),
        chronological_split=chronological_split_obj,
        counts=counts_obj,
        exclusions=exclusions_obj,
        quarantines=quarantines_obj,
        model_identity=model_identity_obj,
        executable_state=executable_state_obj,
        assumptions=shared_assumptions,
        missing_data=shared_missing,
        limitations=limitation_block,
        metrics={
            "validation": _model_metric_bundle(metrics.get("validation")),
            "test": _model_metric_bundle(metrics.get("test")),
            "sample_assessment": sample_assessment,
        },
    )

    audit_payload = research_contract(
        measured_data={
            "markets": audit.markets,
            "snapshots": audit.snapshots,
            "market_counts": {"markets": audit.markets, "snapshots": audit.snapshots},
            "providers": list(providers),
            "requested_range": requested_range_obj,
            "requested_dates": requested_range,
            "usable_event_dates": None
            if not collect_manifest
            else collect_manifest.get("usable_event_dates"),
            "collection_options": collection_options,
            "discovered_outside_range": None
            if not collect_manifest
            else collect_manifest.get("discovered_outside_range"),
            "price_http_errors": None
            if not collect_manifest
            else collect_manifest.get("price_http_errors"),
            "price_history_empty": None
            if not collect_manifest
            else collect_manifest.get("price_history_empty"),
            "price_schema_errors": None
            if not collect_manifest
            else collect_manifest.get("price_schema_errors"),
            "city_coverage": dict(audit.city_coverage),
            "station_coverage": dict(audit.station_coverage),
            "date_coverage": dict(audit.date_coverage),
            "duplicates": audit.duplicates,
            "exclusions": dict(audit.exclusions),
            "quarantined": None if not collect_manifest else collect_manifest.get("quarantined"),
            "excluded": dict(audit.exclusions),
            "collect_manifest": collect_manifest,
            "chronological_split": chronological_split_obj,
        },
        model_output={"note": "Dataset audit does not fit a probability model."},
        assumptions=shared_assumptions,
        missing_data={
            "field_missingness": dict(audit.field_missingness),
            "exclusions": dict(audit.exclusions),
            "notes": list(audit.notes),
            **shared_missing,
        },
        inferences={
            "trading_inference": False,
            "alpha_claimed": False,
            "timestamp_violations": list(audit.timestamp_violations),
            "no_alpha_or_executability": (
                "No alpha or executability claim from coverage counts alone."
            ),
        },
        limitations=limitation_block,
        extra={
            "audit": audit.as_dict(),
            "collect_manifest": collect_manifest,
            "common_research_context": common_ctx,
        },
    )
    audit_md = render_markdown(
        title="Phase 3 dataset audit",
        measured=(
            f"markets={audit.markets}",
            f"snapshots={audit.snapshots}",
            f"providers={list(providers)}",
            requested_range,
            usable_range,
            collection_options,
            outside_range,
            price_fetch_counts,
            f"city_coverage={_stable(audit.city_coverage)}",
            f"station_coverage={_stable(audit.station_coverage)}",
            f"date_coverage={_stable(audit.date_coverage)}",
            f"duplicates={audit.duplicates}",
            f"exclusions={_stable(audit.exclusions)}",
        ),
        model_output=("Dataset audit does not fit a probability model.",),
        assumptions=(
            split_line,
            "Timestamps are timezone-aware UTC.",
            "Date filters are applied only after conservative market parsing.",
            "Out-of-range discovered markets are counted, not listed per-market in quarantine.",
        ),
        missing=(
            f"field_missingness={_stable(audit.field_missingness)}",
            f"exclusions={_stable(audit.exclusions)}",
            *audit.notes,
        ),
        inferences=(
            "No trading inference is drawn from coverage counts.",
            "timestamp_violations=" + (", ".join(audit.timestamp_violations) or "none"),
        ),
        extra_limitations=(sample_line, scored_line),
    )
    write_report_pair(
        reports / "phase3_dataset_audit.md",
        reports / "phase3_dataset_audit.json",
        audit_md,
        audit_payload,
    )

    cal_payload = research_contract(
        measured_data={
            "split": {
                "train_dates": list(split.train),
                "validation_dates": list(split.validation),
                "test_dates": list(split.test),
            },
            "snapshot_counts": {"train": n_train, "validation": n_val, "test": n_test},
            "scored": {
                "validation": metrics.get("validation"),
                "test": metrics.get("test"),
            },
            "providers": list(providers),
            "requested_range": requested_range_obj,
            "usable_event_dates": None
            if not collect_manifest
            else collect_manifest.get("usable_event_dates"),
            "sample_assessment": sample_assessment,
            "chronological_split": chronological_split_obj,
        },
        model_output={
            "model_type": "ForecastErrorBucketModel",
            "models": [
                "ForecastErrorBucketModel",
                "HistoricalFrequencyBaseline",
            ],
            "validation": metrics.get("validation"),
            "test": metrics.get("test"),
            "calibration_metrics": {
                "validation": _model_metric_bundle(metrics.get("validation")),
                "test": _model_metric_bundle(metrics.get("test")),
            },
            "sample_assessment": sample_assessment,
        },
        assumptions={
            "train_only_fit": True,
            "settlement_labels_are_targets_not_features": True,
            "validation_test_labels_unused_during_fit": True,
            "operational_minimum_scored_events": sample_assessment[
                "operational_minimum_scored_events"
            ],
            **shared_assumptions,
        },
        missing_data={
            "historical_asks": True,
            "executable_fills": True,
            "small_n_descriptive_only": True,
            "sample_assessment_reason": sample_assessment["reason"],
            **shared_missing,
        },
        inferences={
            "alpha_claimed": False,
            "sample_conclusion": sample_assessment["conclusion"],
            "tradable_edge": False,
            "no_alpha_or_executability": (
                "No alpha claim from calibration scores; executable edge not established."
            ),
        },
        limitations=limitation_block,
        extra={
            "split": metrics.get("split"),
            "sample_assessment": sample_assessment,
            "common_research_context": common_ctx,
        },
    )
    cal_md = render_markdown(
        title="Phase 3 model calibration",
        measured=(
            split_line,
            sample_line,
            scored_line,
            requested_range,
            usable_range,
            f"providers={list(providers)}",
        ),
        model_output=(
            f"validation={_stable(metrics['validation'])}",
            f"test={_stable(metrics['test'])}",
            f"HistoricalFrequencyBaseline={_stable(metrics['test'].get('baseline'))}",
            f"sample_assessment={_stable(sample_assessment)}",
        ),
        assumptions=(
            "Forecast-error distribution and frequency baseline are fit on train dates only.",
            "Settlement labels are evaluation targets, not features.",
            "Validation/test labels are not used during fitting.",
            (
                f"Operational minimum unique scored events="
                f"{sample_assessment['operational_minimum_scored_events']} "
                "(pipeline assumption, not a universal statistical threshold)."
            ),
        ),
        missing=(
            "If n is small, Brier/log loss/ECE are descriptive only.",
            "Historical asks are absent. Calibration metrics use Gamma settlement labels; "
            "market-mispricing comparisons use descriptive CLOB p, and neither implies fills.",
            sample_assessment["reason"],
        ),
        inferences=(
            "No alpha claim is made from calibration scores alone.",
            f"Sample conclusion: {sample_assessment['conclusion']}.",
            "Insufficient executable sample: do not treat scores as tradable edge.",
        ),
    )
    write_report_pair(
        reports / "phase3_model_calibration.md",
        reports / "phase3_model_calibration.json",
        cal_md,
        cal_payload,
    )

    bt_payload = research_contract(
        measured_data={
            "validation_candidates": backtest_val.candidates,
            "test_candidates": backtest_test.candidates,
            "validation_executable_trades": backtest_val.executable_trades,
            "test_executable_trades": backtest_test.executable_trades,
            "validation_descriptive_analysis": _descriptive_dict(backtest_val),
            "test_descriptive_analysis": _descriptive_dict(backtest_test),
            "providers": list(providers),
            "sample_assessment": sample_assessment,
            "chronological_split": chronological_split_obj,
            "requested_range": requested_range_obj,
        },
        model_output={
            "validation": _backtest_dict(
                backtest_val, model_metrics=_model_metric_bundle(metrics.get("validation"))
            ),
            "test": _backtest_dict(
                backtest_test, model_metrics=_model_metric_bundle(metrics.get("test"))
            ),
            "thresholds": list(EDGE_THRESHOLDS),
            "fixed_size": 1.0,
            "leverage": False,
            "compounding": False,
            "kelly": False,
        },
        assumptions={
            "raw_edge_definition": "model_probability - descriptive_market_probability",
            "executable_requires_historical_ask": True,
            "thresholds": list(EDGE_THRESHOLDS),
            "no_leverage_compounding_kelly": True,
            **shared_assumptions,
        },
        missing_data={
            "historical_asks": True,
            "executable_fills": True,
            "fees_slippage": True,
            "validation_reason": backtest_val.reason,
            "test_reason": backtest_test.reason,
            "threshold_selection_reason": backtest_val.threshold_selection_reason,
            **shared_missing,
        },
        inferences={
            "classification": "descriptive/non-executable",
            "alpha_claimed": False,
            "executable_trades": 0,
            "sample_conclusion": sample_assessment["conclusion"],
            "no_alpha_or_executability": (
                "No alpha claim: executable trades=0 without historical asks."
            ),
        },
        limitations=limitation_block,
        extra={
            "validation": _backtest_dict(
                backtest_val, model_metrics=_model_metric_bundle(metrics.get("validation"))
            ),
            "test": _backtest_dict(
                backtest_test, model_metrics=_model_metric_bundle(metrics.get("test"))
            ),
            "thresholds": list(EDGE_THRESHOLDS),
            "fixed_size": 1.0,
            "leverage": False,
            "compounding": False,
            "kelly": False,
            "sample_assessment": sample_assessment,
            "common_research_context": common_ctx,
        },
    )
    bt_md = render_markdown(
        title="Phase 3 backtest",
        measured=(
            f"validation_candidates={backtest_val.candidates}",
            f"test_candidates={backtest_test.candidates}",
            f"validation_candidates_by_threshold={_stable(backtest_val.candidates_by_threshold)}",
            f"test_candidates_by_threshold={_stable(backtest_test.candidates_by_threshold)}",
            f"validation_executable_trades={backtest_val.executable_trades}",
            f"test_executable_trades={backtest_test.executable_trades}",
            (
                "validation_descriptive_averages="
                f"market_p={backtest_val.average_descriptive_market_probability} "
                f"model_p={backtest_val.average_model_probability} "
                f"raw_edge={backtest_val.average_raw_edge}"
            ),
            (
                "test_descriptive_averages="
                f"market_p={backtest_test.average_descriptive_market_probability} "
                f"model_p={backtest_test.average_model_probability} "
                f"raw_edge={backtest_test.average_raw_edge}"
            ),
            (
                "Descriptive mispricing summaries below are validation/test descriptive "
                "only; not executable results."
            ),
            f"validation_descriptive_analysis={_stable(_descriptive_dict(backtest_val))}",
            f"test_descriptive_analysis={_stable(_descriptive_dict(backtest_test))}",
            scored_line,
        ),
        model_output=(
            f"validation_status={backtest_val.status}",
            f"test_status={backtest_test.status}",
            "pnl/roi/drawdown/profit_factor/win_rate/average_executable_entry_price "
            "are null when no executable trades exist.",
            f"selected_threshold={backtest_val.selected_threshold}",
            f"threshold_selection_reason={backtest_val.threshold_selection_reason}",
            (
                "model_metrics_copied_from_calibration="
                f"validation={_stable(_model_metric_bundle(metrics.get('validation')))} "
                f"test={_stable(_model_metric_bundle(metrics.get('test')))}"
            ),
        ),
        assumptions=(
            "raw_edge = model_probability - descriptive_market_probability.",
            "Executable edge requires a sourced historical ask, which these APIs do not provide.",
            "Thresholds predeclared: 0.05, 0.10, 0.15, 0.20. Fixed size, no leverage/compounding/Kelly.",
            "Validation threshold selection did not occur because historical asks are absent.",
            "raw_edge buckets: <=0%; (0,5%]; (5,10%]; (10,15%]; (15,20%]; >20%.",
            "entry-price buckets use descriptive market p; 1c/3c/5c/10c begin bands; "
            "exactly 25c is in 10-25c; >25c is strict.",
            "lead-time uses forecast_lead_hours; exact 1h is 1-6h; exact 6/12/24/48 stay "
            "in the lower-labeled band.",
            "Group breakdowns by city/station/event_month/season/bucket_region are "
            "descriptive counts only and do not imply causal alpha.",
        ),
        missing=(
            backtest_val.reason,
            backtest_test.reason,
            backtest_val.threshold_selection_reason or "",
            "Fees and slippage are not applied because there are no fills.",
        ),
        inferences=(
            "This run is classified descriptive/non-executable.",
            "No alpha claim: executable trades=0.",
            f"Sample conclusion: {sample_assessment['conclusion']}.",
        ),
        extra_limitations=(split_line, sample_line),
    )
    write_report_pair(
        reports / "phase3_backtest.md",
        reports / "phase3_backtest.json",
        bt_md,
        bt_payload,
    )

    tail_payload = research_contract(
        measured_data={
            "split_name": tail.split_name,
            "bands": [asdict(band) for band in tail.bands],
            "max_band_settled_yes_share": tail.max_band_settled_yes_share,
            "providers": list(providers),
            "sample_assessment": sample_assessment,
            "chronological_split": chronological_split_obj,
            "requested_range": requested_range_obj,
        },
        model_output={
            "jackpot_concentration": tail.jackpot_concentration,
            "executable_survival": tail.executable_survival,
            "executable_survival_status": tail.executable_survival_status,
            "pnl": tail.pnl,
            "roi": tail.roi,
            "max_drawdown": tail.max_drawdown,
            "profit_factor": tail.profit_factor,
            "robustness_remove_largest_1_pnl": tail.robustness_remove_largest_1_pnl,
            "robustness_remove_largest_3_pnl": tail.robustness_remove_largest_3_pnl,
            "robustness_remove_largest_5_pnl": tail.robustness_remove_largest_5_pnl,
        },
        assumptions={
            "tail_bands": ["<1c", "1-3c", "3-5c"],
            "settled_yes_uses_gamma_labels": True,
            "max_band_share_is_count_not_return": True,
            **shared_assumptions,
        },
        missing_data={
            "historical_asks": True,
            "executable_survival": {
                "value": tail.executable_survival,
                "status": tail.executable_survival_status,
                "reason": "historical asks unavailable"
                if tail.executable_survival is None
                else None,
            },
            "executable_survival_status": tail.executable_survival_status,
            "notes": list(tail.notes),
            "pnl": {"value": None, "reason": "no executable fills"},
            "roi": {"value": None, "reason": "no executable fills"},
            "max_drawdown": {"value": None, "reason": "no executable fills"},
            "profit_factor": {"value": None, "reason": "no executable fills"},
            **shared_missing,
        },
        inferences={
            "tail_alpha_claimed": False,
            "oos_descriptive_only": True,
            "sample_conclusion": sample_assessment["conclusion"],
            "no_alpha_or_executability": ("No tail-alpha claim without executable fills."),
        },
        limitations=limitation_block,
        extra={
            "split_name": tail.split_name,
            "bands": [asdict(band) for band in tail.bands],
            "executable_survival": tail.executable_survival,
            "executable_survival_status": tail.executable_survival_status,
            "jackpot_concentration": tail.jackpot_concentration,
            "max_band_settled_yes_share": tail.max_band_settled_yes_share,
            "pnl": tail.pnl,
            "roi": tail.roi,
            "max_drawdown": tail.max_drawdown,
            "profit_factor": tail.profit_factor,
            "robustness_remove_largest_1_pnl": tail.robustness_remove_largest_1_pnl,
            "robustness_remove_largest_3_pnl": tail.robustness_remove_largest_3_pnl,
            "robustness_remove_largest_5_pnl": tail.robustness_remove_largest_5_pnl,
            "notes": list(tail.notes),
            "sample_assessment": sample_assessment,
            "common_research_context": common_ctx,
        },
    )
    tail_md = render_markdown(
        title="Phase 3 tail alpha",
        measured=tuple(
            f"{band.band}: n={band.n} settled_yes_count={band.settled_yes_count} "
            f"yes_frac={band.settled_yes_fraction} "
            f"mean_model_p={band.mean_model_probability} mean_raw_edge={band.mean_raw_edge}"
            for band in tail.bands
        ),
        model_output=(
            f"jackpot_concentration={tail.jackpot_concentration} (null; not return concentration)",
            f"max_band_settled_yes_share={tail.max_band_settled_yes_share}",
            f"executable_survival={tail.executable_survival} "
            f"status={tail.executable_survival_status}",
            "Largest 1/3/5 removal robustness PnL is null without fills.",
        ),
        assumptions=(
            "Tail bands are descriptive market prices: <1c, 1-3c, 3-5c.",
            "Settled YES counts/fractions use Gamma settlement labels.",
            "max_band_settled_yes_share is a YES-count share across bands, not return concentration.",
        ),
        missing=(
            f"executable_survival=null status={tail.executable_survival_status} "
            "because historical asks are absent.",
            *tail.notes,
        ),
        inferences=(
            "No tail-alpha claim is made without executable fills.",
            "OOS breakdown is descriptive only on the test dates.",
            f"Sample conclusion: {sample_assessment['conclusion']}.",
        ),
        extra_limitations=(split_line, sample_line),
    )
    write_report_pair(
        reports / "phase3_tail_alpha.md",
        reports / "phase3_tail_alpha.json",
        tail_md,
        tail_payload,
    )


def _backtest_dict(
    result: BacktestEvaluation,
    *,
    model_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    thresholds = result.candidates_by_threshold or {}
    return {
        "status": result.status,
        "reason": result.reason,
        "split_name": result.split_name,
        "candidates": result.candidates,
        "executable_trades": result.executable_trades,
        "pnl": result.pnl,
        "roi": result.roi,
        "max_drawdown": result.max_drawdown,
        "profit_factor": result.profit_factor,
        "win_rate": result.win_rate,
        "average_executable_entry_price": result.average_executable_entry_price,
        "average_descriptive_market_probability": result.average_descriptive_market_probability,
        "average_model_probability": result.average_model_probability,
        "average_raw_edge": result.average_raw_edge,
        "fees_mode": result.fees_mode,
        "thresholds": list(result.thresholds),
        "selected_threshold": result.selected_threshold,
        "threshold_selection_reason": result.threshold_selection_reason,
        "candidates_by_threshold": {str(key): value for key, value in thresholds.items()},
        "descriptive_analysis": _descriptive_dict(result),
        "model_metrics": model_metrics
        or {
            "multiclass_brier": None,
            "clipped_log_loss": None,
            "ece": None,
            "source": "phase3_model_calibration",
        },
    }


def _descriptive_dict(result: BacktestEvaluation) -> dict[str, Any] | None:
    if result.descriptive_analysis is None:
        return None
    return result.descriptive_analysis.as_dict()


def _model_metric_bundle(split_metrics: Any) -> dict[str, Any]:
    if not isinstance(split_metrics, dict):
        return {
            "multiclass_brier": None,
            "clipped_log_loss": None,
            "ece": None,
            "source": "phase3_model_calibration",
            "note": "copied pointer to already-computed validation/test model metrics",
        }
    return {
        "multiclass_brier": split_metrics.get("multiclass_brier"),
        "clipped_log_loss": split_metrics.get("clipped_log_loss"),
        "ece": split_metrics.get("ece"),
        "source": "phase3_model_calibration",
        "note": "copied from already-computed validation/test model metrics; not execution PnL",
    }


def _stable(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
