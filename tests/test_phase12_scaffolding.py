from weather_alpha.backtest.interfaces import BacktestEngine, BacktestRequest
from weather_alpha.probability.interfaces import CalibrationReport, ProbabilityModel
from weather_alpha.reports.interfaces import ReportBuilder, ResearchReport


def test_probability_model_is_explicit_scaffold() -> None:
    model = ProbabilityModel()
    try:
        model.predict_bucket_probabilities(market_id="x")
    except NotImplementedError as exc:
        assert "phase" in str(exc).lower() or "scaffold" in str(exc).lower()
    else:
        raise AssertionError("expected NotImplementedError")


def test_calibration_and_backtest_return_insufficient_data() -> None:
    report = CalibrationReport.insufficient("no resolved markets loaded")
    assert report.status == "insufficient_data"
    assert report.scores == ()
    engine = BacktestEngine()
    result = engine.run(BacktestRequest(as_of=None, market_ids=()))
    assert result.status == "insufficient_data"
    assert result.pnl is None


def test_report_builder_does_not_invent_alpha() -> None:
    builder = ReportBuilder()
    report = builder.build()
    assert isinstance(report, ResearchReport)
    assert report.status == "insufficient_data"
    assert report.alpha_claim is None
