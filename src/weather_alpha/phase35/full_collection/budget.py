"""Strict hard-cap estimator and fail-closed enforcer. No network."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from weather_alpha.phase35.full_collection.policy import (
    CLOB_ATTEMPT_CAP,
    ECMWF_ATTEMPT_CAP,
    ECMWF_LOGICAL_IDENTITY_CAP,
    END_DATE,
    GAMMA_ATTEMPT_CAP,
    GLOBAL_GET_ATTEMPT_CAP,
    MARKET_PROVIDER,
    MAX_ATTEMPTS_PER_IDENTITY,
    PLANNING_BASELINE_CLOB_IDENTITIES,
    PLANNING_BASELINE_ECMWF_LOGICAL,
    PLANNING_BASELINE_GAMMA_IDENTITIES,
    PREFLIGHT_OK,
    PRICE_PROVIDER,
    REQUEST_BUDGET_REDESIGN_REQUIRED,
    REQUEST_POLICY_VERSION,
    RETRY_MODE,
    START_DATE,
    STORAGE_HARD_CAP_BYTES,
    STORAGE_PREFLIGHT_MIN_BYTES,
    TARGET_CITIES_CANONICAL,
    THEORETICAL_ENVELOPE_AUTHORIZATION,
    YES_PENDING_FINAL_REVIEW,
)
from weather_alpha.phase35.full_collection.schedule import (
    clob_identities,
    ecmwf_logical_identities,
    gamma_identities,
)


class DiskProbe(Protocol):
    def free_bytes(self, path: Path) -> int: ...

    def used_bytes(self, path: Path) -> int: ...


@dataclass(frozen=True, slots=True)
class StaticDiskProbe:
    free_bytes_value: int
    used_bytes_value: int = 0

    def free_bytes(self, path: Path) -> int:
        del path
        return self.free_bytes_value

    def used_bytes(self, path: Path) -> int:
        del path
        return self.used_bytes_value


class RealDiskProbe:
    """Local filesystem probe. Never contacts providers."""

    def free_bytes(self, path: Path) -> int:
        probe = path if path.exists() else Path.cwd()
        return int(shutil.disk_usage(probe).free)

    def used_bytes(self, path: Path) -> int:
        if not path.exists():
            return 0
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


@dataclass(frozen=True, slots=True)
class BudgetEstimate:
    gamma_identities: int
    clob_identities: int
    ecmwf_logical_identities: int
    gamma_initial_attempts: int
    clob_initial_attempts: int
    ecmwf_initial_attempts: int
    initial_total_attempts: int
    gamma_retry_reserve: int
    ecmwf_retry_reserve: int
    clob_retry_reserve: int
    gamma_max_attempts: int
    clob_max_attempts: int
    ecmwf_max_attempts: int
    global_max_attempts: int
    theoretical_gamma_attempts: int
    theoretical_ecmwf_attempts: int
    theoretical_clob_attempts: int
    theoretical_global_attempts: int
    theoretical_envelope_authorized: bool
    max_attempts_per_identity: int
    planning_baseline_ecmwf_logical: int
    computed_ecmwf_logical: int

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "clob_identities": self.clob_identities,
            "clob_initial_attempts": self.clob_initial_attempts,
            "clob_max_attempts": self.clob_max_attempts,
            "clob_retry_reserve": self.clob_retry_reserve,
            "computed_ecmwf_logical": self.computed_ecmwf_logical,
            "ecmwf_initial_attempts": self.ecmwf_initial_attempts,
            "ecmwf_logical_identities": self.ecmwf_logical_identities,
            "ecmwf_max_attempts": self.ecmwf_max_attempts,
            "ecmwf_retry_reserve": self.ecmwf_retry_reserve,
            "gamma_identities": self.gamma_identities,
            "gamma_initial_attempts": self.gamma_initial_attempts,
            "gamma_max_attempts": self.gamma_max_attempts,
            "gamma_retry_reserve": self.gamma_retry_reserve,
            "global_max_attempts": self.global_max_attempts,
            "initial_total_attempts": self.initial_total_attempts,
            "max_attempts_per_identity": self.max_attempts_per_identity,
            "planning_baseline_ecmwf_logical": self.planning_baseline_ecmwf_logical,
            "theoretical_clob_attempts": self.theoretical_clob_attempts,
            "theoretical_ecmwf_attempts": self.theoretical_ecmwf_attempts,
            "theoretical_envelope_authorized": self.theoretical_envelope_authorized,
            "theoretical_gamma_attempts": self.theoretical_gamma_attempts,
            "theoretical_global_attempts": self.theoretical_global_attempts,
        }


@dataclass(frozen=True, slots=True)
class BudgetEnforcement:
    allowed: bool
    status: str
    network_authorized: bool
    full_collection_start_allowed: str
    theoretical_envelope_authorized: bool
    violated_caps: tuple[str, ...]
    estimate: BudgetEstimate
    storage_preflight_ok: bool
    detail: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "detail": list(self.detail),
            "estimate": self.estimate.as_dict(),
            "full_collection_start_allowed": self.full_collection_start_allowed,
            "network_authorized": self.network_authorized,
            "request_budget": request_budget_report(self.estimate),
            "status": self.status,
            "storage_preflight_ok": self.storage_preflight_ok,
            "theoretical_envelope_authorized": self.theoretical_envelope_authorized,
            "violated_caps": list(self.violated_caps),
        }


def provider_budget_bucket(provider: str) -> str:
    lowered = provider.strip().lower()
    if lowered == MARKET_PROVIDER or lowered.startswith("gamma"):
        return "gamma"
    if lowered == PRICE_PROVIDER or "clob" in lowered:
        return "clob"
    return "ecmwf"


def request_budget_report(estimate: BudgetEstimate) -> dict[str, Any]:
    return {
        "cap_bounded_maxima": {
            "clob": estimate.clob_max_attempts,
            "ecmwf": estimate.ecmwf_max_attempts,
            "gamma": estimate.gamma_max_attempts,
            "global": estimate.global_max_attempts,
        },
        "initial": {
            "clob": estimate.clob_initial_attempts,
            "ecmwf": estimate.ecmwf_initial_attempts,
            "gamma": estimate.gamma_initial_attempts,
            "total": estimate.initial_total_attempts,
        },
        "policy_version": REQUEST_POLICY_VERSION,
        "preflight": PREFLIGHT_OK,
        "retry_mode": RETRY_MODE,
        "retry_reserves": {
            "clob": estimate.clob_retry_reserve,
            "ecmwf": estimate.ecmwf_retry_reserve,
            "gamma": estimate.gamma_retry_reserve,
        },
        "theoretical_all_transient_once": {
            "clob": estimate.theoretical_clob_attempts,
            "ecmwf": estimate.theoretical_ecmwf_attempts,
            "gamma": estimate.theoretical_gamma_attempts,
            "global": estimate.theoretical_global_attempts,
        },
        "theoretical_envelope": THEORETICAL_ENVELOPE_AUTHORIZATION,
        "theoretical_envelope_authorized": estimate.theoretical_envelope_authorized,
    }


def estimate_full_collection_budget(
    *,
    start: str = START_DATE,
    end: str = END_DATE,
    cities: tuple[str, ...] = TARGET_CITIES_CANONICAL,
) -> BudgetEstimate:
    gamma = len(gamma_identities(start=start, end=end, cities=cities))
    clob = len(clob_identities(start=start, end=end, cities=cities))
    computed_ecmwf = len(ecmwf_logical_identities(start=start, end=end, cities=cities))
    # Fail-closed: never under-count relative to the retained planning baseline.
    ecmwf = max(computed_ecmwf, PLANNING_BASELINE_ECMWF_LOGICAL)
    per_identity = MAX_ATTEMPTS_PER_IDENTITY
    theoretical_gamma = gamma * per_identity
    theoretical_ecmwf = ecmwf * per_identity
    theoretical_clob = clob * per_identity
    theoretical_global = theoretical_gamma + theoretical_ecmwf + theoretical_clob
    gamma_max = min(theoretical_gamma, GAMMA_ATTEMPT_CAP)
    ecmwf_max = min(theoretical_ecmwf, ECMWF_ATTEMPT_CAP)
    clob_max = min(theoretical_clob, CLOB_ATTEMPT_CAP)
    global_max = min(gamma_max + ecmwf_max + clob_max, GLOBAL_GET_ATTEMPT_CAP)
    theoretical_authorized = (
        theoretical_gamma <= GAMMA_ATTEMPT_CAP
        and theoretical_ecmwf <= ECMWF_ATTEMPT_CAP
        and theoretical_clob <= CLOB_ATTEMPT_CAP
        and theoretical_global <= GLOBAL_GET_ATTEMPT_CAP
    )
    return BudgetEstimate(
        gamma_identities=gamma,
        clob_identities=clob,
        ecmwf_logical_identities=ecmwf,
        gamma_initial_attempts=gamma,
        clob_initial_attempts=clob,
        ecmwf_initial_attempts=ecmwf,
        initial_total_attempts=gamma + clob + ecmwf,
        gamma_retry_reserve=GAMMA_ATTEMPT_CAP - gamma,
        ecmwf_retry_reserve=ECMWF_ATTEMPT_CAP - ecmwf,
        clob_retry_reserve=CLOB_ATTEMPT_CAP - clob,
        gamma_max_attempts=gamma_max,
        clob_max_attempts=clob_max,
        ecmwf_max_attempts=ecmwf_max,
        global_max_attempts=global_max,
        theoretical_gamma_attempts=theoretical_gamma,
        theoretical_ecmwf_attempts=theoretical_ecmwf,
        theoretical_clob_attempts=theoretical_clob,
        theoretical_global_attempts=theoretical_global,
        theoretical_envelope_authorized=theoretical_authorized,
        max_attempts_per_identity=per_identity,
        planning_baseline_ecmwf_logical=PLANNING_BASELINE_ECMWF_LOGICAL,
        computed_ecmwf_logical=computed_ecmwf,
    )


def enforce_request_budget(
    estimate: BudgetEstimate | None = None,
    *,
    disk: DiskProbe | None = None,
    storage_root: Path | None = None,
) -> BudgetEnforcement:
    plan = estimate or estimate_full_collection_budget()
    violated: list[str] = []
    detail: list[str] = []
    if plan.gamma_identities != PLANNING_BASELINE_GAMMA_IDENTITIES:
        violated.append("gamma_identity_baseline")
        detail.append(
            f"gamma identities {plan.gamma_identities} != baseline "
            f"{PLANNING_BASELINE_GAMMA_IDENTITIES}"
        )
    if plan.clob_identities != PLANNING_BASELINE_CLOB_IDENTITIES:
        violated.append("clob_identity_baseline")
        detail.append(
            f"clob identities {plan.clob_identities} != baseline "
            f"{PLANNING_BASELINE_CLOB_IDENTITIES}"
        )
    if plan.gamma_max_attempts > GAMMA_ATTEMPT_CAP:
        violated.append("gamma_attempts")
        detail.append(
            f"gamma cap-bounded max attempts {plan.gamma_max_attempts} exceed cap "
            f"{GAMMA_ATTEMPT_CAP}"
        )
    if plan.ecmwf_logical_identities > ECMWF_LOGICAL_IDENTITY_CAP:
        violated.append("ecmwf_logical_identities")
        detail.append(
            f"ecmwf logical identities {plan.ecmwf_logical_identities} exceed cap "
            f"{ECMWF_LOGICAL_IDENTITY_CAP}"
        )
    if plan.ecmwf_max_attempts > ECMWF_ATTEMPT_CAP:
        violated.append("ecmwf_attempts")
        detail.append(
            f"ecmwf cap-bounded max attempts {plan.ecmwf_max_attempts} exceed cap "
            f"{ECMWF_ATTEMPT_CAP}"
        )
    if plan.clob_max_attempts > CLOB_ATTEMPT_CAP:
        violated.append("clob")
        detail.append(
            f"clob cap-bounded max attempts {plan.clob_max_attempts} exceed cap {CLOB_ATTEMPT_CAP}"
        )
    if plan.global_max_attempts > GLOBAL_GET_ATTEMPT_CAP:
        violated.append("global_get_attempts")
        detail.append(
            f"global cap-bounded max GET attempts {plan.global_max_attempts} exceed cap "
            f"{GLOBAL_GET_ATTEMPT_CAP}"
        )
    if plan.gamma_retry_reserve < 0:
        violated.append("gamma_retry_reserve")
        detail.append(f"gamma retry reserve {plan.gamma_retry_reserve} is negative")
    if plan.ecmwf_retry_reserve < 0:
        violated.append("ecmwf_retry_reserve")
        detail.append(f"ecmwf retry reserve {plan.ecmwf_retry_reserve} is negative")
    if plan.clob_retry_reserve < 0:
        violated.append("clob_retry_reserve")
        detail.append(f"clob retry reserve {plan.clob_retry_reserve} is negative")

    storage_ok = True
    if disk is not None:
        root = storage_root or Path("data/phase35/historical")
        free = disk.free_bytes(root)
        used = disk.used_bytes(root)
        if free < STORAGE_PREFLIGHT_MIN_BYTES:
            storage_ok = False
            violated.append("storage_preflight")
            detail.append(
                f"free bytes {free} below preflight minimum {STORAGE_PREFLIGHT_MIN_BYTES}"
            )
        if used >= STORAGE_HARD_CAP_BYTES:
            storage_ok = False
            violated.append("storage_hard_cap")
            detail.append(f"used bytes {used} at or above hard cap {STORAGE_HARD_CAP_BYTES}")

    allowed = not violated
    if allowed:
        status = PREFLIGHT_OK
        start_allowed = YES_PENDING_FINAL_REVIEW
    else:
        status = REQUEST_BUDGET_REDESIGN_REQUIRED
        start_allowed = "NO"
    return BudgetEnforcement(
        allowed=allowed,
        status=status,
        network_authorized=False,
        full_collection_start_allowed=start_allowed,
        theoretical_envelope_authorized=plan.theoretical_envelope_authorized,
        violated_caps=tuple(violated),
        estimate=plan,
        storage_preflight_ok=storage_ok,
        detail=tuple(detail),
    )
