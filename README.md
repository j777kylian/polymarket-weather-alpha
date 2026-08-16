# Polymarket Weather Market Alpha Research System

**RESEARCH ONLY. This repository never places orders, signs transactions, holds wallet keys, or sends POST/PUT/PATCH/DELETE HTTP requests. There is no live trading path and no profit-optimization code.**

Phase 1–2 collect **public read-only** Polymarket and Open-Meteo data into SQLite plus raw JSON, with conservative parsers and explicit provenance.

Phase 3 adds a **bounded research-only** pipeline under `src/weather_alpha/research/`: discover closed temperature-bucket markets (`limit_per_type=50` with bounded pages), attach CLOB `prices-history` descriptive probabilities (timestamp + raw provenance), Open-Meteo Single Runs ECMWF forecasts with conservative `available_at=run+6h` and per-hour forecast state, archive diagnostic maxima (not decision-time observations), build a Parquet research dataset, fit interpretable baselines, and write deterministic audit/calibration/backtest/tail reports. Historical asks are unavailable from official public APIs, so backtests are classified **descriptive/non-executable** with `executable_trades=0`, null `selected_threshold`, and null PnL—never fabricated fills. Small scored samples are reported as **insufficient/inconclusive** under a documented operational minimum (assumption, not a universal statistical law).

This project is standalone. It does not import or modify any other AIWorkspace repository.

## Reproducible setup (uv, Python 3.11)

`.python-version` pins the validated interpreter series to Python 3.11. `uv.lock`
is version-controlled and pins the validated dependency graph; do not delete or
regenerate it during routine validation.

```bash
# Install the locked runtime and all development dependencies into .venv.
uv sync --locked --all-groups

# Run the committed checks against that locked environment.
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
uv run --locked mypy src tests
uv run --locked pytest -q tests/test_phase3_*.py
uv run --locked pytest -q
uv build
```

The validated archival environment used `uv 0.12.4` and Python 3.11. `uv` may
be installed independently, but a clone must use `uv sync --locked --all-groups`
before validation so it does not silently resolve a new dependency graph.

The default suite uses fixtures and injectable GET transports; it blocks live
HTTP. No environment variables are required for the offline test/build workflow.
`.env.example` is intentionally placeholder-only: do not add API keys, tokens,
wallet credentials, or signing material.

## CLI

```bash
uv run weather-alpha init-db --db data/processed/weather_alpha.sqlite
uv run weather-alpha inspect-db --db data/processed/weather_alpha.sqlite

uv run weather-alpha collect-polymarket --dry-run --max-pages 2 \
  --start-date 2024-07-01 --end-date 2024-07-07

uv run weather-alpha collect-weather --dry-run \
  --station LFPG --start-date 2024-07-01 --end-date 2024-07-03 \
  --provider open-meteo-historical-forecast

# Phase 3 research collection (bounded; GET-only). Prefer --dry-run first.
uv run weather-alpha phase3-collect --dry-run \
  --start-date 2026-07-01 --end-date 2026-07-31 \
  --output-root data/processed/phase3 \
  --max-search-pages 5 --price-fidelity-minutes 60 --forecast-lead-hours 24

# Phase 3 evaluation from already-collected normalized JSONL/Parquet.
uv run weather-alpha phase3-run \
  --input-root data/processed/phase3 \
  --output-root data/processed/phase3
```

`--dry-run` prints intended scope and performs **no HTTP and no writes**. Phase 3 date spans are capped (62 days). No API keys are required or accepted for trading.

Weather providers: `open-meteo-historical-forecast` (default Phase 1/2),
`open-meteo-archive`, `open-meteo-ensemble`. Phase 3 point-in-time forecasts use
Open-Meteo **Single Runs** (`ecmwf_ifs`) only—not Historical Forecast.

## Phase 3 data and report reproducibility

A clone reproduces the **source code, locked Python environment, tests, and
committed deterministic Phase 3 reports**. It does **not** reproduce the exact
frozen historical raw dataset by itself: raw payloads and normalized local data
under `data/phase3/**` are intentionally ignored because they are collection
artifacts, may be large, and depend on public providers' current availability.
No raw Phase 3 dataset is committed.

The frozen Phase 3 experiment requested `2026-03-20` through `2026-04-18` from
Polymarket Gamma public search, Polymarket CLOB `GET /prices-history`, Open-Meteo
Single Runs (`ecmwf_ifs`), and Open-Meteo Archive (diagnostic only). The delivered,
version-controlled outputs are:

- `reports/phase3_dataset_audit.{json,md}`
- `reports/phase3_model_calibration.{json,md}`
- `reports/phase3_backtest.{json,md}`
- `reports/phase3_tail_alpha.{json,md}`

When authorized to make a new bounded public GET-only collection, use a local
ignored root such as `data/phase3/<range>/`. That collection can create
`phase3_snapshots.jsonl`, `phase3_snapshots.parquet`,
`phase3_quarantine.json`, `phase3_source_manifest.json`, and `raw/` beneath its
chosen input root. It must not be treated as a reproduction of the historical
frozen corpus unless its manifest and provenance are independently compared.

With an existing local normalized corpus, run the analysis offline without
collecting network data and write regenerated reports to a separate directory:

```bash
uv run --locked weather-alpha phase3-run \
  --input-root data/phase3/2026-03-20_2026-04-18_1h \
  --output-root /tmp/weather-alpha-phase3-reports
```

The command requires `phase3_snapshots.jsonl` and reads an optional
`phase3_quarantine.json` and `phase3_source_manifest.json` from `--input-root`.
It writes the four JSON/Markdown reports below `--output-root/reports/`. Do not
overwrite the committed delivered reports during validation.

## Architecture

```
src/weather_alpha/
  http/           GET-only client, retries, injectable transport
  config/         paths, date bounds, station loader
  models/         UTC timestamps, temperatures, records
  storage/        SQLite schema/repository, raw JSON paths
  collectors/     Polymarket + Open-Meteo GET collectors
  research/       Phase 3 dataset, models, backtest, tail, reports
  probability/    Phase 1/2 scaffold (still present)
  backtest/       Phase 1/2 scaffold (still present)
  reports/        Phase 1/2 scaffold (still present)
  cli.py
config/stations.yaml
data/{polymarket,weather,processed}/
docs/             API limits, source evaluation, integrity checklist
```

## API limitations (short)

Official public Polymarket APIs used: Gamma `GET /events`, `GET /markets`, `GET /public-search`; CLOB `GET /prices-history`, `GET /book`; Data API `GET /trades`.

**Official public APIs do not supply arbitrary historical order-book reconstruction.** CLOB `prices-history` points are `{t,p}` descriptive probabilities only. Phase 3 keeps `executable_entry_price` / `best_bid` / `best_ask` / `midpoint` / `spread` null unless genuinely sourced.

Open-Meteo Historical Forecast **stitches successive runs** and does not expose per-timestep issuance time—**not** used as Phase 3 point-in-time model input. Single Runs (`run=`) provide initialization time; this project stores `issued_at=run` and conservative `available_at=run+6h`. Archive maxima are **diagnostic** grid/reanalysis values, not Wunderground settlement prints. See [docs/API_LIMITATIONS.md](docs/API_LIMITATIONS.md) and [docs/SOURCE_EVALUATION.md](docs/SOURCE_EVALUATION.md).

## Data provenance

- Timestamps: timezone-aware UTC (naive datetimes rejected).
- Temperatures: stored in Celsius **and** as source value/unit (Fahrenheit New York labels convert to °C while retaining the raw label).
- Raw JSON: content-addressed SHA-256 paths under `data/` from canonical URL plus payload digest; `raw_payloads` rows are keyed by `(content_sha256, request_url)` and share that provenance with normalized records.
- Parsers keep unresolved temperature questions rather than inventing city/station/bucket metadata. Ambiguous settlement/station/date/bucket rows are quarantined with reasons.
- Configured stations: Paris CDG (LFPG), London City (EGLC), Munich (EDDM), Amsterdam Schiphol (EHAM), New York JFK (KJFK), New York LaGuardia (KLGA), Milan Malpensa (LIMC). Polymarket resolution ICAO is parsed from market text when present (Paris markets often name LFPB, which is **not** silently rewritten to LFPG; unknown stations are quarantined).

Integrity checklist: [docs/RESEARCH_INTEGRITY.md](docs/RESEARCH_INTEGRITY.md).
