"""Momentum entries, and the loader."""
import time

import pytest

from bot.strategies import load
from bot.strategies.base import Strategy
from bot.strategies.momentum import MomentumStrategy
from tests.helpers import make


def _tape(st, steps, sign, step=0.0004):
    now = time.time() * 1000
    st.trades.clear()
    for i in range(steps):
        st.trades.append({"price": 0.0151 * (1 + sign * i * step), "amount": 1,
                          "direction": "buy" if sign > 0 else "sell",
                          "ts": now - (steps - i) * 500})


def test_strategy_holds_on_wide_spread(tmp_path):
    cfg, st, _ = make(tmp_path)
    st.fee_taker = 0.0005
    st.book.bid, st.book.ask = 0.0150, 0.0152
    _tape(st, 20, 1, 0.0005)
    i = MomentumStrategy(cfg, st).on_tick()
    assert i.action == "hold" and "spread" in i.reason, i.reason


def test_strategy_fires_on_momentum(tmp_path):
    cfg, st, _ = make(tmp_path)
    st.fee_taker = 0.00005
    st.book.bid, st.book.ask = 0.015100, 0.015101
    st.book.ts = time.time()
    _tape(st, 30, 1)
    i = MomentumStrategy(cfg, st).on_tick()
    assert i.action == "buy" and i.volume == 1, (i.action, i.reason)
    j = MomentumStrategy(cfg, st)
    _tape(st, 30, -1)
    assert j.on_tick().action == "sell"


def test_same_instance_cools_down(tmp_path):
    cfg, st, _ = make(tmp_path)
    st.fee_taker = 0.00005
    st.book.bid, st.book.ask = 0.015100, 0.015101
    st.book.ts = time.time()
    _tape(st, 30, 1)
    s = MomentumStrategy(cfg, st)
    assert s.on_tick().action == "buy"
    assert s.on_tick().action == "hold"
    assert "cooldown" in s.on_tick().reason


def test_holds_when_book_stale_position_open_or_tape_unusable(tmp_path):
    cfg, st, _ = make(tmp_path)
    st.book.ts = time.time() - 30
    assert "stale" in MomentumStrategy(cfg, st).on_tick().reason
    st.book.ts = time.time()
    st.position.volume = 1
    assert "position open" in MomentumStrategy(cfg, st).on_tick().reason
    st.position.volume = 0
    _tape(st, 9, 1)
    assert "not enough" in MomentumStrategy(cfg, st).on_tick().reason
    st.trades.clear()
    now = time.time() * 1000
    for i in range(12):
        st.trades.append({"price": 0.0 if i == 0 else 0.0151, "amount": 1,
                          "direction": "buy", "ts": now - (12 - i) * 500})
    assert MomentumStrategy(cfg, st).on_tick().action == "hold"


def test_loader(tmp_path):
    cfg, st, _ = make(tmp_path)
    assert isinstance(load("momentum", cfg, st), MomentumStrategy)
    assert isinstance(load("none", cfg, st), Strategy)
    assert load("none", cfg, st).on_tick().action == "hold"
    with pytest.raises(ValueError, match="Unknown strategy"):
        load("grid", cfg, st)
