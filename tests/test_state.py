"""Market and account state, using the field names the exchange actually sends."""
import time

from bot.state import State


def test_bbo_and_trades_update_the_book():
    st = State("JST-USDT")
    st.on_market({"ch": "market.JST-USDT.bbo", "tick": {"bid": [1.5], "ask": [1.6, 4]}})
    assert st.book.bid == 1.5 and st.book.bid_size == 0
    assert st.book.ask == 1.6 and st.book.ask_size == 4
    assert st.book.fresh
    st.on_market({"ch": "market.JST-USDT.trade.detail", "tick": {"data": [
        {"price": "1.55", "amount": "2", "direction": "buy", "ts": 1000},
    ]}})
    assert st.trades[-1]["price"] == 1.55
    assert st.trades[-1]["direction"] == "buy"


def test_position_uses_open_avg_price_and_direction():
    st = State("JST-USDT")
    st.on_private({"topic": "positions.JST-USDT", "data": [{
        "contract_code": "JST-USDT",
        "position_side": "both",
        "direction": "sell",
        "open_avg_price": "0.0151",
        "volume": "2",
        "lever_rate": 3,
        "profit_unreal": "-0.4",
    }]})
    assert st.position.entry_price == 0.0151
    assert st.position.direction == "sell"
    assert st.position.volume == 2
    assert st.position.unrealized == -0.4
    assert st.position.is_long is False
    opened = st.position.opened_at
    st.on_private({"topic": "positions", "data": [{
        "contract_code": "JST-USDT",
        "position_side": "both",
        "direction": "sell",
        "open_avg_price": "0.0152",
        "volume": "3",
        "profit_unreal": "0",
    }]})
    assert st.position.opened_at == opened
    assert st.position.volume == 3


def test_other_contracts_and_explicit_sides_are_respected():
    st = State("JST-USDT")
    st.on_private({"topic": "positions", "data": [
        {"contract_code": "BTC-USDT", "position_side": "long", "volume": "9",
         "open_avg_price": "100"},
        {"contract_code": "JST-USDT", "position_side": "short", "direction": "buy",
         "cost_open": "0.02", "volume": "1"},
    ]})
    assert st.position.volume == 1
    assert st.position.entry_price == 0.02
    assert st.position.is_long is False


def test_zero_open_price_is_kept_and_missing_price_falls_through():
    st = State("JST-USDT")
    st.on_private({"topic": "positions", "data": [{
        "contract_code": "JST-USDT", "position_side": "long", "volume": "1",
        "open_avg_price": 0, "cost_open": "5",
    }]})
    assert st.position.entry_price == 0.0
    st.on_private({"topic": "positions", "data": [{
        "contract_code": "JST-USDT", "position_side": "long", "volume": "1",
        "entry_price": "7",
    }]})
    assert st.position.entry_price == 7.0


def test_orders_accounts_and_day_rollover():
    st = State("JST-USDT")
    st.on_private({"topic": "orders", "data": {
        "client_order_id": "7", "status": 3, "profit": "0",
    }})
    assert "7" in st.open_orders
    st.on_private({"topic": "orders", "data": [
        {"client_order_id": "7", "status": 6, "real_profit": "-1.25"},
    ]})
    assert st.open_orders == {}
    assert st.realized_today == -1.25
    st.on_private({"topic": "orders", "data": [
        {"order_id": "9", "status": "canceled"},
        {"client_order_id": "8", "status": "filled", "profit": "0.5"},
    ]})
    assert "9" not in st.open_orders and "8" not in st.open_orders
    assert st.realized_today == -0.75
    st.day = "1999-01-01"
    st.add_realized(0.25)
    assert st.realized_today == 0.25
    assert st.day == time.strftime("%Y-%m-%d", time.gmtime())
    st.on_private({"topic": "accounts", "data": {"equity": "15.5", "available_margin": "14"}})
    assert st.equity == 15.5 and st.available == 14
    snap = st.snapshot()
    assert snap["contract"] == "JST-USDT"
    assert snap["realized_today"] == 0.25


def test_book_fresh_boundary():
    st = State("JST-USDT")
    st.book.bid, st.book.ask = 1, 1
    st.book.ts = time.time() - 10
    assert st.book.fresh is False
    st.book.ts = time.time() - 9.9
    assert st.book.fresh is True
    st.book.ts = 0
    assert st.book.fresh is False
