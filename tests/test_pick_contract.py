"""Contract ranking. The HTTP fetch stays out of this test."""
from tools.pick_contract import rank_contracts


def _contracts():
    return [
        {"contract_code": "JST-USDT", "contract_size": 1, "contract_status": 1},
        {"contract_code": "BTC-USDT", "contract_size": 0.001, "contract_status": 1},
        {"contract_code": "DEAD-USDT", "contract_size": 1, "contract_status": 0},
        {"contract_code": "ZERO-USDT", "contract_size": 1, "contract_status": 1},
    ]


def _ticks():
    return [
        {"contract_code": "JST-USDT", "ask": [1.0, 1], "bid": [0.998, 1]},          # 0.2002%
        {"contract_code": "BTC-USDT", "ask": [100000, 1], "bid": [99990, 1]},
        {"contract_code": "DEAD-USDT", "ask": [1, 1], "bid": [0.999, 1]},
        {"contract_code": "ZERO-USDT", "ask": [0, 1], "bid": [0, 1]},
        {"contract_code": "MISSING-USDT", "ask": [1, 1], "bid": [0.999, 1]},
        {"contract_code": "WIDE-USDT", "ask": [1.01, 1], "bid": [1.0, 1]},
    ]


def test_filters_sort_and_the_spread_boundary():
    rows = rank_contracts(_contracts() + [
        {"contract_code": "WIDE-USDT", "contract_size": 1, "contract_status": 1},
        {"contract_code": "EDGE-USDT", "contract_size": 1, "contract_status": 1},
    ], _ticks() + [
        {"contract_code": "EDGE-USDT", "ask": [1.00125, 1], "bid": [0.99875, 1]},
    ], capital=15, leverage=3, min_contracts=5, max_spread=0.25)
    names = [row["contract"] for row in rows]
    assert "BTC-USDT" not in names          # one contract costs more than the budget
    assert "DEAD-USDT" not in names
    assert "ZERO-USDT" not in names
    assert "MISSING-USDT" not in names
    assert "WIDE-USDT" not in names          # spread above 0.25
    assert names[0] == "JST-USDT"
    assert "EDGE-USDT" in names             # spread equal to the cap is allowed
    assert rows[0]["contracts_affordable"] == int(45 // 1.0)
    assert rows[0]["break_even_move_pct"] == rows[0]["spread_pct"]


def test_empty_when_nothing_qualifies():
    assert rank_contracts(_contracts(), _ticks(), capital=15, leverage=1,
                          min_contracts=100, max_spread=0.01) == []
