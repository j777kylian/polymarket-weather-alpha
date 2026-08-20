"""Frozen Phase 3.5 full historical collection policy.

Budget preflight is not a collection execution grant. Real provider execution
requires a separately persisted authorization receipt bound to an immutable
manifest. No real collection has occurred.
"""

from __future__ import annotations

from typing import Final

from weather_alpha.phase35.config import PRE_REGISTERED_CHECKPOINT_HOURS

SCHEMA_VERSION: Final = "phase35-full-collection-manifest-v1"
PARSER_SCHEMA_VERSION: Final = "phase35-full-collection-parser-v1"
AUTHORIZATION_SCHEMA_VERSION: Final = "phase35-full-collection-authorization-v1"
HASH_ALGORITHM: Final = "sha256"

START_DATE: Final = "2026-03-01"
END_DATE: Final = "2026-05-29"
TARGET_CITIES_CANONICAL: Final[tuple[str, ...]] = (
    "amsterdam",
    "london",
    "milan",
    "munich",
    "new york",
    "paris",
)
CHECKPOINTS: Final[tuple[int, ...]] = PRE_REGISTERED_CHECKPOINT_HOURS
assert CHECKPOINTS == (48, 24, 12, 6, 3, 1)

FORECAST_PROVIDER: Final = "open_meteo_single_runs"
FORECAST_MODEL: Final = "ecmwf_ifs"
FORECAST_AVAILABILITY_RULE: Final = "available_at_lte_decision_ts"
MARKET_PROVIDER: Final = "polymarket_gamma_public_search"
PRICE_PROVIDER: Final = "polymarket_clob_prices_history"
RAW_STORAGE_NAMESPACE: Final = "data/phase35/historical/raw/"
STABLE_PROVENANCE_PREFIX: Final = "historical/raw/"

HTTP_METHOD: Final = "GET"
TIMEOUT_SECONDS: Final = 35.0
CONCURRENCY: Final = 1
INTER_ATTEMPT_DELAY_SECONDS: Final = 2.0
MAX_RETRIES: Final = 1
MAX_ATTEMPTS_PER_IDENTITY: Final = MAX_RETRIES + 1
RETRY_AFTER_CAP_SECONDS: Final = 60.0
RETRYABLE_HTTP_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
TRANSIENT_5XX: Final[frozenset[int]] = frozenset({500, 502, 503, 504})
RETRY_ONLY: Final[tuple[str, ...]] = (
    "429",
    "timeout",
    "tls",
    "transient_transport",
    "transient_5xx",
)
RETRY_MODE: Final = "classification_triggered"
REQUEST_POLICY_VERSION: Final = "phase35-full-collection-request-policy-v2"

GAMMA_ATTEMPT_CAP: Final = 600
ECMWF_LOGICAL_IDENTITY_CAP: Final = 5000
ECMWF_ATTEMPT_CAP: Final = 10000
CLOB_ATTEMPT_CAP: Final = 600
GLOBAL_GET_ATTEMPT_CAP: Final = 11200
STORAGE_PREFLIGHT_MIN_BYTES: Final = 2 * 1024**3
STORAGE_HARD_CAP_BYTES: Final = 3 * 1024**3

PLANNING_BASELINE_GAMMA_IDENTITIES: Final = 540
PLANNING_BASELINE_ECMWF_LOGICAL: Final = 4829
PLANNING_BASELINE_CLOB_IDENTITIES: Final = 540

REQUEST_BUDGET_REDESIGN_REQUIRED: Final = "REQUEST_BUDGET_REDESIGN_REQUIRED"
PREFLIGHT_OK: Final = "PREFLIGHT_OK"
YES_PENDING_FINAL_REVIEW: Final = "YES_PENDING_FINAL_REVIEW"
THEORETICAL_ENVELOPE_AUTHORIZATION: Final = "NOT_AUTHORIZED"

GAMMA_ENDPOINT: Final = "https://gamma-api.polymarket.com/public-search"
CLOB_ENDPOINT: Final = "https://clob.polymarket.com/prices-history"
ECMWF_ENDPOINT: Final = "https://single-runs-api.open-meteo.com/v1/forecast"
ENDPOINT_ALLOWLIST: Final[tuple[str, ...]] = (GAMMA_ENDPOINT, CLOB_ENDPOINT, ECMWF_ENDPOINT)

CANDIDATE_SELECTION_RULE: Final = (
    "latest_open_meteo_single_runs_ecmwf_ifs_with_available_at_lte_decision_ts; "
    "tie-break available_at, issued_at, run_param"
)
PRICE_SELECTION_RULE: Final = "price.observed_at <= decision_ts; descriptive_only"

CLOB_FIDELITY_MINUTES: Final = 60
CLOB_WINDOW_EXTRA_LOOKBACK_SECONDS: Final = 48 * 3600
CLOB_WINDOW_RULE_VERSION: Final = "phase35-clob-window-rule-v1"
RECOVERY_SCHEMA_VERSION: Final = "phase35-clob-recovery-manifest-v1"
RECOVERY_AUTHORIZATION_SCHEMA_VERSION: Final = "phase35-clob-recovery-authorization-v1"
RECOVERY_SCOPE_CLOB_ONLY: Final = "CLOB_ONLY"
RECOVERY_RAW_STORAGE_NAMESPACE: Final = "data/phase35/historical/recoveries/"
PARENT_CLOB_HTTP_FAILURE_SCALE: Final = 435
# V2 correction recovery is a separate five-identity path; not the legacy 435 planner.
CORRECTION_SCHEMA_VERSION: Final = "phase35-clob-correction-manifest-v1"
CORRECTION_AUTHORIZATION_SCHEMA_VERSION: Final = "phase35-clob-correction-authorization-v1"
CORRECTION_SCOPE_CLOB_V2: Final = "CLOB_CORRECTION_V2"
CORRECTION_RAW_STORAGE_NAMESPACE: Final = "data/phase35/historical/corrections/"
FIRST_RECOVERY_COLLECTION_ID: Final = "phase35-clob-recovery-1ea1f85f6672"
V2_CORRECTION_TARGET_COUNT: Final = 5
V2_CORRECTION_PROVENANCE_COUNT: Final = 5
CORRECTION_REASON_MISSING_CANONICAL_FAMILY_OWNED_HISTORY: Final = (
    "MISSING_CANONICAL_FAMILY_OWNED_CLOB_HISTORY"
)
# Persisting HTTP_FAILURE bodies would change ledger provenance (today null
# hashes/paths), resume hash verification, and the preserved parent 435 records.
HTTP_FAILURE_BODY_PERSISTENCE_DEFERRED: Final = True

OVERALL_COVERAGE_MIN: Final = 0.95
CITY_COVERAGE_MIN: Final = 0.90
LEAD_COVERAGE_MIN: Final = 0.90
MONTH_COVERAGE_MIN: Final = 0.85
SETTLEMENT_OVERALL_MIN: Final = 0.98
SETTLEMENT_SCORED_MIN: Final = 1.0
CLOB_OVERALL_MIN: Final = 0.85
CLOB_CITY_MIN: Final = 0.75
SYSTEMATIC_CLUSTER_CONSECUTIVE_DAYS: Final = 7
MAX_UNRESOLVED_SYSTEMATIC_CLUSTERS: Final = 0

FUTURE_LEAKAGE_MAX: Final = 0
RETROSPECTIVE_SUBSTITUTION_MAX: Final = 0
RAW_HASH_FAILURE_MAX: Final = 0
UNREVIEWED_INVALID_GROUP_MAX: Final = 0

FORBIDDEN_CANONICAL_PATH_PREFIXES: Final[tuple[str, ...]] = ("/tmp/", "/Users/", "/home/")
