"""Read-only calls against api.sunx.io using the local .env.

The key cannot trade and the wallet is empty. This test refuses every path
except public market data and the signed read endpoints. It never places,
cancels, or arms anything.
"""
import asyncio
from pathlib import Path

import pytest

from bot.config import Config
from bot.rest import RestError, SunXRest
from bot.runner import Bot

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"

READ_ONLY = {
    ("GET", "/sapi/v1/public/contract_info"),
    ("GET", "/sapi/v1/public/risk/limit"),
    ("GET", "/sapi/v1/market/bbo"),
    ("GET", "/sapi/v1/account/balance"),
    ("POST", "/sapi/v1/account/fee_rate"),
    ("GET", "/sapi/v1/trade/position/opens"),
    ("GET", "/sapi/v1/trade/order/opens"),
}


def _load_env(monkeypatch):
    if not ENV.exists():
        pytest.skip("no .env beside the bot")
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            monkeypatch.setenv(key.strip(), value.strip())


class GuardedRest(SunXRest):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.calls = []

    async def _request(self, method, path, params=None, body=None, signed=None):
        self.calls.append((method, path))
        if (method, path) not in READ_ONLY:
            raise AssertionError(f"refusing non-read call {method} {path}")
        return await super()._request(method, path, params, body, signed)


@pytest.fixture
def readonly_cfg(monkeypatch):
    _load_env(monkeypatch)
    cfg = Config()
    if not cfg.access_key or not cfg.secret_key:
        pytest.skip("SUNX keys are empty")
    cfg.paper = True
    return cfg


def test_readonly_account_is_empty_and_preflight_sends_nothing(readonly_cfg):
    rest = GuardedRest(readonly_cfg)

    async def go():
        try:
            balance = await rest.account_balance()
            positions = await rest.positions(readonly_cfg.contract_code)
            orders = await rest.open_orders(readonly_cfg.contract_code)
            try:
                fees = await rest.fee_rate(readonly_cfg.contract_code)
            except RestError as exc:
                fees = exc
            return balance, positions, orders, fees
        finally:
            await rest.aclose()

    balance, positions, orders, fees = asyncio.run(go())
    assert float(balance.get("equity") or 0) == 0.0
    assert float(balance.get("available_margin") or 0) == 0.0
    assert positions == []
    assert orders == []
    assert not isinstance(fees, Exception), fees
    assert "open_taker_fee" in fees and "close_taker_fee" in fees

    bot = Bot(readonly_cfg)
    asyncio.run(bot.rest.aclose())
    bot.rest = GuardedRest(readonly_cfg)

    async def preflight():
        try:
            return await bot.preflight()
        finally:
            await bot.rest.aclose()

    report = asyncio.run(preflight())
    assert report["mode"] == "paper"
    assert report["ready"] is True, report["problems"]
    assert float(report.get("account_equity") or 0) == 0.0
    assert bot.state.fee_taker == 0.0004
    assert bot.state.fee_maker == 0.00015
    assert report["max_contracts_allowed"] >= 1
    assert bot.rest.calls
    assert all(call in READ_ONLY for call in bot.rest.calls)
    assert report["contract"] == readonly_cfg.contract_code
