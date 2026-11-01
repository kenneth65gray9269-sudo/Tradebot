"""Paper fills update the position. Live failures are journaled and not retried."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from bot.executor import Executor
from bot.rest import RestError
from tests.helpers import make


def _exec(tmp_path, **kw):
    cfg, st, r = make(tmp_path, **kw)
    return cfg, st, r, Executor(cfg, st, r, rest=None)


def test_paper_executor_sends_nothing_and_journals(tmp_path):
    _, st, _, ex = _exec(tmp_path)
    res = asyncio.run(ex.submit("buy", 1, note="unit test"))
    assert res["ok"] and res["paper"] and res["fill_price"] == st.book.ask
    assert st.position.volume == 1
    assert st.position.direction == "buy"
    assert st.position.entry_price == st.book.ask
    rec = [json.loads(line) for line in (Path(tmp_path) / "orders.jsonl").read_text().splitlines()]
    assert rec[-1]["event"] == "paper_fill" and rec[-1]["mode"] == "paper"


def test_paper_sell_fills_at_bid_and_close_flattens_once(tmp_path):
    _, st, _, ex = _exec(tmp_path)
    res = asyncio.run(ex.submit("sell", 1))
    assert res["fill_price"] == st.book.bid
    assert st.position.direction == "sell"
    assert st.position.is_long is False
    entry = st.position.entry_price
    st.book.ask = entry * 0.99
    asyncio.run(ex.close_all(note="stop"))
    assert st.position.volume == 0
    assert st.position.entry_price == 0
    expected = (entry - entry * 0.99) * 1 * st.contract_size
    assert abs(st.realized_today - expected) < 1e-9
    asyncio.run(ex.close_all(note="again"))
    assert st.realized_today == expected
    events = [json.loads(line)["event"] for line in (Path(tmp_path) / "orders.jsonl").read_text().splitlines()]
    assert events.count("paper_close_all") == 2


def test_paper_averages_adds_and_reduce_only_does_not_reverse(tmp_path):
    _, st, _, ex = _exec(tmp_path)
    asyncio.run(ex.submit("buy", 1))
    first = st.book.ask
    st.book.ask = first * 1.01
    st.book.ts = __import__("time").time()
    asyncio.run(ex.submit("buy", 1))
    assert st.position.volume == 2
    assert abs(st.position.entry_price - (first + first * 1.01) / 2) < 1e-12
    st.book.bid = first
    asyncio.run(ex.submit("sell", 1, reduce_only=True))
    assert st.position.volume == 1
    assert st.position.direction == "buy"
    asyncio.run(ex.submit("sell", 5, reduce_only=True))
    assert st.position.volume == 0


def test_fractional_volume_is_rejected_and_not_truncated(tmp_path):
    _, st, _, ex = _exec(tmp_path)
    res = asyncio.run(ex.submit("buy", 0.9))
    assert not res["ok"] and "whole number" in res["reason"]
    assert ex.sent == 0 and ex.rejected == 1 and st.position.volume == 0


def test_client_order_ids_are_unique_and_in_range(tmp_path):
    _, _, _, ex = _exec(tmp_path)
    ids = {ex.build_order("buy", 1)["client_order_id"] for _ in range(500)}
    assert len(ids) == 500
    assert all(1 <= int(i) <= 9223372036854775807 for i in ids)


def test_client_order_ids_unique_inside_one_millisecond(tmp_path):
    _, _, _, ex = _exec(tmp_path)
    with patch("bot.executor.time.time", return_value=1_700_000_000.123):
        ids = [ex.build_order("buy", 1)["client_order_id"] for _ in range(1500)]
    assert len(set(ids)) == 1500
    assert all(1 <= int(i) <= 9223372036854775807 for i in ids)


def test_rejected_order_is_journalled_and_not_sent(tmp_path):
    _, _, _, ex = _exec(tmp_path, contract_size=0.001, price=77700.0)
    res = asyncio.run(ex.submit("buy", 1))
    assert not res["ok"] and ex.sent == 0 and ex.rejected == 1
    rec = [json.loads(line) for line in (Path(tmp_path) / "orders.jsonl").read_text().splitlines()]
    assert rec[-1]["event"] == "rejected"


class _Rest:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    async def place_order(self, order):
        self.calls += 1
        if self.fail:
            raise RestError("network", {"code": 500})
        return {"order_id": "42", "client_order_id": order["client_order_id"]}

    async def cancel_all(self, code):
        raise RestError("cancel down")

    async def close_position_market(self, code, position_side=None):
        return {"closed": code}


def test_live_failure_is_journaled_once_and_cancel_failure_is_journaled(tmp_path):
    cfg, st, r = make(tmp_path)
    cfg.paper = False
    rest = _Rest(fail=True)
    ex = Executor(cfg, st, r, rest)
    res = asyncio.run(ex.submit("buy", 1))
    assert not res["ok"] and rest.calls == 1 and st.position.volume == 0
    cancel = asyncio.run(ex.cancel_all(note="shutdown"))
    assert not cancel["ok"]
    close = asyncio.run(ex.close_all(note="flat"))
    assert close["ok"] and close["response"]["closed"] == "SUN-USDT"
    events = [json.loads(line)["event"] for line in (Path(tmp_path) / "orders.jsonl").read_text().splitlines()]
    assert events == ["failed", "cancel_all_failed", "close_all"]


def test_limit_order_requires_a_price(tmp_path):
    _, _, _, ex = _exec(tmp_path)
    try:
        ex.build_order("buy", 1, order_type="limit")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "price" in str(exc)
    order = ex.build_order("buy", 1, order_type="limit", price=0.02, reduce_only=True)
    assert order["price"] == "0.02" and order["reduce_only"] == 1
