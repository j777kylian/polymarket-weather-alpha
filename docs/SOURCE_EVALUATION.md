# Source evaluation (weather data)

This document compares public weather sources for **research** on Polymarket daily-high temperature markets. It does not recommend paying for data or claiming that any source matches Polymarket resolution.

Polymarket weather markets typically resolve from **Wunderground station daily highs** (whole °C) at a named airport station. That resolution source is **not** an official HTTP API used by this collector. Do not treat Open-Meteo (or any model/reanalysis) as a drop-in substitute for the resolution print.

## Staged approach

1. **Phase 1 (this repo):** collect Polymarket public market/trade/price data and Open-Meteo **coordinate** forecasts/observations for configured ICAO points. Store raw JSON and provenance. Do not claim calibration skill.
2. **Phase 2:** align forecast valid times vs market close times; document station mismatches (example: Paris markets often name **LFPB / Le Bourget**, while this catalog includes **LFPG / CDG** because it was requested). No invented mapping.
3. **Phase 3:** Single Runs ECMWF point-in-time forecasts (`available_at=run+6h`), descriptive `prices-history` probabilities with `market_price_observed_at` plus price-raw provenance, interpretable baselines, and non-executable backtests. Archive maxima remain **diagnostic**, not settlement, and are **not** decision-time observation features.
4. **Later (not implemented):** if a licensed Wunderground/station-observation feed becomes available, store it as a separate provider. Only then can resolution-matching actuals be studied. Until then, Open-Meteo archive maxima are **reanalysis/model gridpoint values**, not METAR/Wunderground station maxima.

## Providers

| Source | What it is | Access | History | Licensing / limits | Use in this project |
| --- | --- | --- | --- | --- | --- |
| **Open-Meteo Historical Forecast API** | Stitched high-resolution NWP (first hours of each run) via `historical-forecast-api.open-meteo.com/v1/forecast` | Public GET, no key for non-commercial use | Typically ~2021/2022 onward depending on model | Open-Meteo license; commercial use has separate terms | **Implemented.** Daily max + hourly 2 m temperature. **Issuance/run time is not in the payload**; stored as null. |
| **Open-Meteo Archive** | ERA5 / ERA5-Land / related reanalysis via `archive-api.open-meteo.com/v1/archive` | Public GET, no key for non-commercial use | Decades (ERA5 from 1940) | Same as Open-Meteo | **Implemented** as hourly `temperature_2m`, `relative_humidity_2m`, `dew_point_2m`, `wind_speed_10m`, `cloud_cover`, `surface_pressure` plus daily `temperature_2m_max` at airport coordinates. Non-temperature variables keep source units; `temperature_celsius` is null. Not station METAR. **Phase 3 does not use Archive hourly rows as decision-time observations** (`observation_max_so_far_c` stays null). Daily max is diagnostic / train-only forecast-error target only. |
| **Open-Meteo Ensemble API** | Perturbed members via `ensemble-api.open-meteo.com/v1/ensemble` | Public GET | Short native history; `past_days` limited | Same as Open-Meteo | **Implemented as an adapter.** Members stored **only** when `*_memberNN` series are present. Member issuance time is **not** provided (see Open-Meteo issue discussion on run columns). |
| **Open-Meteo Previous Runs / Single Runs** | Lead-time offsets or full run cubes (`run=` initialization time) | Public GET; coverage varies (Single Runs ECMWF IFS HRES from 2024-03; broader models later) | Incomplete vs live forecast | Same as Open-Meteo | **Phase 3 implements Single Runs** (`https://single-runs-api.open-meteo.com/v1/forecast`, `models=ecmwf_ifs`, no `start_date`/`end_date`) with `issued_at=run` and conservative `available_at=run+6h` for hours 00/06/12/18 UTC. Historical Forecast stitching is not a substitute. Docs: [Single Runs](https://open-meteo.com/en/docs/single-runs-api), [Previous Runs](https://open-meteo.com/en/docs/previous-runs-api). |
| **ECMWF** | IFS HRES / ENS official products | Typically registered, often licensed (TIGGE, CDS, commercial) | Long, but not a free anonymous REST dump of ENS members | Restrictive redistribution | **Not collected.** Open-Meteo may redistribute subsets; this repo talks to Open-Meteo only. |
| **NOAA GFS / GEFS** | Global deterministic + ensemble | Public (NOMADS, AWS, Open-Meteo GFS models) | GFS Open-Meteo archive from ~2021-03 | US public domain for raw NOAA grids; Open-Meteo wrapper still under Open-Meteo terms | Available indirectly if `models=gfs_*` is passed later. Default historical-forecast call uses Open-Meteo default/best-match, not a claimed GFS-only series. |
| **Meteostat** | Station observations (hourly/daily) aggregated from multiple networks | Public API with key limits; bulk dumps exist | Station-dependent; gaps common | Meteostat license; attribution | **Not implemented.** Attractive for station actuals, but coverage/finalization vs Wunderground is unverified here. |
| **NOAA climate / NCEI** | GHCN, ISD/ISH, LCD | Public | Long climate series | US public domain | **Not implemented.** Useful for climate normals, not for same-day Wunderground resolution copies. |
| **Wunderground history pages** | Polymarket’s named resolution source for several temperature events | HTML, not a documented public JSON API in this project | Station-dependent; revisions until “finalized” | ToS / scraping restrictions | **Not collected.** Parser retains ICAO/station names from market text instead of scraping. |

## Honest limitations (do not ignore)

- Open-Meteo **Historical Forecast** is a **stitched nowcast-like series**, not the forecast that was live at a given lead time. Skill vs market prices requires Previous Runs or Single Runs (or original ECMWF/NOAA issuance archives).
- `generationtime_ms` is **server processing time**, never model initialization time.
- Gridpoint temperature ≠ airport instrument shelter. Elevation and siting differ.
- Ensemble members must not be synthesized. If the JSON has no `temperature_2m_member01` (etc.), member tables stay empty.
- Airport coordinates in `config/stations.yaml` are ARP-style points for Open-Meteo queries. They are **not** proof of Polymarket resolution location.
- Gamma `startDate`/`endDate` are market lifecycle timestamps, not a trusted point-in-time price window. Sampled historical weather records can be internally inconsistent (for example `startDate` after `endDate`). Phase 3 CLOB `prices-history` queries use a documented **7-day lookback ending at `decision_ts`** and never those Gamma fields.
- Gamma `public-search` is a current search index, not a guaranteed archival universe. Delisted, unindexed, or metadata-mutated markets may be absent, so Phase 3 can suffer survivorship bias and does **not** claim complete historical market coverage.
