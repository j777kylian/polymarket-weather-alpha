"""Scoring integrity: no fabricated winners, event grouping, baseline metrics."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from weather_alpha.research.metrics import multiclass_brier
from weather_alpha.research.model import HistoricalFrequencyBaseline
from weather_alpha.research.run import run_phase3, score_split
from weather_alpha.research.types import ResearchSnapshot, event_group_key


def _snap(
    *,
    token: str,
    event_date: str,
    bucket: str,
    settlement: str | None,
    event_id: str | None,
    station: str = "LFPG",
    city: str = "paris",
    min_c: float = 31.0,
    max_c: float = 31.0,
    condition: str | None = None,
    question: str | None = None,
) -> ResearchSnapshot:
    return ResearchSnapshot(
        condition_id=condition or f"{event_id or 'e'}-{bucket}",
        market_id="m",
        token_id=token,
        city=city,
        station_icao=station,
        event_date=event_date,
        bucket_label=bucket,
        bucket_kind="exact",
        temperature_celsius_min=min_c,
        temperature_celsius_max=max_c,
        decision_ts=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        market_probability=0.4,
        executable_entry_price=None,
        best_bid=None,
        best_ask=None,
        midpoint=None,
        spread=None,
        volume=None,
        liquidity=None,
        weather_issued_at=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        weather_available_at=datetime(2026, 7, 1, 6, 0, tzinfo=UTC),
        forecast_daily_max_c=30.0,
        observation_max_so_far_c=None,
        observation_as_of=None,
        settlement_label=settlement,
        diagnostic_actual_max_c=31.0,
        provenance_urls=(),
        raw_paths=(),
        content_hashes=(),
        limitations=("test",),
        event_id=event_id,
        question=question,
    )


def test_event_group_key_uses_event_id_and_does_not_merge_unrelated_markets() -> None:
    a = _snap(
        token="a",
        event_date="2026-07-15",
        bucket="30°C",
        settlement="Yes",
        event_id="event-a",
        condition="cond-a",
    )
    b = _snap(
        token="b",
        event_date="2026-07-15",
        bucket="31°C",
        settlement="Yes",
        event_id="event-b",
        condition="cond-b",
    )
    c = _snap(
        token="c",
        event_date="2026-07-15",
        bucket="32°C",
        settlement="No",
        event_id="event-a",
        condition="cond-a2",
    )
    assert event_group_key(a) == event_group_key(c)
    assert event_group_key(a) != event_group_key(b)
    fallback = _snap(
        token="d",
        event_date="2026-07-15",
        bucket="30°C",
        settlement="Yes",
        event_id=None,
        city="paris",
        station="LFPG",
        question="Highest temperature in Paris on July 15, 2026?",
    )
    other_city = _snap(
        token="e",
        event_date="2026-07-15",
        bucket="30°C",
        settlement="Yes",
        event_id=None,
        city="london",
        station="EGLC",
        question="Highest temperature in London on July 15, 2026?",
    )
    assert event_group_key(fallback) != event_group_key(other_city)


def test_score_split_skips_zero_or_multiple_yes_winners() -> None:
    none_yes = (
        _snap(token="a", event_date="2026-07-10", bucket="30°C", settlement="No", event_id="e1"),
        _snap(token="b", event_date="2026-07-10", bucket="31°C", settlement="No", event_id="e1"),
    )
    model_p = {"a": 0.4, "b": 0.6}
    scored = score_split(none_yes, model_p, model_p)
    assert scored["scored_events"] == 0
    assert scored["skipped_events"] == 1
    assert "zero YES" in " ".join(scored["skip_reasons"]).lower() or any(
        "zero" in reason.lower() for reason in scored["skip_reasons"]
    )

    two_yes = (
        _snap(token="c", event_date="2026-07-11", bucket="30°C", settlement="Yes", event_id="e2"),
        _snap(token="d", event_date="2026-07-11", bucket="31°C", settlement="Yes", event_id="e2"),
    )
    scored_multi = score_split(two_yes, {"c": 0.4, "d": 0.6}, {"c": 0.5, "d": 0.5})
    assert scored_multi["scored_events"] == 0
    assert any("multiple" in reason.lower() for reason in scored_multi["skip_reasons"])


def test_score_split_skips_missing_or_non_normalized_probabilities() -> None:
    rows = (
        _snap(token="a", event_date="2026-07-10", bucket="30°C", settlement="Yes", event_id="e1"),
        _snap(token="b", event_date="2026-07-10", bucket="31°C", settlement="No", event_id="e1"),
    )
    missing = score_split(rows, {"a": 0.4}, {"a": 0.5, "b": 0.5})
    assert missing["scored_events"] == 0
    assert any("missing" in reason.lower() for reason in missing["skip_reasons"])

    incomplete = score_split(rows, {"a": 0.4, "b": 0.4}, {"a": 0.5, "b": 0.5})
    assert incomplete["scored_events"] == 0
    assert any(
        "normal" in reason.lower() or "sum" in reason.lower()
        for reason in incomplete["skip_reasons"]
    )


def test_score_split_does_not_default_missing_winner_to_index_zero() -> None:
    # If the first bucket is No and there is no Yes, fabricating target 0 would score it.
    rows = (
        _snap(token="a", event_date="2026-07-10", bucket="30°C", settlement="No", event_id="e1"),
        _snap(token="b", event_date="2026-07-10", bucket="31°C", settlement="No", event_id="e1"),
    )
    model_p = {"a": 0.99, "b": 0.01}
    scored = score_split(rows, model_p, model_p)
    assert scored["multiclass_brier"] is None
    assert scored["scored_events"] == 0


def test_score_split_computes_baseline_comparison_metrics() -> None:
    train = (
        _snap(
            token="t1",
            event_date="2026-07-01",
            bucket="30°C",
            settlement="Yes",
            event_id="tr1",
            min_c=30,
            max_c=30,
        ),
        _snap(
            token="t2",
            event_date="2026-07-01",
            bucket="31°C",
            settlement="No",
            event_id="tr1",
            min_c=31,
            max_c=31,
        ),
    )
    test = (
        _snap(
            token="a",
            event_date="2026-07-10",
            bucket="30°C",
            settlement="Yes",
            event_id="te1",
            min_c=30,
            max_c=30,
        ),
        _snap(
            token="b",
            event_date="2026-07-10",
            bucket="31°C",
            settlement="No",
            event_id="te1",
            min_c=31,
            max_c=31,
        ),
    )
    baseline = HistoricalFrequencyBaseline()
    baseline.fit(train)
    baseline_probs = {
        snap.token_id: bucket.probability
        for snap, bucket in zip(test, baseline.predict_event(test), strict=True)
    }
    model_probs = {"a": 0.7, "b": 0.3}
    scored = score_split(test, model_probs, baseline_probs)
    assert scored["scored_events"] == 1
    assert scored["skipped_events"] == 0
    assert scored["multiclass_brier"] == multiclass_brier(((0.7, 0.3),), (0,))
    assert "baseline" in scored
    assert scored["baseline"]["multiclass_brier"] is not None
    assert scored["baseline"]["clipped_log_loss"] is not None
    assert scored["baseline"]["ece"] is not None
    assert scored["baseline"]["probability_sum_ok"] is True
    assert "baseline_token_count" not in scored or scored["baseline"][
        "multiclass_brier"
    ] != scored.get("baseline_token_count")


def test_run_phase3_reports_scored_skipped_and_insufficient_sample(tmp_path: Path) -> None:
    snapshots = (
        _snap(token="a", event_date="2026-07-01", bucket="30°C", settlement="Yes", event_id="e1"),
        _snap(token="b", event_date="2026-07-01", bucket="31°C", settlement="No", event_id="e1"),
        _snap(token="c", event_date="2026-07-08", bucket="30°C", settlement="No", event_id="e2"),
        _snap(token="d", event_date="2026-07-08", bucket="31°C", settlement="No", event_id="e2"),
        _snap(token="e", event_date="2026-07-15", bucket="30°C", settlement="Yes", event_id="e3"),
        _snap(token="f", event_date="2026-07-15", bucket="31°C", settlement="No", event_id="e3"),
    )
    result = run_phase3(
        snapshots,
        output_dir=tmp_path,
        collect_manifest={
            "start_date": "2026-07-01",
            "end_date": "2026-07-15",
            "cities": ["paris"],
            "max_search_pages": 2,
            "search_limit_per_type": 50,
            "discovered_outside_range": 4,
            "snapshots": 6,
            "price_http_errors": 2,
            "price_history_empty": 1,
        },
    )
    test_metrics = result.metrics["test"]
    assert test_metrics["scored_events"] + test_metrics["skipped_events"] >= 1
    assert "skip_reasons" in test_metrics
    cal = (tmp_path / "reports" / "phase3_model_calibration.json").read_text(encoding="utf-8")
    assert "HistoricalFrequencyBaseline" in cal or "baseline" in cal
    md = (tmp_path / "reports" / "phase3_model_calibration.md").read_text(encoding="utf-8")
    for section in ("MEASURED DATA", "MODEL OUTPUT", "ASSUMPTIONS", "MISSING DATA", "INFERENCES"):
        assert section in md
    assert "Open-Meteo" in md or "open-meteo" in md.lower()
    assert "insufficient" in md.lower() or "inconclusive" in md.lower()
    audit_md = (tmp_path / "reports" / "phase3_dataset_audit.md").read_text(encoding="utf-8")
    assert "2026-07-01" in audit_md
    assert "discovered_outside_range" in audit_md or "outside_range" in audit_md
    assert "price_http_errors=2" in audit_md
    assert "price_history_empty=1" in audit_md
    bt = (tmp_path / "reports" / "phase3_backtest.md").read_text(encoding="utf-8")
    assert "threshold selection" in bt.lower()
    assert (
        "did not" in bt.lower()
        or "not occur" in bt.lower()
        or "no validation threshold" in bt.lower()
    )
    tail = (tmp_path / "reports" / "phase3_tail_alpha.md").read_text(encoding="utf-8")
    assert "jackpot" in tail.lower()
    assert "null" in tail.lower()
