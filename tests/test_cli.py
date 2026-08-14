from pathlib import Path

import pytest
from click.testing import CliRunner

from weather_alpha.cli import main


def test_dry_run_collect_polymarket_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "collect-polymarket",
            "--dry-run",
            "--max-pages",
            "2",
            "--start-date",
            "2024-07-01",
            "--end-date",
            "2024-07-07",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()
    assert "paris" in result.output.lower()
    assert not list(tmp_path.glob("*.sqlite"))
    assert not (tmp_path / "data" / "polymarket").exists() or not any(
        (tmp_path / "data" / "polymarket").rglob("*.json")
    )


def test_dry_run_collect_weather_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    stations = Path(__file__).resolve().parents[1] / "config" / "stations.yaml"
    result = runner.invoke(
        main,
        [
            "collect-weather",
            "--dry-run",
            "--station",
            "LFPG",
            "--start-date",
            "2024-07-01",
            "--end-date",
            "2024-07-03",
            "--provider",
            "open-meteo-historical-forecast",
            "--stations-file",
            str(stations),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()
    assert "LFPG" in result.output


def test_date_range_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["collect-weather", "--start-date", "2024-07-10", "--end-date", "2024-07-01"],
    )
    assert result.exit_code != 0


def test_init_db_and_inspect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    db = tmp_path / "research.sqlite"
    init = runner.invoke(main, ["init-db", "--db", str(db)])
    assert init.exit_code == 0, init.output
    inspect = runner.invoke(main, ["inspect-db", "--db", str(db)])
    assert inspect.exit_code == 0, inspect.output
    assert "markets" in inspect.output


def test_cli_rejects_unbounded_page_size_and_unknown_city(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    too_big = runner.invoke(main, ["collect-polymarket", "--dry-run", "--page-size", "10000"])
    assert too_big.exit_code != 0
    zero = runner.invoke(main, ["collect-polymarket", "--dry-run", "--page-size", "0"])
    assert zero.exit_code != 0
    city = runner.invoke(main, ["collect-polymarket", "--dry-run", "--city", "atlantis"])
    assert city.exit_code != 0
    detail = runner.invoke(main, ["collect-polymarket", "--dry-run", "--max-detail-markets", "0"])
    assert detail.exit_code != 0
