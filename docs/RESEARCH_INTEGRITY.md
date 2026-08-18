# Research integrity checklist

Phase 1/2 stores data and defines interfaces. It does **not** produce alpha, edge, or trading signals. Before any later evaluation, every item below must be addressed with evidence from stored provenance—not assumptions.

## Look-ahead leakage

- [x] Open-Meteo Archive is **retrospective** and does **not** expose immutable point-in-time publication/revision timestamps. It must not populate `observation_max_so_far_c`, `observation_as_of`, or any observed covariate at decision T. Archive daily maximum may remain `diagnostic_actual_max_c` (training-target diagnostic only).
- [x] Forecast features at time `t` use only information issued at or before `t` (Phase 3 Single Runs: `available_at=run+6h` must be ≤ decision time).
- [x] Open-Meteo Historical Forecast series is **not** treated as a lagged forecast: it stitches recent run hours and will leak if used as “the forecast traders saw.” Phase 3 does not use it as point-in-time model input.
- [x] `issued_at` is null unless the API actually supplied a run initialization time (or the request used Single Runs `run=`). Null means **unavailable**, not “same as valid time.”
- [x] Market prices used as features are timestamped at or before the decision time (`market_price_observed_at` stored separately from `decision_ts`); CLOB `prices-history` points are not assumed to be executable at that instant. Point-in-time queries use a documented 7-day lookback ending at `decision_ts` and do **not** trust Gamma `startDate`/`endDate` (those lifecycle timestamps can be internally inconsistent). Price-history request URL/raw_path/content_sha256 are retained on the snapshot. Missing `p` stays null, never zero. HTTP failures are counted and limited separately from HTTP 200 empty history.
- [x] Phase 3.5 keeps historical descriptive prices in `data/phase35/historical/` contracts and forward executable books in `data/phase35/forward/`; historical `p` is never labeled ask/bid/fill. Pre-registered checkpoints are exactly 48/24/12/6/3/1 hours. An executable full-collection orchestrator exists; real provider execution is disabled unless an explicitly authorized immutable manifest passes its persisted integrity-anchor checks. Manifest creation, authorization, and collection execution are separate. Without valid persisted authorization, execution fails closed. No real collection has occurred. Historical research readiness may use `survivorship_limited_descriptive` without proving universe completeness; forward observational readiness is distinct from `EXECUTABLE_TWO_SIDED_BOOK_VALIDATED`; fixture-only never establishes live forward readiness; `PHASE35_COLLECTION_READY` is not executable/profitable.

## Timestamp alignment

- [ ] All stored timestamps are timezone-aware UTC.
- [ ] Daily maxima keep the **local date + timezone name** from the weather payload; converting a local calendar day to UTC must use that zone, not the researcher’s laptop zone.
- [ ] Open-Meteo naive local hours use `ZoneInfo` for the payload timezone. Repeated fall-DST wall-clock hours map to `fold=0` then `fold=1`; extra duplicates and spring-gap times are skipped. Original local strings stay in raw JSON only.
- [ ] Polymarket `startDate`/`endDate` strings without offsets are dropped (not coerced to UTC).
- [ ] Trade `timestamp` values from Data API are Unix seconds interpreted as UTC.

## Resolution rules

- [ ] Winning bucket is taken from market resolution / outcome settlement data, not from Open-Meteo archive max.
- [ ] Whole-degree Celsius rounding follows the **market rules text**, not a generic `.round()`.
- [ ] “Finalized” Wunderground language in descriptions is recorded, not implemented as a scraper.

## Unit conversion

- [ ] Source unit and source value are retained on every temperature row.
- [ ] Celsius normalization is formula-based (`F→C`, `K→C`); never infer unit from magnitude.

## Station mapping

- [ ] City parsed from question/slug only among {Paris, London, Munich, Amsterdam, New York, Milan}.
- [ ] Event date: complete question date is authoritative; complete slug date is used only if question month/day is absent or agrees; description may fill a unique non-conflicting year. Conflicting sources leave `event_date` null with a parse note.
- [ ] ICAO is copied from market text/URL when present. Wunderground URLs that end with punctuation (e.g. `.../EGLC.` / `.../KLGA.`) are accepted as a 4-letter path segment. **No** CDG↔LFPG↔LFPB invention. Bare English tokens such as CITY/THIS/WILL are not treated as ICAO.
- [ ] Unresolved questions remain in `markets` with `parse_status=unresolved` or `partial`.
- [ ] Configured weather stations (LFPG, EGLC, EDDM, EHAM, KJFK, LIMC) are collection points, not resolution oracles.

## Missing and duplicate data

- [ ] Upserts are idempotent on natural keys; reruns must not duplicate trades/prices.
- [ ] Empty pages stop pagination; `max-pages` is a hard bound.
- [ ] Data API `/trades` offset cap (10000) is documented; deep history needs `start`/`end` windows (official docs). Not silently padded.

## Executable prices, spread, fees, liquidity

- [x] Mid/last/`prices-history` is **not** an executable fill.
- [x] Current CLOB book snapshots (bids/asks, tick, min size) are the only official public book; there is **no** official historical book API.
- [x] Fees, maker/taker, and slippage are not modeled in Phase 1/2/3 (`pnl` stays null when asks are absent).
- [x] Thin books and tail buckets (`or below` / `or higher`) need extra liquidity checks before any economic claim.
- [x] Phase 3 backtests with missing historical asks report `non_executable`, `executable_trades=0`, and null PnL/ROI/drawdown/profit factor—never fabricated zeros.

## Survivorship bias

- [ ] Discovery must include **closed** markets, not only Gamma’s default `closed=false`.
- [ ] Delisted/removed markets that never appear in Gamma cannot be reconstructed from these APIs.
- [ ] Search + paged `/markets` still misses markets whose question text does not match the conservative temperature pattern.

## Tail buckets

- [ ] `N°C or below` / `N°C or higher` are stored as open-ended buckets (`min_c` or `max_c` null).
- [ ] Unrecognized outcome labels are stored without a fabricated numeric range.

## Trading prohibition

- [ ] No order placement, signing, wallet keys, or non-GET HTTP.
- [ ] `--dry-run` performs no network and no writes.
