"""Phase 3.5 full historical collection machinery.

GET-only research orchestrator. Request-budget policy v2 passes preflight as
YES_PENDING_FINAL_REVIEW. Real provider execution is disabled unless an
explicitly authorized immutable manifest passes its persisted integrity-anchor
checks. Manifest creation, authorization, and collection execution are separate
operations. No real collection has occurred.
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
