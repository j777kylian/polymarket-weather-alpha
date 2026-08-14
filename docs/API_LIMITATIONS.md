# Official API coverage and limitations

This project uses **documented public GET endpoints only**. If an official API does not provide a field, the database stores null and a provenance limitation. Tests use fixtures; the default pytest suite makes **no live HTTP calls**.

## Polymarket

| Need | Official public endpoint | Notes |
| --- | --- | --- |
| Event/market discovery | `GET https://gamma-api.polymarket.com/events` | limit/offset (and a separate keyset route). Default listing filters `closed=false` unless specified. |
| Market listing | `GET https://gamma-api.polymarket.com/markets` | Same pagination family. |
| Search | `GET https://gamma-api.polymarket.com/public-search?q=` | Used for “highest temperature in {city}”. |
| Token price history | `GET https://clob.polymarket.com/prices-history?market={token_id}` | `startTs`/`endTs` or `interval`; `fidelity` in minutes. Points are `{t, p}`. |
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

**Not claimed**

- Per-timestep model run initialization on Historical Forecast or Ensemble responses.
- METAR/Wunderground station observations.
- Fabricated ensemble members when the JSON omits them.

## Raw payload layout

Deterministic content-addressed paths: `data/{source}/{sha256[:2]}/{sha256}.json` where the digest is `sha256(canonical_request_url + "\\n" + payload_digest)`. The same URL with the same payload reuses the path; a changed payload writes a new file so current-book and trades history is not overwritten. SQLite `raw_payloads` identity is `(content_sha256, request_url)` so two different URLs that return identical JSON keep separate rows/paths. Repeated same URL + same digest is idempotent. Every normalized row from a response carries that `raw_path`, `content_sha256`, canonical `request_url`, and the same `retrieved_at`. Schema v2 migrates smoke DBs that still use the legacy `content_sha256`-only primary key without dropping stored payloads.
