# Polymarket Weather Market Alpha Research System

**RESEARCH ONLY. This repository never places orders, signs transactions, holds wallet keys, or sends POST/PUT/PATCH/DELETE HTTP requests. There is no live trading path and no profit-optimization code.**

Phase 1–2 collect **public read-only** Polymarket and Open-Meteo data into SQLite plus raw JSON, with conservative parsers and explicit provenance. Probability, calibration, backtest, and report modules are typed scaffolds that return `NotImplementedError` or `insufficient_data`. They do not invent alpha.

This project is standalone. It does not import or modify any other AIWorkspace repository.

## Setup (uv, Python 3.11+)

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install hatchling editables
uv pip install -e ".[dev]" --no-build-isolation
```

`--no-build-isolation` is required while the tree is not fully git-tracked (uv’s isolated build copies git-listed files only). After files are committed, `uv pip install -e ".[dev]"` is sufficient.

## Tests, lint, typecheck

The default suite uses fixtures and injectable GET transports. Live network is blocked.

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src tests
```

## CLI

```bash
uv run weather-alpha init-db --db data/processed/weather_alpha.sqlite
uv run weather-alpha inspect-db --db data/processed/weather_alpha.sqlite

uv run weather-alpha collect-polymarket --dry-run --max-pages 2 \
  --start-date 2024-07-01 --end-date 2024-07-07

uv run weather-alpha collect-weather --dry-run \
  --station LFPG --start-date 2024-07-01 --end-date 2024-07-03 \
  --provider open-meteo-historical-forecast
```

`--dry-run` prints intended scope and performs **no HTTP and no writes**. Date ranges are validated; default collection bounds are `max-pages=5` and a 31-day weather/market date span. No API keys are required or accepted for trading.

Weather providers: `open-meteo-historical-forecast` (default), `open-meteo-archive`, `open-meteo-ensemble`.

## Architecture

```
src/weather_alpha/
  http/           GET-only client, retries, injectable transport
  config/         paths, date bounds, station loader
  models/         UTC timestamps, temperatures, records
  storage/        SQLite schema/repository, raw JSON paths
  collectors/     Polymarket + Open-Meteo GET collectors
  probability/    Phase 1/2 scaffold
  backtest/       Phase 1/2 scaffold
  reports/        Phase 1/2 scaffold
  cli.py
config/stations.yaml
data/{polymarket,weather,processed}/
docs/             API limits, source evaluation, integrity checklist
```

## API limitations (short)

Official public Polymarket APIs used: Gamma `GET /events`, `GET /markets`, `GET /public-search`; CLOB `GET /prices-history`, `GET /book`; Data API `GET /trades`.

**Official public APIs do not supply arbitrary historical order-book reconstruction.** This collector stores the **current** CLOB book when run. Book history begins at first snapshot.

Open-Meteo Historical Forecast **stitches successive runs** and does not expose per-timestep issuance time. Ensemble member run times are not fabricated. See [docs/API_LIMITATIONS.md](docs/API_LIMITATIONS.md) and [docs/SOURCE_EVALUATION.md](docs/SOURCE_EVALUATION.md).

## Data provenance

- Timestamps: timezone-aware UTC (naive datetimes rejected).
- Temperatures: stored in Celsius **and** as source value/unit.
- Raw JSON: content-addressed SHA-256 paths under `data/` from canonical URL plus payload digest; `raw_payloads` rows are keyed by `(content_sha256, request_url)` and share that provenance with normalized records.
- Parsers keep unresolved temperature questions rather than inventing city/station/bucket metadata.
- Configured stations: Paris CDG (LFPG), London City (EGLC), Munich (EDDM), Amsterdam Schiphol (EHAM), New York JFK (KJFK), Milan Malpensa (LIMC). Polymarket resolution ICAO is parsed from market text when present (Paris markets often name LFPB, which is **not** silently rewritten to LFPG).

Integrity checklist: [docs/RESEARCH_INTEGRITY.md](docs/RESEARCH_INTEGRITY.md).
