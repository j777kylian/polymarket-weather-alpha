# Phase 3.5 full historical collection design

**Status:** executable full-collection orchestrator implemented under `REQUEST_POLICY_VERSION = phase35-full-collection-request-policy-v2`. Request-budget preflight **passes** the classification-triggered initial-plus-reserve cap-bounded policy. `FULL_COLLECTION_START_ALLOWED = YES_PENDING_FINAL_REVIEW`. The theoretical all-transient-once envelope is **NOT AUTHORIZED**. Real provider execution is **disabled** unless an explicitly authorized immutable manifest passes its persisted integrity-anchor checks (manifest SHA-256 bound by a separately created authorization receipt). Manifest creation, authorization, and collection execution are separate operations. **No real collection has occurred.**

## Scope and retained limitations

- **Candidate window:** `2026-03-01` through `2026-05-29` inclusive (90 days).
- **Cities (canonical):** Amsterdam, London, Milan, Munich, New York, Paris (`amsterdam`, `london`, `milan`, `munich`, `new york`, `paris`).
- **Stations:** the repository station catalog (`config/stations.yaml`); source ICAO is never remapped.
- **Checkpoints:** `48, 24, 12, 6, 3, 1` hours (frozen Phase 3.5 leads).
- **Source qualification:** `SAMPLED_SOURCE_QUALIFIED`, not `EXHAUSTIVELY_SOURCE_PROVEN` and not `MODEL_READY`.
- `GAMMA_SURVIVORSHIP_LIMITATION = YES`.
- `HISTORICAL_UNIVERSE_COMPLETE = NOT_PROVEN`.
- Historical CLOB `/prices-history` values are `DESCRIPTIVE_ONLY`, never bid, ask, fill, or realized execution price.
- Historical descriptive collection and forward observed order-book collection remain separate tracks. This document does not authorize forward collection, orders, cancellations, wallets, signing, private keys, transactions, paper trading, live trading, a daemon, or Phase 4.

This design is based on the accepted Phase 3.5 freeze commit `662f11bab46fa6853a90ed18aa439c1da5fd1c88` plus the v2 request-policy contract. An executable full-collection orchestrator exists; real provider execution is disabled unless an explicitly authorized immutable manifest passes its persisted integrity-anchor checks. Frozen Phase 3 semantics and accepted Phase 3.5 readiness rules are unchanged. `PHASE35_COLLECTION_READY` remains infrastructure/source readiness and is not `PHASE35_DATASET_READY`. No real collection has occurred.

## 1. Scientific unit and identity

The unit of collection is a **canonical weather event family**, never an independently treated mutually exclusive bucket outcome. The existing Phase 3 identity hierarchy, station parsing, native-unit handling, complete-bucket topology, settlement semantics, and quarantine rules remain authoritative.

For every family, retain:

- `canonical_event_id` and canonical identity key;
- city, parsed settlement station and configured research station where applicable (never silently remap stations);
- event date, local timezone, native settlement unit and source value;
- complete mutually exclusive bucket topology, labels, outcome IDs, and settlement status/label;
- Gamma discovery evidence and raw provenance;
- all attached forecast, descriptive-price, selection, and collection-attempt provenance.

An invalid, overlapping, gapped, duplicate, or otherwise incomplete bucket topology is not silently repaired. It is recorded as `INELIGIBLE` or quarantined with a reason.

## 2. Fixed decision matrix and point-in-time rules

For every eligible event family, create one row per fixed checkpoint:

```text
48h, 24h, 12h, 6h, 3h, 1h
```

`decision_ts` is the event's local midnight minus the checkpoint lead, converted to UTC using the retained event timezone. For each checkpoint:

```text
forecast.available_at <= decision_ts
price.observed_at <= decision_ts
```

Forecast selection uses only Open-Meteo **Single Runs**, model `ecmwf_ifs`, retaining the latest candidate satisfying the conservative existing availability rule. Open-Meteo Archive and stitched Historical Forecast are prohibited substitutes. Missing data remains missing and must be classified, not replaced by a later forecast.

CLOB history is kept only as timestamped descriptive probability. It does not make a snapshot executable and cannot be combined with future order-book evidence.

## 3. Immutable, resumable manifest

Before the first request, write and hash one immutable manifest. The manifest includes at least:

```text
COLLECTION_ID
SCHEMA_VERSION
CODE_COMMIT
START_DATE
END_DATE
CITIES
STATIONS
CHECKPOINTS
FORECAST_PROVIDER
FORECAST_MODEL
FORECAST_AVAILABILITY_RULE
MARKET_PROVIDER
PRICE_PROVIDER
REQUEST_POLICY
RATE_LIMIT_POLICY
RETRY_POLICY
RAW_STORAGE_NAMESPACE
CREATED_AT
```

Required frozen values for this candidate:

```text
START_DATE=2026-03-01
END_DATE=2026-05-29
CITIES=[amsterdam, london, milan, munich, new york, paris]
CHECKPOINTS=[48,24,12,6,3,1]
FORECAST_PROVIDER=open_meteo_single_runs
FORECAST_MODEL=ecmwf_ifs
FORECAST_AVAILABILITY_RULE=available_at_lte_decision_ts
MARKET_PROVIDER=polymarket_gamma_public_search
PRICE_PROVIDER=polymarket_clob_prices_history
RAW_STORAGE_NAMESPACE=data/phase35/historical/raw/
HTTP_METHOD=GET
TIMEOUT_SECONDS=35
CONCURRENCY=1
INTER_ATTEMPT_DELAY_SECONDS=2
MAX_RETRIES=1
MAX_ATTEMPTS_PER_IDENTITY=2
REQUEST_POLICY_VERSION=phase35-full-collection-request-policy-v2
RETRY_MODE=classification_triggered
RETRY_ONLY=429,timeout,TLS,transient_transport,transient_5xx
RETRY_AFTER_CAP_SECONDS=60
```

Providers/endpoints (GET-only, existing `ReadOnlyHttpClient` boundary):

```text
Gamma public GET https://gamma-api.polymarket.com/public-search
CLOB GET https://clob.polymarket.com/prices-history
Open-Meteo Single Runs https://single-runs-api.open-meteo.com/v1/forecast models=ecmwf_ifs
```

A restart reads the manifest and durable attempt index. It must reuse only immutable successful payloads whose canonical request identity, content hash, parser version, and classification are already complete. It must never replay a successful immutable request merely because the process restarted.

**Manifest creation is fail-closed before network.** The v2 planner authorizes only the classification-triggered initial-plus-reserve **cap-bounded** envelope. If that bounded plan can exceed any hard cap, status is `REQUEST_BUDGET_REDESIGN_REQUIRED` and no authorized collection manifest is written. The theoretical all-transient-once envelope is reported and is **NOT AUTHORIZED**. Scientific scope must not be sampled, reduced, or have checkpoints altered to fit a cap.

## 4. Attempt ledger, raw provenance, and state taxonomy

Every request attempt gets a persisted append-only record keyed by `COLLECTION_ID`, provider, canonical request identity, and attempt number. Required fields:

```text
provider
endpoint
http_method=GET
canonical_request_identity
normalized_request_parameters
attempt_number
attempt_timestamp_utc
latency_ms
http_status
retry_after_seconds
content_sha256
stable_raw_provenance_path
parser_schema_version
result_classification
error_class/detail (if any)
```

Allowed final classifications are exactly:

```text
PENDING
SUCCESS
VALID_EMPTY
PROVIDER_NO_DATA
RATE_LIMITED
TIMEOUT
TLS_FAILURE
TRANSIENT_TRANSPORT_FAILURE
TRANSIENT_5XX
HTTP_FAILURE
SCHEMA_ERROR
INELIGIBLE
SKIPPED_ALREADY_COMPLETE
INTERRUPTED_RESUMABLE
```

`RATE_LIMITED`, `TIMEOUT`, `TLS_FAILURE`, `TRANSIENT_TRANSPORT_FAILURE`, and `TRANSIENT_5XX` are operational outcomes, never provider-no-data evidence. `HTTP_FAILURE` is reserved for permanent deterministic HTTP failures (including non-retryable 4xx). `VALID_EMPTY` is distinct from `PROVIDER_NO_DATA`; neither may be converted to a zero probability or synthetic forecast. Runtime retry-reserve or attempt-cap exhaustion persists ledger/missingness evidence and raises a typed state with `COLLECTION_STATUS=INTERRUPTED_RESUMABLE`. No sampling, scope reduction, silent continuation, or missingness reinterpretation is allowed.

Runtime raw locations may be absolute internally. Canonical artifacts serialize only stable POSIX paths, for example:

```text
historical/raw/<provider>/<yyyy-mm-dd>/<content-sha256>.json
```

along with `content_sha256`, normalized request identity, provider, and parser/schema version. Canonical reports must never serialize `/tmp/`, `/Users/`, `/home/`, clone roots, CI roots, or other machine-specific paths.

Resumability requires identity present, raw exists, hash matches, parser compatible, and complete state. Duplicate success replay is rejected. Storage-cap and global-cap stop behavior is fail-closed.

## 5. Binding request, rate-limit, duration, and storage budget

The prior stopped exhaustive estimate for this exact candidate is retained as the planning baseline (not a collection authorization):

| Component | Best-case logical GETs | Planning baseline |
|---|---:|---:|
| Gamma city-date discovery | 540 | 540 |
| ECMWF Single Runs immutable keys | computed, fail-closed `max(computed, 4829)` | 4,829 |
| CLOB descriptive histories | 540 | 540 |

Binding hard caps:

```text
Gamma attempts <= 600
ECMWF logical identities <= 5000
ECMWF attempts <= 10000
CLOB attempts <= 600
Global GET attempts <= 11200
Storage preflight free >= 2 GiB
Storage hard cap used >= 3 GiB fail-closed
```

Maximum attempts per identity = `MAX_RETRIES + 1` = 2, **classification-triggered**, not `identity_count * 2` pre-reservation. Every identity has one initial GET. Exactly one additional attempt is allowed only after `RATE_LIMITED`/`429`, `TIMEOUT`, `TLS_FAILURE`, typed transient transport failure, or typed transient 5xx. Never retry `SUCCESS`, `VALID_EMPTY`, `PROVIDER_NO_DATA`, `SCHEMA_ERROR`, `INELIGIBLE`, permanent deterministic 4xx, or persisted immutable success.

Planner contract (exact):

```text
initial:                 gamma 540 / ecmwf 4829 / clob 540 / total 5909
retry reserves:          gamma 60 / ecmwf 5171 / clob 60
cap-bounded maxima:      gamma 600 / ecmwf 9658 / clob 600 / global 10858
theoretical all-once:    gamma 1080 / ecmwf 9658 / clob 1080 / global 11818
```

Preflight **PASS**es the initial-plus-reserve cap-bounded policy (`FULL_COLLECTION_START_ALLOWED=YES_PENDING_FINAL_REVIEW`). The theoretical envelope is **NOT AUTHORIZED**. Runtime exhaustion of a retry reserve or attempt cap fails closed with `COLLECTION_STATUS=INTERRUPTED_RESUMABLE` and persisted ledger/missingness evidence.

Duration floors at 2s ordinary pacing remain review context only (not an execution grant):

```text
best case at 2s: >= 197 minutes for 5,909 identities
conservative operating envelope: >= 9h to >= 13h plus provider Retry-After/backoff
```

Local accepted-v3 p95 raw sizes remain Gamma 2,399,899 bytes, ECMWF 4,314 bytes, and CLOB 1,217 bytes (~1.23 GiB raw; ~1.41 GiB with 15% index/report reserve). Preflight still requires **>= 2 GiB** free; hard stop at **3 GiB** used.

Hard stop conditions: global or provider cap reached, local storage reserve/hard cap breached, manifest/hash mismatch, raw-provenance hash failure, unexpected HTTP method, schema incompatibility, or operator stop. No failure category permits automatic use of a later or retrospective substitute.

## 6. Required completeness and missingness matrices

Produce machine-readable matrices with `expected_count`, `observed_count`, `usable_count`, `missing_count`, `missing_fraction`, and reason-stratified `missing_reasons` for each of:

```text
DATE
CITY
STATION
CHECKPOINT
ECMWF_RUN_CYCLE
EVENT_FAMILY
PRICE_HISTORY
MONTH
```

Every cell includes classifications from the attempt ledger. Data must not be dropped solely because it is missing. Denominators are the expected grid. Missing checkpoints remain in the denominator.

Required systematic-failure views include consecutive-week city/provider clusters, endpoint/status/exception clusters, station clusters, checkpoint clusters, event-family topology failures, and month-level imbalance. A rate limit, timeout, or TLS failure must appear separately from valid empty/no-data response classes.

Binding systematic-cluster threshold: **7 consecutive calendar days** of operational failure for the same city/station view; **zero** unresolved clusters allowed (`MAX_UNRESOLVED_SYSTEMATIC_CLUSTERS = 0`).

## 7. Binding `PHASE35_DATASET_READY` contract

`PHASE35_COLLECTION_READY` remains infrastructure/source readiness. It is not evidence that a completed dataset is fit for Phase 4. Define separately:

```text
PHASE35_DATASET_READY =
    provenance_complete
    AND point_in_time_integrity
    AND topology_integrity
    AND settlement_availability_acceptable
    AND descriptive_price_coverage_acceptable
    AND coverage_policy_accepted
    AND no_unresolved_systematic_failure_cluster
```

Non-negotiable invariants:

```text
future_leakage_count == 0
retrospective_substitution_count == 0
raw_provenance_hash_failures == 0
unreviewed invalid_event_group_count == 0
```

The last invariant may be relaxed only through an explicit, pre-reviewed quarantine policy that preserves every excluded family and reason; it cannot silently remove records.

Binding coverage thresholds:

```text
overall coverage >= 0.95
each city >= 0.90
each lead/checkpoint >= 0.90
each month >= 0.85
settlement overall >= 0.98
settlement among scored families == 1.00
CLOB descriptive overall >= 0.85
CLOB descriptive each city >= 0.75
```

The gate must evaluate at least: overall event-family coverage; each city; each parsed settlement station and configured research station; each month; every checkpoint; systematic provider failure clusters; valid complete bucket topology; settlement availability; descriptive CLOB availability; future-leakage count; retrospective-substitution count; provenance completeness.

`PHASE35_DATASET_READY` is determined without collecting. An empty uncollected grid does not pass.

## 8. Dataset freeze artifact

Only if `PHASE35_DATASET_READY` passes after independent review, write a deterministic dataset freeze artifact containing:

```text
DATASET_ID
COLLECTION_ID
CODE_COMMIT
MANIFEST_SHA256
RAW_INDEX_SHA256
CANONICAL_DATASET_SHA256
REPORT_SHA256
DATE_RANGE
EVENT_COUNT
SNAPSHOT_COUNT
CHECKPOINT_COUNTS
CITY_COUNTS
STATION_COUNTS
MONTH_COUNTS
MISSINGNESS_SUMMARY
QUARANTINE_SUMMARY
KNOWN_LIMITATIONS
```

Production freeze is the offline command `phase35-freeze-dataset`. It consumes persisted collection artifacts under `<collection_root>/<collection_id>/` plus the authoritative machine audit JSON `reports/phase35_historical_audit.json`. `AUDIT_REPORT_SHA256` / `REPORT_SHA256` hashes that canonical JSON (not Markdown). `phase35-dataset-acceptance` remains a synthetic test helper and is not a production freeze. Freeze artifacts are not written while the audit fails. Phase 4, if separately authorized, must consume the frozen `DATASET_ID` and hashes. It must not silently recollect, mutate, append to, or replace the training dataset.

## 9. Forward track separation

Historical rows remain:

```text
track=HISTORICAL_DESCRIPTIVE
price_semantics=DESCRIPTIVE_ONLY
```

Forward rows remain:

```text
track=OBSERVED_ORDER_BOOK
```

No type conversion is allowed from historical descriptive CLOB probability to an executable price. Forward activity remains GET-only/read-only and is outside this collection design.

## 10. Commands — plan, authorize, and collect are separate; no real collection has occurred

```bash
# 1. Create/validate the immutable contract only; no collection request and no authorization receipt.
uv run weather-alpha phase35-full-collection-plan \
  --start-date 2026-03-01 \
  --end-date 2026-05-29 \
  --manifest data/phase35/historical/manifests/<COLLECTION_ID>.json

# 2. Explicit offline authorization. Writes a persisted receipt bound to the
#    manifest digest. Does not mutate the manifest and does not contact providers.
uv run weather-alpha phase35-authorize-historical \
  --manifest data/phase35/historical/manifests/<COLLECTION_ID>.json \
  --authorization data/phase35/historical/manifests/<COLLECTION_ID>.authorization.json

# 3. Collection execution requires both artifacts. Real provider GETs occur only
#    after the receipt's COLLECTION_ID, MANIFEST_SHA256, CODE_COMMIT, and
#    REQUEST_POLICY_VERSION match the recomputed immutable manifest and the
#    current frozen runtime contract. Absent/mismatched artifacts fail closed
#    with PROVIDER_REQUESTS=0.
uv run weather-alpha phase35-collect-historical \
  --manifest data/phase35/historical/manifests/<COLLECTION_ID>.json \
  --authorization data/phase35/historical/manifests/<COLLECTION_ID>.authorization.json \
  --output-root data/phase35/historical

# Offline dataset audit of a persisted collection; no provider contact.
uv run weather-alpha phase35-audit-historical \
  --collection-id <COLLECTION_ID> \
  --collection-root data/phase35/historical

# Production offline dataset freeze from persisted COMPLETE artifacts.
# Refuses unless PHASE35_DATASET_READY and raw/audit integrity still hold.
uv run weather-alpha phase35-freeze-dataset \
  --collection-id <COLLECTION_ID> \
  --collection-root data/phase35/historical

# Synthetic offline dataset audit for tests only. Not a production freeze:
# it does not load a collection namespace or hash real ledger/corpus artifacts.
uv run weather-alpha phase35-dataset-acceptance \
  --manifest data/phase35/historical/manifests/<COLLECTION_ID>.json \
  --output-root data/phase35/historical
```

The plan and authorize commands make no provider calls. Plan reports the v2 cap-bounded budget as passing pending final review and reports the theoretical envelope as `NOT_AUTHORIZED`. Budget preflight is not an execution grant. Collection execution exists in the orchestrator and is fail-closed without a valid persisted authorization receipt bound to the unchanged manifest digest. Dataset freeze is a separate offline operation over persisted artifacts and makes no provider calls. **No real collection has occurred.**

## Final design status

```text
POLICY_STATUS = BINDING
REQUEST_POLICY_VERSION = phase35-full-collection-request-policy-v2
FULL_COLLECTION_STARTED = NO
EXECUTABLE_ORCHESTRATOR = YES
REAL_PROVIDER_EXECUTION = DISABLED_UNLESS_PERSISTED_AUTHORIZATION_VERIFIES
FULL_COLLECTION_START_ALLOWED = YES_PENDING_FINAL_REVIEW
FORWARD_CONTINUOUS_COLLECTION_STARTED = NO
DAEMON_STARTED = NO
PHASE4_STARTED = NO
PAPER_TRADING_STARTED = NO
LIVE_TRADING_STARTED = NO
PREFLIGHT_STATUS = PREFLIGHT_OK
THEORETICAL_ENVELOPE = NOT_AUTHORIZED
NEXT_RECOMMENDED_ACTION = EXPLICIT_AUTHORIZATION_RECEIPT_THEN_COLLECTION_EXECUTION
```
