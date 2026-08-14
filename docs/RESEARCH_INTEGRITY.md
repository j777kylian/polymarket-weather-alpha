# Research integrity checklist

Phase 1/2 stores data and defines interfaces. It does **not** produce alpha, edge, or trading signals. Before any later evaluation, every item below must be addressed with evidence from stored provenance—not assumptions.

## Look-ahead leakage

- [ ] Forecast features at time `t` use only information issued at or before `t`.
- [ ] Open-Meteo Historical Forecast series is **not** treated as a lagged forecast: it stitches recent run hours and will leak if used as “the forecast traders saw.”
- [ ] `issued_at` is null unless the API actually supplied a run initialization time (or the request used Single Runs `run=`). Null means **unavailable**, not “same as valid time.”
- [ ] Market prices used as features are timestamped at or before the decision time; CLOB `prices-history` points are not assumed to be executable at that instant.

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
- [ ] ICAO is copied from market text/URL when present. **No** CDG↔LFPG↔LFPB invention.
- [ ] Unresolved questions remain in `markets` with `parse_status=unresolved` or `partial`.
- [ ] Configured weather stations (LFPG, EGLC, EDDM, EHAM, KJFK, LIMC) are collection points, not resolution oracles.

## Missing and duplicate data

- [ ] Upserts are idempotent on natural keys; reruns must not duplicate trades/prices.
- [ ] Empty pages stop pagination; `max-pages` is a hard bound.
- [ ] Data API `/trades` offset cap (10000) is documented; deep history needs `start`/`end` windows (official docs). Not silently padded.

## Executable prices, spread, fees, liquidity

- [ ] Mid/last/`prices-history` is **not** an executable fill.
- [ ] Current CLOB book snapshots (bids/asks, tick, min size) are the only official public book; there is **no** official historical book API.
- [ ] Fees, maker/taker, and slippage are not modeled in Phase 1/2 (`pnl` stays null).
- [ ] Thin books and tail buckets (`or below` / `or higher`) need extra liquidity checks before any economic claim.

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
