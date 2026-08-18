"""Phase 3.5 full historical collection pre-network contract.

GET-only research machinery. Request-budget policy v2 passes preflight as
YES_PENDING_FINAL_REVIEW. Live collection is not an execution grant.
"""

from __future__ import annotations

from weather_alpha.phase35.full_collection.policy import (
    REQUEST_BUDGET_REDESIGN_REQUIRED,
    REQUEST_POLICY_VERSION,
    YES_PENDING_FINAL_REVIEW,
)

__all__ = [
    "REQUEST_BUDGET_REDESIGN_REQUIRED",
    "REQUEST_POLICY_VERSION",
    "YES_PENDING_FINAL_REVIEW",
    "__doc__",
]
