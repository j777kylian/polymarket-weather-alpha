# PHASE35B_DATASET_PROTOCOL_V2

## Scope

This document defines the additive V2 semantics for Phase 3.5b dataset construction and audit.
V1 artifacts remain immutable evidence and are not rewritten.

## Why V1 Remains NO

- `PHASE35_V1_DATASET_READY=NO` remains preserved.
- V1 future leakage counts include taxonomy overcount from `selected_price=None` with no pre-decision market observation.
- V1 does not encode market-relative T0 censoring and price-age semantics as first-class fields.

## PIT Taxonomy Correction

V2 separates:

- `ACTUAL_FUTURE_LEAKAGE` only when a selected value exists and `observed_at > decision_ts`.
- `NO_PRE_DECISION_PRICE` when selected price is `None` and history exists but all points are post-decision.
- `PRICE_HISTORY_EMPTY` for empty history in-window.
- `PROVIDER_FAILURE` and `SCHEMA_FAILURE` as explicit non-semantic failures.

## Event-Relative Axis (Fixed)

V2 retains all six fixed checkpoints:

- `EVENT_MINUS_48H`
- `EVENT_MINUS_24H`
- `EVENT_MINUS_12H`
- `EVENT_MINUS_6H`
- `EVENT_MINUS_3H`
- `EVENT_MINUS_1H`

Each checkpoint reports forecast availability, market observability, pipeline status, and analysis eligibility separately.

## Market-Relative Axis

V2 encodes neutral observation-time fields:

- `FIRST_OBSERVED_PRICE_WITHIN_COLLECTION_WINDOW`
- `first_observed_price_ts`
- `request_start_ts`
- `seconds_from_window_start`
- `event_time_minus_first_observed_price_seconds`

T0 censoring rule:

- `T0_LEFT_CENSORED = first_observed_price_ts - request_start_ts <= 3600 seconds`
- `T0_UNCENSORED = not T0_LEFT_CENSORED`

Left-censored rows are retained and labeled; they are excluded only from primary true-T0 eligibility.

## Market Age Semantics

Targets are:

- `T0`
- `T0+1h`
- `T0+3h`
- `T0+6h`
- `T0+12h`

For each target:

- `analysis_target_ts`
- `selected_market_price`
- `selected_market_price_observed_at`
- `PIT_VALID`
- `PRICE_AGE_AT_TARGET_SECONDS`

Price age is distinct from PIT validity; stale-but-past prices are not future leakage.

## Three Analysis Tracks (Eligibility Metadata Only)

- `TRACK_A_FORECAST_CALIBRATION`: requires PIT forecast + settlement.
- `TRACK_B_FIXED_TIME_MARKET_ALPHA`: Track A + valid pre-decision market price.
- `TRACK_C_EARLY_MARKET_ALPHA`: market-relative; primary cohort requires uncensored T0 plus PIT-valid forecast/market and boundary validity. Left-censored cohort is retained separately.

No alpha or PnL computation is included in this implementation pass.

## Shared-Token Mapping Invariant

For each selected family:

- selected CLOB token must belong to that family's canonical `yes_token_ids`.
- shared tokens across distinct non-alias families are rejected/quarantined in planning.

## Correction-Recovery Requirement

V2 adds deterministic offline derivation of unresolved correction identities:

- derive from canonical family ownership plus persisted planning/parsing artifacts
- include only missing correct family-owned CLOB histories
- `Gamma=0`, `ECMWF=0`, CLOB-only correction identities

No network authorization receipt or execution is created by this planning step.

## Readiness State Machine

V2 state fields:

- `PHASE35_V1_DATASET_READY`
- `PHASE35B_V2_PROTOCOL_PROPOSED`
- `PHASE35B_V2_IMPLEMENTED`
- `PHASE35B_V2_FROZEN`
- `POINT_IN_TIME_INTEGRITY_READY`
- `FORECAST_CALIBRATION_DATA_READY`
- `FIXED_TIME_MARKET_ALPHA_DATA_READY`
- `EARLY_MARKET_ALPHA_DATA_READY`
- `PHASE35B_V2_DATASET_READY`

Until correction recovery execution + final V2 audit + freeze, `PHASE35B_V2_DATASET_READY=NOT_YET_ESTABLISHED`.

## Pre-Registration Boundary

- Historical CLOB remains descriptive-only.
- No provider-network requests are required for this V2 implementation pass.
- No freeze creation is performed here.
- No Phase 4 start, alpha result, or PnL computation is included.

## Known Limitations

- Missing family-owned CLOB histories remain unresolved until separately authorized correction recovery executes.
- Left-censored T0 is explicitly tracked; true first-observable claim is limited to uncensored cohort only.
