"""Preflight and one paper loop, with the exchange replaced by fakes."""
import asyncio
import json
from unittest.mock import patch

from bot.rest import RestError
from bot.runner import Bot, _fee_value, _one_way_fee
from bot.strategies.base import Intent, Strategy
from tests.helpers import make


class Scripted(Strategy):
    name = "script"

    def __init__(self, cfg, state, actions):
        super().__init__(cfg, state)
        self.actions = list(actions)

    def on_tick(self):
        if self.actions:
            return self.actions.pop(0)
        return Intent("hold", reason="idle")


class FakeRest:
    def __init__(self, *, size="1", status=1, ask=0.1, bid=0.099, fee=None, fee_error=None):
        self.size = size
        self.status = status
        self.ask = ask
        self.bid = bid
        self.fee = {"taker_fee_rate": 0.0004, "maker_fee_rate": 0.0002} if fee is None else fee
        self.fee_error = fee_error
        self.closed = False

    async def contract_info(self, code):
        return {"contract_size": self.size, "price_tick": "0.0001", "contract_status": self.status}

    async def risk_limit(self, code):
        return {"max_lever": 20, "maintenance_margin_rate": 0.005, "min_volume": 1}

    async def bbo(self, code):
        return {"ask": [self.ask, 10], "bid": [self.bid, 10]}

    async def fee_rate(self, code):
        if self.fee_error:
            raise self.fee_error
        return self.fee

    async def account_balance(self):
        return {"equity": "0", "available_margin": "0"}

    async def aclose(self):
        self.closed = True


def _bot(tmp_path, rest, **kw):
    cfg, _, _ = make(tmp_path, **kw)
    bot = Bot(cfg)
    asyncio.run(bot.rest.aclose())
    bot.rest = rest
    return bot


def test_fee_value_keeps_an_explicit_zero():
    assert _fee_value({"taker_fee_rate": 0, "taker": 0.1}, "taker_fee_rate", "taker") == 0.0
    assert _fee_value({"taker": 0}, "taker_fee_rate", "taker") == 0.0
    assert _fee_value({}, "taker_fee_rate", "taker") is None
    assert _fee_value({"taker_fee_rate": ""}, "taker_fee_rate") is None


def test_one_way_fee_reads_the_live_open_and_close_fields():
    payload = {"open_taker_fee": 0.0004, "close_taker_fee": 0.0002,
               "open_maker_fee": 0, "close_maker_fee": 0.0001}
    assert abs(_one_way_fee(payload, ("taker_fee_rate", "taker"),
                            "open_taker_fee", "close_taker_fee") - 0.0003) < 1e-12
    assert _one_way_fee(payload, ("maker_fee_rate", "maker"),
                        "open_maker_fee", "close_maker_fee") == 0.00005
    assert _one_way_fee({"taker_fee_rate": 0, "open_taker_fee": 0.1},
                        ("taker_fee_rate", "taker"), "open_taker_fee", "close_taker_fee") == 0.0


def test_preflight_ready_when_one_contract_is_affordable(tmp_path):
    bot = _bot(tmp_path, FakeRest(ask=0.1, bid=0.099, size="1"))
    report = asyncio.run(bot.preflight())
    assert report["ready"] is True
    assert report["max_contracts_allowed"] >= 1
    assert report["keys"] == "not configured - paper mode only"
    assert bot.state.contract_size == 1.0


def test_preflight_rejects_an_unaffordable_contract_and_a_zero_fee_is_kept(tmp_path):
    bot = _bot(tmp_path, FakeRest(ask=100, bid=99, size="1", fee={"taker_fee_rate": 0, "maker_fee_rate": 0}),
               access_key="ak", secret_key="sk")
    report = asyncio.run(bot.preflight())
    assert report["ready"] is False
    assert any("cannot afford" in p for p in report["problems"])
    assert bot.state.fee_taker == 0.0
    assert report["min_profitable_move_pct"] == 0.0


def test_preflight_reports_status_and_fee_errors(tmp_path):
    bot = _bot(tmp_path, FakeRest(status=0, fee_error=RestError("no fee")),
               access_key="ak", secret_key="sk")
    report = asyncio.run(bot.preflight())
    assert report["ready"] is False
    assert any("not currently tradeable" in p for p in report["problems"])
    assert any("fee rate" in p for p in report["problems"])


def test_run_stops_before_trading_when_preflight_fails(tmp_path):
    rest = FakeRest(ask=100, bid=99, size="1")
    bot = _bot(tmp_path, rest)
    assert asyncio.run(bot.run()) == 1
    assert rest.closed is True


def test_paper_step_enters_stops_out_and_halt_blocks_the_next_entry(tmp_path):
    bot = _bot(tmp_path, FakeRest(), stop_loss_pct=1.0)
    bot.strategy = Scripted(bot.cfg, bot.state, [Intent("buy", volume=1, reason="go")])
    bot.state.book.bid, bot.state.book.ask, bot.state.book.ts = 0.099, 0.1, __import__("time").time()
    bot.state.contract_size = 1
    bot.state.min_volume = 1
    bot.state.max_leverage = 20

    async def go():
        await bot._step()
        assert bot.state.position.volume == 1
        assert bot.state.position.direction == "buy"
        entry = bot.state.position.entry_price
        bot.state.book.bid = bot.state.book.ask = entry * 0.98
        await bot._step()
        assert bot.state.position.volume == 0
        assert bot.exec.sent == 1
        closes = [json.loads(line) for line in open(bot.exec._journal, encoding="utf-8")]
        assert sum(1 for row in closes if row["event"] == "paper_close_all") == 1
        await bot._step()
        assert sum(1 for line in open(bot.exec._journal, encoding="utf-8")
                   if '"paper_close_all"' in line) == 1
        bot.strategy.actions.append(Intent("buy", volume=1, reason="again"))
        bot.risk.halt("manual")
        await bot._step()
        assert bot.exec.sent == 1
        assert bot.exec.rejected == 0
        assert bot.state.position.volume == 0

    asyncio.run(go())


def test_daily_loss_closes_then_the_next_entry_is_rejected(tmp_path):
    bot = _bot(tmp_path, FakeRest(), max_daily_loss_usdt=2)
    bot.strategy = Scripted(bot.cfg, bot.state, [Intent("buy", volume=1, reason="go"),
                                                 Intent("buy", volume=1, reason="again")])
    bot.state.book.bid, bot.state.book.ask, bot.state.book.ts = 0.099, 0.1, __import__("time").time()
    bot.state.contract_size = 1
    bot.state.min_volume = 1
    bot.state.max_leverage = 20

    async def go():
        await bot._step()
        bot.state.add_realized(-3)
        bot.state.book.bid = bot.state.book.ask = bot.state.position.entry_price
        await bot._step()
        assert bot.state.position.volume == 0
        await bot._step()
        assert bot.risk.halted
        assert bot.exec.rejected == 1
        assert bot.state.position.volume == 0

    asyncio.run(go())


def test_live_halt_when_position_is_open_and_deadman_is_not_armed(tmp_path):
    bot = _bot(tmp_path, FakeRest())
    bot.cfg.paper = False
    bot.deadman.armed = False
    bot.state.position.volume = 1
    bot.state.position.position_side = "long"
    bot.state.position.entry_price = 0.1
    bot.state.position.opened_at = __import__("time").time()
    bot.state.book.bid = bot.state.book.ask = 0.1
    bot.state.book.ts = bot.state.position.opened_at
    asyncio.run(bot._step())
    assert bot.risk.halted
    assert "dead man" in bot.risk.halt_reason
    assert bot.state.position.volume == 1


def test_trade_loop_runs_one_step_then_stops(tmp_path):
    bot = _bot(tmp_path, FakeRest())
    bot.strategy = Strategy(bot.cfg, bot.state)
    bot.market.connected.set()
    calls = {"n": 0}

    async def fast_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] > 1:
            bot._stop.set()

    async def go():
        with patch("bot.runner.asyncio.sleep", fast_sleep):
            await bot._trade_loop()

    asyncio.run(go())
    assert calls["n"] == 2
    assert bot.exec.sent == 0
