"""Risk gates, including the boundaries the engine uses."""
import time
from unittest.mock import patch

from tests.helpers import make


def test_max_contracts_from_capital(tmp_path):
    _, _, r = make(tmp_path)
    assert r.max_contracts() == int(15.0 // (0.0151 * 100)), r.max_contracts()


def test_explicit_position_cap_overrides_notional(tmp_path):
    _, _, r = make(tmp_path, max_position_contracts=2)
    assert r.max_contracts() == 2


def test_rejects_when_one_contract_unaffordable(tmp_path):
    _, _, r = make(tmp_path, contract_size=0.001, price=77700.0)
    assert r.max_contracts() == 0
    d = r.check({"volume": 1})
    assert not d and "cannot afford" in d.reason, d.reason


def test_rejects_stale_book(tmp_path):
    _, st, r = make(tmp_path)
    st.book.ts = time.time() - 60
    d = r.check({"volume": 1})
    assert not d and "stale" in d.reason
    assert r.check({"volume": 1, "reduce_only": True})


def test_book_fresh_and_rate_window_are_exclusive_at_the_boundary(tmp_path):
    _, st, r = make(tmp_path, max_orders_per_minute=1)
    now = 1_000_000.0
    with patch("bot.state.time.time", return_value=now):
        st.book.ts = now - 10
        assert st.book.fresh is False
        st.book.ts = now - 9.999
        assert st.book.fresh is True
    st.book.ts = time.time()
    with patch("bot.risk.time.time", return_value=now):
        r._order_times.append(now - 60)
        d = r.check({"volume": 1})
        assert not d and "rate limit" in d.reason
        r._order_times.clear()
        r._order_times.append(now - 60.001)
        assert r.check({"volume": 1})


def test_rejects_over_position_cap(tmp_path):
    _, st, r = make(tmp_path)
    st.position.volume = r.max_contracts()
    d = r.check({"volume": 1})
    assert not d and "exceeds the cap" in d.reason, d.reason


def test_rejects_over_leverage(tmp_path):
    _, _, r = make(tmp_path, lev=1)
    d = r.check({"volume": r.max_contracts() + 5})
    assert not d


def test_rejects_fractional_and_sub_minimum_volume(tmp_path):
    _, st, r = make(tmp_path)
    d = r.check({"volume": 0.9})
    assert not d and "whole number" in d.reason
    d = r.check({"volume": 1.5})
    assert not d and "whole number" in d.reason
    st.min_volume = 2
    d = r.check({"volume": 1})
    assert not d and "minimum" in d.reason
    assert r.check({"volume": 2.0})


def test_open_order_cap_and_exchange_leverage(tmp_path):
    cfg, st, r = make(tmp_path, max_open_orders=2)
    st.open_orders = {"a": {}, "b": {}}
    d = r.check({"volume": 1})
    assert not d and "open orders" in d.reason
    assert r.check({"volume": 1, "reduce_only": True})
    st.open_orders.clear()
    st.max_leverage = 2
    d = r.check({"volume": 1})
    assert not d and "exchange maximum" in d.reason


def test_rate_limit(tmp_path):
    _, _, r = make(tmp_path, max_orders_per_minute=2)
    assert r.check({"volume": 1}); r.record({})
    assert r.check({"volume": 1}); r.record({})
    d = r.check({"volume": 1})
    assert not d and "rate limit" in d.reason


def test_daily_loss_halts_and_close_still_works(tmp_path):
    _, st, r = make(tmp_path, max_daily_loss_usdt=2.0)
    st.realized_today = -1.0
    st.position.unrealized = 10.0
    assert not r.daily_loss_breached()
    st.position.unrealized = -1.0
    assert r.daily_loss_breached()
    st.position.unrealized = 0.0
    st.realized_today = -2.0
    assert r.daily_loss_breached()
    st.realized_today = -1.999
    assert not r.daily_loss_breached()
    st.add_realized(-2.5)
    d = r.check({"volume": 1})
    assert not d and r.halted
    assert r.check({"volume": 1, "reduce_only": True})
    assert r.check({"volume": 1, "close": True})
    assert not r.check({"volume": 1})


def test_closing_allowed_even_when_capped(tmp_path):
    _, st, r = make(tmp_path)
    st.position.volume = r.max_contracts()
    assert r.check({"volume": 1, "reduce_only": True})


def test_stop_loss_and_take_profit_for_long_and_short(tmp_path):
    _, st, r = make(tmp_path, stop_loss_pct=1.0, take_profit_pct=2.0)
    st.position.volume = 1
    st.position.position_side = "long"
    st.position.entry_price = 0.0151
    st.position.opened_at = time.time()
    st.book.bid = st.book.ask = 0.0151 * 0.98
    assert "stop loss" in (r.exit_reason() or "")
    st.book.bid = st.book.ask = 0.0151 * 1.03
    assert "take profit" in (r.exit_reason() or "")
    st.book.bid = st.book.ask = 0.0151
    assert r.exit_reason() is None

    st.position.position_side = "both"
    st.position.direction = "sell"
    st.book.bid = st.book.ask = 0.0151 * 1.02
    assert "stop loss" in (r.exit_reason() or "")
    st.book.bid = st.book.ask = 0.0151 * 0.97
    assert "take profit" in (r.exit_reason() or "")
    st.book.bid = st.book.ask = 0.0151
    assert r.exit_reason() is None


def test_max_hold_time(tmp_path):
    _, st, r = make(tmp_path, max_hold_seconds=10)
    st.position.volume = 1
    st.position.position_side = "long"
    st.position.entry_price = 0.0151
    st.position.opened_at = time.time() - 60
    assert "max hold" in (r.exit_reason() or "")


def test_fee_threshold(tmp_path):
    _, st, r = make(tmp_path, min_edge_over_fees=3.0)
    st.fee_taker = 0.0005
    assert abs(r.fee_threshold_pct() - 0.0005 * 2 * 100 * 3) < 1e-9
    st.fee_taker = 0.0
    assert r.fee_threshold_pct() == 0.0
    st.fee_taker = None
    assert r.fee_threshold_pct() is None
