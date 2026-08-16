# Official API coverage and limitations

This project uses **documented public GET endpoints only**. If an official API does not provide a field, the database stores null and a provenance limitation. Tests use fixtures; the default pytest suite makes **no live HTTP calls**.

## Polymarket

| Need | Official public endpoint | Notes |
| --- | --- | --- |
| Event/market discovery | `GET https://gamma-api.polymarket.com/events` | limit/offset (and a separate keyset route). Default listing filters `closed=false` unless specified. |
| Market listing | `GET https://gamma-api.polymarket.com/markets` | Same pagination family. |
| Search | `GET https://gamma-api.polymarket.com/public-search?q=` | Used for “highest temperature in {city}”. Phase 3 requests `limit_per_type=50` (bounded page size) with bounded `max-search-pages` so older closed markets remain reachable. Keep `keep_closed_markets=1`. |
| Token price history | `GET https://clob.polymarket.com/prices-history?market={token_id}` | `startTs`/`endTs` or `interval`; `fidelity` in minutes. Points are `{t, p}`. **`p` is descriptive market probability only**—not an executable ask/bid. Phase 3 point-in-time queries **do not** use Gamma `startDate`/`endDate` as the window (those lifecycle timestamps can be internally inconsistent, including `startDate` after `endDate`). The CLOB lookback is **7 calendar days ending at `decision_ts`**: `startTs=decision_ts-7d`, `endTs=decision_ts`. `select_price_at_or_before` remains defense in depth. HTTP 4xx leaves `market_probability` null with an explicit limitation and is counted separately from HTTP 200 empty history. |
| Current order book | `GET https://clob.polymarket.com/book?token_id=` | **Current** book only. |
| Public trades | `GET https://data-api.polymarket.com/trades` | Filter `market` (condition id), `limit`/`offset`, `start`/`end`. Offset max 10000 per window. |

**Not claimed / not implemented**

- Historical order-book reconstruction. Batch `POST /books` exists on CLOB but this client is GET-only and still only returns **current** books.
- Authenticated CLOB user trades (`GET /data/trades` on clob) — requires API keys; out of scope.
- Order placement (`POST /order`, cancels). Intentionally absent.
- WebSocket market streams (could record live books going forward; not in Phase 1/2).

**Rate limits** (Cloudflare, [docs.polymarket.com/api-reference/rate-limits](https://docs.polymarket.com/api-reference/rate-limits)): Gamma `/markets` 300/10s, `/events` 500/10s, `/public-search` 350/10s; Data `/trades` 200/10s; CLOB `/book` 1500/10s, `/prices-history` 1000/10s. Collectors use small page sizes, bounded `max-pages`, and retry/backoff on 429/5xx.

## Open-Meteo

| Need | Official endpoint | Notes |
| --- | --- | --- |
| Historical forecast (stitched) | `GET https://historical-forecast-api.open-meteo.com/v1/forecast` | Same parameters as Forecast API; `start_date`/`end_date`; hourly + daily variables. |
| Archive observations / reanalysis | `GET https://archive-api.open-meteo.com/v1/archive` | Hourly `temperature_2m`, `relative_humidity_2m`, `dew_point_2m`, `wind_speed_10m`, `cloud_cover`, `surface_pressure`; daily `temperature_2m_max`. `timezone` required for daily aggregations. Only temperature/dew-point series are converted to Celsius; other variables keep `source_value`/`source_unit` with `temperature_celsius` null. Naive local hours are converted to UTC with `ZoneInfo`; fall DST overlap hours in an ordered series use `fold=0` then `fold=1` (00:30Z then 01:30Z for Europe/Paris `2024-10-27T02:30`). Extra overlap duplicates and spring-gap local times are skipped, not fabricated. Original local timestamps remain in the immutable raw JSON. |
| Ensemble members | `GET https://ensemble-api.open-meteo.com/v1/ensemble` | Members parsed only if present as `*_memberNN`. |
| Single Runs (Phase 3) | `GET https://single-runs-api.open-meteo.com/v1/forecast` | Requires `models=ecmwf_ifs` (plural; singular `model=` is ignored and the default `ncep_gfs025` is used) + `run=YYYY-MM-DDTHH:MM` (UTC). Do **not** send `start_date`/`end_date` (HTTP 400: “Parameter start_date must not be set”); the endpoint returns the whole run horizon, and local-day filtering is done after parse. Docs: [Single Runs API](https://open-meteo.com/en/docs/single-runs-api). Phase 3 requests ECMWF IFS hours **00/06/12/18 UTC** (sampled GETs on 2026-02-09/10 returned 200) and stores `issued_at` as the requested run initialization; `available_at` is **run + 6 hours** conservatively (public availability is not claimed). No snapshot may use a run before `available_at`. |
| Previous Runs (reference) | See [Previous Runs API](https://open-meteo.com/en/docs/previous-runs-api) | Documented for lead-time/offset workflows. Not the Phase 3 default collector path. |

**Not claimed**

- Per-timestep model run initialization on Historical Forecast or Ensemble responses.
- Using Open-Meteo Historical Forecast as point-in-time model input (stitched; no issuance timestamp).
- METAR/Wunderground station observations.
- Treating Open-Meteo Archive daily maxima as Polymarket settlement observations (diagnostic only).
- Using Open-Meteo Archive hourly rows as decision-time `observation_max_so_far_c` / `observation_as_of` features. Archive does not expose immutable point-in-time publication/revision timestamps; those fields stay null. Point-in-time METAR is not fabricated.
- Fabricated ensemble members when the JSON omits them.
- Historical order-book / ask reconstruction from `prices-history` `p` values.

## Raw payload layout

Deterministic content-addressed paths: `data/{source}/{sha256[:2]}/{sha256}.json` where the digest is `sha256(canonical_request_url + "\\n" + payload_digest)`. The same URL with the same payload reuses the path; a changed payload writes a new file so current-book and trades history is not overwritten. SQLite `raw_payloads` identity is `(content_sha256, request_url)` so two different URLs that return identical JSON keep separate rows/paths. Repeated same URL + same digest is idempotent. Every normalized row from a response carries that `raw_path`, `content_sha256`, canonical `request_url`, and the same `retrieved_at`. Schema v2 migrates smoke DBs that still use the legacy `content_sha256`-only primary key without dropping stored payloads.
