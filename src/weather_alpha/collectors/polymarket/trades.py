"""Deterministic public-trade identity. transactionHash is not unique."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def public_trade_id(row: dict[str, Any]) -> str:
    explicit = row.get("id") or row.get("trade_id") or row.get("tradeId")
    if explicit not in (None, ""):
        return str(explicit)
    parts = {
        "transactionHash": row.get("transactionHash") or row.get("transaction_hash"),
        "conditionId": row.get("conditionId") or row.get("condition_id"),
        "asset": row.get("asset"),
        "timestamp": row.get("timestamp"),
        "side": row.get("side"),
        "price": row.get("price"),
        "size": row.get("size"),
        "outcome": row.get("outcome"),
        "outcomeIndex": row.get("outcomeIndex")
        if row.get("outcomeIndex") is not None
        else row.get("outcome_index"),
    }
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
