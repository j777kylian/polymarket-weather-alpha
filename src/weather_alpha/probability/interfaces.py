"""Phase 1/2 probability scaffolding. No fake alpha."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BucketProbability:
    label: str
    probability: float


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    status: str
    reason: str
    scores: tuple[tuple[str, float], ...] = ()

    @classmethod
    def insufficient(cls, reason: str) -> CalibrationReport:
        return cls(status="insufficient_data", reason=reason, scores=())


class ProbabilityModel:
    """Interface only. Calibration/fitting is not implemented in Phase 1/2."""

    def predict_bucket_probabilities(self, market_id: str) -> tuple[BucketProbability, ...]:
        raise NotImplementedError(
            "Phase 1/2 scaffold: probability model is not implemented; "
            "refusing to invent bucket probabilities"
        )

    def calibrate(self, *_args: Any, **_kwargs: Any) -> CalibrationReport:
        return CalibrationReport.insufficient("no labeled resolved sample loaded")
