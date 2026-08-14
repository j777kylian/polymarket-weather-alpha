"""Phase 1/2 backtest scaffolding. No simulated PnL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class BacktestRequest:
    as_of: datetime | None
    market_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BacktestResult:
    status: str
    reason: str
    pnl: float | None = None
    trades: tuple[str, ...] = ()


class BacktestEngine:
    def run(self, request: BacktestRequest) -> BacktestResult:
        if request.as_of is not None and request.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware UTC when provided")
        if not request.market_ids:
            return BacktestResult(
                status="insufficient_data",
                reason="no market ids supplied; refusing to invent a backtest",
                pnl=None,
            )
        return BacktestResult(
            status="not_implemented",
            reason="Phase 1/2 scaffold: executable-price backtest is not implemented",
            pnl=None,
        )
