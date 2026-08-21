from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from click.testing import CliRunner

from weather_alpha.cli import main
from weather_alpha.phase35.full_collection.audit import ExpectedCell
from weather_alpha.phase35.full_collection.clob_contract import (
    canonical_clob_identity,
    plan_clob_gets,
)
from weather_alpha.phase35.full_collection.policy import (
    CLOB_FIDELITY_MINUTES,
    PARSER_SCHEMA_VERSION,
    PRICE_PROVIDER,
)
from weather_alpha.phase35.full_collection.v2_protocol import (
    PitClassification,
    classify_market_pit,
    derive_correction_plan,
    derive_t0_axis,
    derive_track_eligibility,
    market_relative_targets,
    offline_v2_corpus_audit,
    summarize_v2_counts,
    v2_readiness_state,
)
from weather_alpha.research.prices import PricePoint


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _ledger_line(
    *,
    identity: str,
    market: str,
    start_ts: int,
    end_ts: int,
    classification: str,
    collection_id: str = "phase35-clob-recovery-test",
    fidelity: int = CLOB_FIDELITY_MINUTES,
) -> str:
    payload = {
        "attempt_number": 1,
        "attempt_timestamp_utc": "2026-05-19T12:00:00+00:00",
        "canonical_request_identity": identity,
        "collection_id": collection_id,
        "collection_status": None,
        "content_sha256": None,
        "endpoint": "https://clob.polymarket.com/prices-history",
        "error_class": None,
        "error_detail": None,
        "hash_algorithm": "sha256",
        "http_method": "GET",
        "http_status": 200 if classification == "SUCCESS" else None,
        "latency_ms": 1.0,
        "normalized_request_parameters": {
            "endTs": end_ts,
            "fidelity": fidelity,
            "market": market,
            "startTs": start_ts,
        },
        "parser_schema_version": PARSER_SCHEMA_VERSION,
        "provider": PRICE_PROVIDER,
        "result_classification": classification,
        "retry_after_seconds": None,
        "stable_raw_provenance_path": None,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _dt(hour: int) -> datetime:
    return datetime(2026, 5, 19, hour, 0, tzinfo=UTC)


def test_no_pre_decision_price_is_not_future_leakage() -> None:
    decision = _dt(12)
    classification = classify_market_pit(
        selected_price=None,
        decision_ts=decision,
        price_history_present=True,
    )
    assert classification is PitClassification.NO_PRE_DECISION_PRICE


def test_selected_future_price_is_actual_future_leakage() -> None:
    decision = _dt(12)
    classification = classify_market_pit(
        selected_price=PricePoint(observed_at=_dt(13), price=0.55),
        decision_ts=decision,
        price_history_present=True,
    )
    assert classification is PitClassification.ACTUAL_FUTURE_LEAKAGE


def test_price_history_empty_is_distinct() -> None:
    decision = _dt(12)
    classification = classify_market_pit(
        selected_price=None,
        decision_ts=decision,
        price_history_present=False,
    )
    assert classification is PitClassification.PRICE_HISTORY_EMPTY


def test_t0_censoring_and_uncensored_cases() -> None:
    request_start = _dt(0)
    event_ts = _dt(23)
    uncensored = derive_t0_axis(
        request_start_ts=request_start,
        event_ts=event_ts,
        points=[PricePoint(observed_at=request_start + timedelta(hours=2), price=0.2)],
    )
    assert uncensored.t0_uncensored is True
    assert uncensored.t0_left_censored is False
    censored = derive_t0_axis(
        request_start_ts=request_start,
        event_ts=event_ts,
        points=[PricePoint(observed_at=request_start + timedelta(minutes=45), price=0.2)],
    )
    assert censored.t0_left_censored is True
    assert censored.t0_uncensored is False


def test_market_age_is_distinct_from_pit() -> None:
    t0 = _dt(0)
    points = [
        PricePoint(observed_at=t0, price=0.25),
        PricePoint(observed_at=t0 + timedelta(hours=2), price=0.30),
    ]
    targets = market_relative_targets(
        t0_ts=t0,
        points=points,
        valid_boundary_end_ts=t0 + timedelta(hours=12),
    )
    t12 = next(row for row in targets if row.label == "T0+12h")
    assert t12.pit_valid is True
    assert t12.price_age_at_target_seconds is not None
    assert t12.price_age_at_target_seconds > 6 * 3600


def test_track_c_left_censored_is_retained_not_primary() -> None:
    tracks = derive_track_eligibility(
        forecast_pit_valid=True,
        settlement_present=True,
        market_pit_valid=True,
        t0_uncensored=False,
        within_boundary=True,
    )
    assert tracks.track_a_forecast_calibration is True
    assert tracks.track_b_fixed_time_market_alpha is True
    assert tracks.track_c_early_market_alpha_primary is False
    assert tracks.track_c_left_censored_cohort is True


def test_counts_matrix_explicit_fields() -> None:
    rows = [
        {
            "forecast_available": True,
            "market_price_history_present": True,
            "market_observable": True,
            "pipeline_success": True,
            "analysis_eligible": True,
            "pit_classification": PitClassification.PIT_VALID.value,
        },
        {
            "forecast_available": True,
            "market_price_history_present": False,
            "market_observable": False,
            "pipeline_success": True,
            "analysis_eligible": False,
            "pit_classification": PitClassification.NO_PRE_DECISION_PRICE.value,
        },
    ]
    counts = summarize_v2_counts(rows)
    assert counts.EXPECTED_CELL_COUNT == 2
    assert counts.MARKET_UNOBSERVABLE_COUNT == 1
    assert counts.NO_PRE_DECISION_PRICE_COUNT == 1


def test_shared_token_non_alias_rejected_and_cross_assignment_detected() -> None:
    families = [
        {
            "event_family_id": "london-a",
            "date": "2026-05-19",
            "timezone_name": "Europe/London",
            "member_condition_ids": ["cond-a"],
            "yes_token_ids": ["tok-a", "tok-shared"],
        },
        {
            "event_family_id": "london-b",
            "date": "2026-05-19",
            "timezone_name": "Europe/London",
            "member_condition_ids": ["cond-b"],
            "yes_token_ids": ["tok-b", "tok-shared"],
        },
    ]
    clob_cell_map = {
        "wrong-identity-a": [{"event_family_id": "london-a"}],
        "wrong-identity-b": [{"event_family_id": "london-b"}],
    }
    clob_parsed = [
        {"identity": "wrong-identity-a", "params": {"market": "tok-b"}},
        {"identity": "wrong-identity-b", "params": {"market": "tok-a"}},
    ]
    plan = derive_correction_plan(
        families=families,
        clob_cell_map=clob_cell_map,
        clob_parsed=clob_parsed,
    )
    assert plan.cross_assigned_family_count == 2
    assert plan.token_ownership_violation_count == 2
    assert plan.correction_recovery_required is True
    assert plan.correction_gamma_identity_count == 0
    assert plan.correction_ecmwf_identity_count == 0


def test_correction_plan_derives_missing_family_owned_histories() -> None:
    families = [
        {
            "event_family_id": "amsterdam",
            "date": "2026-05-19",
            "timezone_name": "Europe/Amsterdam",
            "yes_token_ids": ["tok-amsterdam"],
        },
        {
            "event_family_id": "new-york",
            "date": "2026-05-19",
            "timezone_name": "America/New_York",
            "yes_token_ids": ["tok-nyc"],
        },
    ]
    clob_cell_map = {
        "identity-amsterdam": [{"event_family_id": "amsterdam"}],
        "identity-new-york": [{"event_family_id": "new-york"}],
    }
    ams_identity = canonical_clob_identity(
        market="tok-amsterdam",
        start_ts=1747612800,
        end_ts=1747782000,
        fidelity=60,
    )
    clob_parsed = [
        {"identity": ams_identity, "params": {"market": "tok-amsterdam"}},
        {"identity": "identity-new-york", "params": {"market": "tok-amsterdam"}},
    ]
    plan = derive_correction_plan(
        families=families,
        clob_cell_map=clob_cell_map,
        clob_parsed=clob_parsed,
    )
    assert plan.correction_recovery_required is True
    assert plan.correction_clob_identity_count == 1


def test_v2_readiness_stays_not_yet_established_without_recovery_and_freeze() -> None:
    state = v2_readiness_state(
        v2_implemented=True,
        correction_recovery_executed=False,
        final_v2_audit_passed=False,
        frozen=False,
    )
    assert state.PHASE35B_V2_IMPLEMENTED == "YES"
    assert state.PHASE35B_V2_FROZEN == "NO"
    assert state.PHASE35B_V2_DATASET_READY == "NOT_YET_ESTABLISHED"


def test_offline_v2_corpus_audit_is_deterministic(tmp_path: Path) -> None:
    ns = tmp_path / "collection"
    (ns / "events").mkdir(parents=True)
    (ns / "plans").mkdir(parents=True)
    (ns / "parsed").mkdir(parents=True)
    (ns / "events" / "accepted.json").write_text(
        '[{"event_family_id":"f1","date":"2026-05-19","timezone_name":"UTC","yes_token_ids":["tok-1"]}]',
        encoding="utf-8",
    )
    (ns / "plans" / "clob_cell_map.json").write_text(
        '{"id-1":[{"event_family_id":"f1"}]}',
        encoding="utf-8",
    )
    (ns / "parsed" / "clob.json").write_text(
        '[{"identity":"id-1","params":{"market":"tok-x"}}]',
        encoding="utf-8",
    )
    (ns / "observations.json").write_text(
        '[{"forecast_available":true,"market_price_history_present":false,"market_observable":false,"pipeline_success":true,"analysis_eligible":false,"pit_classification":"NO_PRE_DECISION_PRICE"}]',
        encoding="utf-8",
    )
    first = offline_v2_corpus_audit(ns)
    second = offline_v2_corpus_audit(ns)
    assert first == second
    assert first["PHASE35B_V2_DATASET_READY"] == "NOT_YET_ESTABLISHED"


def test_real_planner_shared_token_is_fail_closed() -> None:
    """
    Non-alias multi-family shared tokens must not be selected via positional logic.
    """

    # Shared token sorts first, so the current tokens[0] planner would pick it.
    shared = "tok-shared"
    family_a_token = "tok-a"
    family_b_token = "tok-b"

    families = [
        {
            "city": "paris",
            "date": "2026-03-01",
            "event_family_id": "event_id:a",
            "has_settlement": True,
            "station": "LFPG",
            "timezone_name": "Europe/Paris",
            "yes_token_ids": [shared, family_a_token],
        },
        {
            "city": "london",
            "date": "2026-03-01",
            "event_family_id": "event_id:b",
            "has_settlement": True,
            "station": "EGLC",
            "timezone_name": "Europe/London",
            "yes_token_ids": [shared, family_b_token],
        },
    ]

    expected: tuple[ExpectedCell, ...] = (
        ExpectedCell(
            date="2026-03-01",
            city="paris",
            station="LFPG",
            checkpoint=48,
            event_family_id="event_id:a",
            month="2026-03",
            ecmwf_run_cycle=None,
        ),
        ExpectedCell(
            date="2026-03-01",
            city="london",
            station="EGLC",
            checkpoint=48,
            event_family_id="event_id:b",
            month="2026-03",
            ecmwf_run_cycle=None,
        ),
    )

    plans, mapping = plan_clob_gets(expected, families)
    assert plans, "planner must still return plans for uniquely-owned tokens"

    planned_market_by_family: dict[str, str] = {}
    for planned in plans:
        planned_token = str(planned.params.get("market") or "")
        for cell in mapping.get(planned.identity) or []:
            fid = str(cell.get("event_family_id") or "")
            if fid:
                planned_market_by_family[fid] = planned_token

    assert planned_market_by_family["event_id:a"] == family_a_token
    assert planned_market_by_family["event_id:b"] == family_b_token


def test_offline_v2_corpus_audit_recomputes_real_pit_taxonomy_and_corrected_counts() -> None:
    rec = Path("data/phase35/historical/recoveries/phase35-clob-recovery-1ea1f85f6672")
    result = offline_v2_corpus_audit(rec)

    # Corrected planning: cross-assigned families are fail-closed/quarantined.
    assert result["CROSS_ASSIGNED_FAMILY_COUNT_CORRECTED_PLANNER"] == 0
    assert result["TOKEN_OWNERSHIP_VIOLATION_COUNT_CORRECTED_PLANNER"] == 0

    # PIT taxonomy recomputation from persisted clob.json points (not legacy absent fields).
    assert result["ACTUAL_SELECTED_FUTURE_PRICE_COUNT"] == 0
    # Unresolved cross-assigned cells are excluded from market PIT taxonomy counts.
    assert result["UNRESOLVED_CORRECTION_REQUIRED_CELL_COUNT"] == 30
    assert result["NO_PRE_DECISION_PRICE_CELL_COUNT"] == 348
    assert result["PRICE_HISTORY_EMPTY_FAMILY_COUNT"] == 4
    assert result["PRICE_HISTORY_EMPTY_CELL_COUNT"] == 24

    # T0 censoring classification from persisted CLOB series geometry.
    # Donor-owned identities remain; victim-only wrong prices are not substituted.
    assert result["T0_LEFT_CENSORED_TOKEN_COUNT"] == 8
    assert result["T0_UNCENSORED_TOKEN_COUNT"] == 423

    # Track eligibility: unresolved cross-assignments excluded from Track-B/C.
    assert result["TRACK_A_ELIGIBLE_COUNT"] == 2640
    assert result["TRACK_B_ELIGIBLE_COUNT"] == 2238
    assert result["TRACK_C_PRIMARY_ELIGIBLE_COUNT"] == 2190

    # Fixed checkpoints survive (48h must remain present); unresolved excluded.
    assert result["48H_MARKET_OBSERVABLE_COUNT"] == 431
    assert result["48H_MARKET_UNOBSERVABLE_COUNT"] == 4

    # Correction identities are ledger-derived and still unresolved in this pass.
    assert result["UNRESOLVED_CORRECTION_CLOB_IDENTITY_COUNT"] == 5
    assert result["CORRECTION_GAMMA_IDENTITY_COUNT"] == 0
    assert result["CORRECTION_ECMWF_IDENTITY_COUNT"] == 0

    assert result["PHASE35B_V2_DATASET_READY"] == "NOT_YET_ESTABLISHED"
    assert result["FIXED_TIME_MARKET_ALPHA_DATA_READY"] == "BLOCKED_PENDING_CORRECTION"
    assert result["EARLY_MARKET_ALPHA_DATA_READY"] == "BLOCKED_PENDING_CORRECTION"


def test_offline_v2_corpus_audit_unresolved_correction_identities_match_expected_set() -> None:
    rec = Path("data/phase35/historical/recoveries/phase35-clob-recovery-1ea1f85f6672")
    result = offline_v2_corpus_audit(rec)

    expected = {
        "clob:range:0e5e70dd308314b2852f0b07b63a8be38ee83a8d09bd138e494fda05b6a34ca5",
        "clob:range:4ba45ae25dc365d0ec3198b1b90be361a9ad04edaecba25e1a9b67290d4d00a3",
        "clob:range:85b91fa405262a54698ad649c576b0881e55d97ad4fb21d9b8bd6974c0f2a789",
        "clob:range:bec243e7a820d4f77e3fe0dec77b0c841611bd9948ef7e4b11e94c4f6ea7ac9b",
        "clob:range:fae1e9afa121cb5f71ef855966406b30b8e5001637146bf425580d5f2351e4c9",
    }
    assert set(result["UNRESOLVED_CORRECTION_CLOB_IDENTITIES"]) == expected


def test_correction_plan_uses_ledger_request_params_and_excludes_successful_correct_ids(
    tmp_path: Path,
) -> None:
    """Ledger is authoritative for requested identity/token/window and success exclusion."""

    start_ts = 1_748_000_000
    end_ts = 1_748_100_000
    wrong_token = "tok-wrong"
    correct_token = "tok-family"
    wrong_identity = canonical_clob_identity(
        market=wrong_token, start_ts=start_ts, end_ts=end_ts, fidelity=60
    )
    correct_identity = canonical_clob_identity(
        market=correct_token, start_ts=start_ts, end_ts=end_ts, fidelity=60
    )
    # Parsed deliberately disagrees with ledger on market/window; ledger must win.
    families = [
        {
            "event_family_id": "fam-a",
            "date": "2026-05-19",
            "timezone_name": "UTC",
            "yes_token_ids": [correct_token],
        }
    ]
    clob_cell_map = {wrong_identity: [{"event_family_id": "fam-a"}]}
    clob_parsed = [
        {
            "identity": wrong_identity,
            "params": {"market": "tok-parsed-lie", "startTs": 1, "endTs": 2, "fidelity": 60},
        }
    ]
    ledger_path = tmp_path / "ledger.jsonl"
    ledger_path.write_text(
        _ledger_line(
            identity=wrong_identity,
            market=wrong_token,
            start_ts=start_ts,
            end_ts=end_ts,
            classification="SUCCESS",
        )
        + "\n",
        encoding="utf-8",
    )

    unresolved = derive_correction_plan(
        families=families,
        clob_cell_map=clob_cell_map,
        clob_parsed=clob_parsed,
        ledger_path=ledger_path,
    )
    assert unresolved.correction_clob_identity_count == 1
    assert unresolved.unresolved_correction_clob_identities == (correct_identity,)
    assert len(unresolved.provenance) == 1
    prov = unresolved.provenance[0]
    assert prov.incorrect_old_identity == wrong_identity
    assert prov.incorrect_old_token == wrong_token
    assert prov.correct_token == correct_token
    assert prov.start_ts == start_ts
    assert prov.end_ts == end_ts
    assert prov.fidelity == 60

    # Successful collection of the correct identity excludes it from the plan.
    ledger_path.write_text(
        _ledger_line(
            identity=wrong_identity,
            market=wrong_token,
            start_ts=start_ts,
            end_ts=end_ts,
            classification="SUCCESS",
        )
        + "\n"
        + _ledger_line(
            identity=correct_identity,
            market=correct_token,
            start_ts=start_ts,
            end_ts=end_ts,
            classification="SUCCESS",
        )
        + "\n",
        encoding="utf-8",
    )
    resolved = derive_correction_plan(
        families=families,
        clob_cell_map=clob_cell_map,
        clob_parsed=clob_parsed,
        ledger_path=ledger_path,
    )
    assert resolved.correction_clob_identity_count == 0
    assert resolved.unresolved_correction_clob_identities == ()
    assert resolved.correction_recovery_required is False


def test_cli_phase35_audit_historical_emits_authoritative_v2_audit(
    tmp_path: Path,
) -> None:
    """CLI must emit authoritative V2 audit/report into a temp namespace (offline)."""

    collection_id = "phase35-v2-cli-temp"
    root = tmp_path / "collections"
    ns = root / collection_id
    historical_root = Path("data/phase35/historical")
    historical_before = {
        path: path.stat().st_mtime_ns for path in historical_root.rglob("*") if path.is_file()
    }

    start_ts = 1_748_000_000
    end_ts = 1_748_100_000
    wrong_token = "tok-wrong"
    correct_token = "tok-family"
    wrong_identity = canonical_clob_identity(
        market=wrong_token, start_ts=start_ts, end_ts=end_ts, fidelity=60
    )
    cell = {
        "date": "2026-05-19",
        "city": "london",
        "station": "EGLC",
        "checkpoint": 24,
        "event_family_id": "fam-a",
        "month": "2026-05",
        "ecmwf_run_cycle": None,
    }
    _write_json(ns / "expected_cells.json", [cell])
    _write_json(
        ns / "observations.json",
        [
            {
                **cell,
                "observed": True,
                "usable": False,
                "has_settlement": True,
                "scored": False,
                "has_price_history": True,
                "future_leakage": False,
                "retrospective_substitution": False,
                "raw_hash_ok": True,
                "topology_valid": True,
                "topology_reviewed_quarantine": False,
                "missing_reasons": [],
            }
        ],
    )
    _write_json(
        ns / "events" / "accepted.json",
        [
            {
                "event_family_id": "fam-a",
                "date": "2026-05-19",
                "timezone_name": "UTC",
                "city": "london",
                "station": "EGLC",
                "has_settlement": True,
                "yes_token_ids": [correct_token],
            }
        ],
    )
    _write_json(ns / "plans" / "clob_cell_map.json", {wrong_identity: [cell]})
    _write_json(ns / "parsed" / "clob.json", [])
    (ns / "ledger.jsonl").write_text(
        _ledger_line(
            identity=wrong_identity,
            market=wrong_token,
            start_ts=start_ts,
            end_ts=end_ts,
            classification="SUCCESS",
        )
        + "\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "phase35-audit-historical",
            "--collection-id",
            collection_id,
            "--collection-root",
            str(root),
        ],
    )
    assert result.exit_code in {0, 2}, result.output
    assert "PHASE35B_V2_DATASET_READY" in result.output
    assert "UNRESOLVED_CORRECTION_CLOB_IDENTITY_COUNT" in result.output
    assert "BLOCKED_PENDING_CORRECTION" in result.output

    audit_json = ns / "reports" / "phase35_historical_audit.json"
    assert audit_json.is_file()
    payload = json.loads(audit_json.read_text(encoding="utf-8"))
    measured = payload["measured_data"]
    assert "PHASE35B_V2_AUDIT" in measured
    v2 = measured["PHASE35B_V2_AUDIT"]
    assert v2["UNRESOLVED_CORRECTION_CLOB_IDENTITY_COUNT"] == 1
    assert v2["PHASE35B_V2_DATASET_READY"] == "NOT_YET_ESTABLISHED"
    assert payload["model_output"]["FIXED_TIME_MARKET_ALPHA_DATA_READY"] == (
        "BLOCKED_PENDING_CORRECTION"
    )

    historical_after = {
        path: path.stat().st_mtime_ns for path in historical_root.rglob("*") if path.is_file()
    }
    assert historical_after == historical_before


def test_unresolved_cross_assigned_excluded_from_all_market_derived_metrics(
    tmp_path: Path,
) -> None:
    """Unresolved cross-assigned families never feed market-derived metrics or prices."""

    request_start = datetime(2026, 5, 18, 0, 0, tzinfo=UTC)
    start_ts = int(request_start.timestamp())
    end_ts = int(datetime(2026, 5, 19, 23, 0, tzinfo=UTC).timestamp())
    first_obs = request_start + timedelta(hours=2)

    owned_a = "tok-a"
    owned_b = "tok-b"
    identity_a = canonical_clob_identity(
        market=owned_a, start_ts=start_ts, end_ts=end_ts, fidelity=60
    )
    # Family B was wrongly assigned family A's token/identity.
    families = [
        {
            "event_family_id": "fam-a",
            "date": "2026-05-19",
            "timezone_name": "UTC",
            "city": "london",
            "station": "EGLC",
            "has_settlement": True,
            "yes_token_ids": [owned_a],
        },
        {
            "event_family_id": "fam-b",
            "date": "2026-05-19",
            "timezone_name": "UTC",
            "city": "milan",
            "station": "LIML",
            "has_settlement": True,
            "yes_token_ids": [owned_b],
        },
    ]
    cells = []
    for family_id, city, station in (
        ("fam-a", "london", "EGLC"),
        ("fam-b", "milan", "LIML"),
    ):
        for checkpoint in (48, 24, 12, 6, 3, 1):
            cells.append(
                {
                    "date": "2026-05-19",
                    "city": city,
                    "station": station,
                    "checkpoint": checkpoint,
                    "event_family_id": family_id,
                    "month": "2026-05",
                    "ecmwf_run_cycle": None,
                }
            )

    ns = tmp_path / "ns"
    _write_json(ns / "events" / "accepted.json", families)
    _write_json(ns / "expected_cells.json", cells)
    _write_json(
        ns / "plans" / "clob_cell_map.json",
        {identity_a: [cell for cell in cells if cell["event_family_id"] in {"fam-a", "fam-b"}]},
    )
    _write_json(
        ns / "parsed" / "clob.json",
        [
            {
                "identity": identity_a,
                "params": {
                    "market": owned_a,
                    "startTs": start_ts,
                    "endTs": end_ts,
                    "fidelity": 60,
                },
                "points": [
                    {
                        "observed_at": first_obs.isoformat(),
                        "price": 0.42,
                    }
                ],
            }
        ],
    )
    (ns / "ledger.jsonl").write_text(
        _ledger_line(
            identity=identity_a,
            market=owned_a,
            start_ts=start_ts,
            end_ts=end_ts,
            classification="SUCCESS",
        )
        + "\n",
        encoding="utf-8",
    )

    result = offline_v2_corpus_audit(ns)
    assert result["UNRESOLVED_CORRECTION_CLOB_IDENTITY_COUNT"] == 1
    assert result["UNRESOLVED_CORRECTION_REQUIRED_CELL_COUNT"] == 6
    # Only fam-a contributes market-derived support (6 cells / 1 token).
    assert result["MARKET_PRICE_HISTORY_PRESENT_COUNT"] == 6
    assert result["MARKET_OBSERVABLE_COUNT"] == 6
    assert result["T0_LEFT_CENSORED_TOKEN_COUNT"] == 0
    assert result["T0_UNCENSORED_TOKEN_COUNT"] == 1
    # 48h/24h decisions precede first observed price => not Track-B eligible.
    assert result["TRACK_B_ELIGIBLE_COUNT"] == 4
    assert result["TRACK_C_PRIMARY_ELIGIBLE_COUNT"] == 4
    assert result["48H_MARKET_OBSERVABLE_COUNT"] == 1
    assert result["48H_MARKET_UNOBSERVABLE_COUNT"] == 0
    # Market-age denominators exclude unresolved family B.
    t0_support = result["MARKET_AGE_TARGET_SUPPORT"]["T0"]
    assert t0_support["eligible_denominator"] == 4
    assert t0_support["pit_valid_count"] == 4
    assert t0_support["uncensored_count"] == 4
    # Wrong price must not be treated as selected for unresolved cells.
    assert result["ACTUAL_SELECTED_FUTURE_PRICE_COUNT"] == 0
    assert result["NO_PRE_DECISION_PRICE_CELL_COUNT"] == 2
    assert (
        result["NO_PRE_DECISION_PRICE_CELL_COUNT"]
        + result["UNRESOLVED_CORRECTION_REQUIRED_CELL_COUNT"]
        + result["TRACK_B_ELIGIBLE_COUNT"]
        == 12
    )


def test_v2_readiness_fail_closed_when_unresolved_corrections_remain() -> None:
    state = v2_readiness_state(
        v2_implemented=True,
        correction_recovery_executed=True,
        final_v2_audit_passed=True,
        frozen=True,
        unresolved_correction_count=5,
        track_a_support=True,
    )
    assert state.PHASE35B_V2_DATASET_READY != "YES"
    assert state.PHASE35B_V2_DATASET_READY == "NOT_YET_ESTABLISHED"
    assert state.FIXED_TIME_MARKET_ALPHA_DATA_READY == "BLOCKED_PENDING_CORRECTION"
    assert state.EARLY_MARKET_ALPHA_DATA_READY == "BLOCKED_PENDING_CORRECTION"


def test_v2_readiness_eligible_before_freeze_without_requiring_frozen() -> None:
    """DATASET_READY may be YES as freeze-eligible; FROZEN stays independent/NO."""

    state = v2_readiness_state(
        v2_implemented=True,
        correction_recovery_executed=True,
        final_v2_audit_passed=True,
        frozen=False,
        unresolved_correction_count=0,
        track_a_support=True,
    )
    assert state.PHASE35B_V2_DATASET_READY == "YES"
    assert state.PHASE35B_V2_FROZEN == "NO"
    assert state.FIXED_TIME_MARKET_ALPHA_DATA_READY == "YES"
    assert state.EARLY_MARKET_ALPHA_DATA_READY == "YES"
    assert state.FORECAST_CALIBRATION_DATA_READY == "YES"


def test_v2_readiness_fail_closed_without_track_a_support() -> None:
    """Affirmative DATASET_READY requires track_a_support even when other gates pass."""

    state = v2_readiness_state(
        v2_implemented=True,
        correction_recovery_executed=True,
        final_v2_audit_passed=True,
        frozen=False,
        unresolved_correction_count=0,
        track_a_support=False,
    )
    assert state.PHASE35B_V2_DATASET_READY == "NOT_YET_ESTABLISHED"
    assert state.PHASE35B_V2_FROZEN == "NO"
    assert state.FORECAST_CALIBRATION_DATA_READY == "NO"


def test_v2_readiness_stays_not_ready_when_substantive_gates_fail() -> None:
    """Incomplete recovery/audit must remain NOT_YET_ESTABLISHED; FROZEN still NO."""

    state = v2_readiness_state(
        v2_implemented=True,
        correction_recovery_executed=True,
        final_v2_audit_passed=False,
        frozen=False,
        unresolved_correction_count=0,
        track_a_support=True,
    )
    assert state.PHASE35B_V2_DATASET_READY == "NOT_YET_ESTABLISHED"
    assert state.PHASE35B_V2_FROZEN == "NO"
