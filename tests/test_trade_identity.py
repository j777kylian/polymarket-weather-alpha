from weather_alpha.collectors.polymarket.trades import public_trade_id


def test_explicit_trade_id_is_preferred() -> None:
    row = {
        "id": "trade-explicit",
        "transactionHash": "0xabc",
        "conditionId": "0xcond",
        "asset": "token-a",
        "timestamp": 1720000000,
        "side": "BUY",
        "price": 0.4,
        "size": 10,
        "outcome": "Yes",
        "outcomeIndex": 0,
    }
    assert public_trade_id(row) == "trade-explicit"


def test_shared_transaction_hash_yields_distinct_ids() -> None:
    base = {
        "transactionHash": "0xabc",
        "conditionId": "0xcond",
        "timestamp": 1720000000,
        "side": "BUY",
        "price": 0.4,
        "size": 10,
        "outcomeIndex": 0,
    }
    first = {**base, "asset": "token-a", "outcome": "Yes"}
    second = {**base, "asset": "token-b", "outcome": "No"}
    assert public_trade_id(first) != public_trade_id(second)
    assert public_trade_id(first) == public_trade_id(dict(first))
