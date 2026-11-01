"""Wires everything together and runs the bot.

    python -m bot.runner --preflight      checks keys, fees, limits; sends nothing
    python -m bot.runner                  paper mode (default)
    BOT_LIVE=1 python -m bot.runner       live mode
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import time
from pathlib import Path

from .config import Config
from .deadman import DeadMansSwitch
from .executor import Executor
from .feed import MarketFeed, PrivateFeed
from .rest import RestError, SunXRest
from .risk import RiskEngine
from .state import State
from .strategies import load as load_strategy

log = logging.getLogger("bot")


def _fee_value(payload: dict, *keys: str) -> float | None:
    """First present fee. An explicit zero is a real fee, not a missing one."""
    for key in keys:
        if key not in payload:
            continue
        value = payload[key]
        if value is None or value == "":
            continue
        return float(value)
    return None


def _one_way_fee(payload: dict, explicit: tuple[str, ...], open_key: str, close_key: str) -> float | None:
    """One-way fee. The live API sends open_* and close_* instead of taker_fee_rate.

    The rest of the bot multiplies this by two for a round trip, so differing
    open and close rates are stored as their average.
    """
    found = _fee_value(payload, *explicit)
    if found is not None:
        return found
    opened = _fee_value(payload, open_key)
    closed = _fee_value(payload, close_key)
    if opened is None:
        return closed
    if closed is None:
        return opened
    return (opened + closed) / 2


def setup_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(Path(log_dir) / "bot.log", encoding="utf-8")],
    )


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = State(cfg.contract_code)
        self.rest = SunXRest(cfg)
        self.risk = RiskEngine(cfg, self.state)
        self.exec = Executor(cfg, self.state, self.risk, self.rest)
        self.deadman = DeadMansSwitch(cfg, self.rest)
        self.strategy = load_strategy(cfg.strategy, cfg, self.state)
        self.market = MarketFeed(cfg.contract_code, self._on_market)
        self.private = PrivateFeed(cfg, self._on_private)
        self._stop = asyncio.Event()
        self.started_at = time.time()

    # ---------- feed callbacks ----------
    async def _on_market(self, msg: dict) -> None:
        self.state.on_market(msg)

    async def _on_private(self, msg: dict) -> None:
        self.state.on_private(msg)

    # ---------- startup checks ----------
    async def preflight(self) -> dict:
        report: dict = {"mode": "paper" if self.cfg.paper else "LIVE",
                        "contract": self.cfg.contract_code}
        problems = self.cfg.validate()

        info = await self.rest.contract_info(self.cfg.contract_code)
        self.state.contract_size = float(info["contract_size"])
        self.state.price_tick = float(info["price_tick"])
        report["contract_size"] = self.state.contract_size
        report["contract_status"] = info.get("contract_status")
        if info.get("contract_status") != 1:
            problems.append(f"{self.cfg.contract_code} is not currently tradeable "
                            f"(contract_status={info.get('contract_status')}).")

        rl = await self.rest.risk_limit(self.cfg.contract_code)
        if rl:
            self.state.max_leverage = float(rl.get("max_lever") or 0)
            self.state.maintenance_margin_rate = float(rl.get("maintenance_margin_rate") or 0)
            self.state.min_volume = float(rl.get("min_volume") or 1)
            report.update(exchange_max_leverage=self.state.max_leverage,
                          maintenance_margin_rate=self.state.maintenance_margin_rate,
                          min_volume=self.state.min_volume)

        bbo = await self.rest.bbo(self.cfg.contract_code)
        ask = float((bbo.get("ask") or [0])[0] or 0)
        self.state.book.ask = ask
        self.state.book.bid = float((bbo.get("bid") or [0])[0] or 0)
        self.state.book.ts = time.time()
        notional = ask * self.state.contract_size
        report["price"] = ask
        report["notional_per_contract_usdt"] = round(notional, 6)
        report["max_contracts_allowed"] = self.risk.max_contracts()
        if notional > 0:
            report["leverage_needed_for_one_contract"] = round(notional / self.cfg.capital_usdt, 3)
        if self.risk.max_contracts() < 1:
            problems.append(
                f"With {self.cfg.capital_usdt} USDT at {self.cfg.max_leverage}x you cannot afford "
                f"one contract of {self.cfg.contract_code} (1 contract = {notional:.4f} USDT). "
                f"Pick a contract with a smaller notional or raise the limits.")

        if self.cfg.access_key and self.cfg.secret_key:
            try:
                fr = await self.rest.fee_rate(self.cfg.contract_code)
                self.state.fee_taker = _one_way_fee(fr, ("taker_fee_rate", "taker"),
                                                       "open_taker_fee", "close_taker_fee")
                self.state.fee_maker = _one_way_fee(fr, ("maker_fee_rate", "maker"),
                                                       "open_maker_fee", "close_maker_fee")
                report["fee_taker"] = self.state.fee_taker
                report["fee_maker"] = self.state.fee_maker
                thr = self.risk.fee_threshold_pct()
                if thr is not None:
                    report["min_profitable_move_pct"] = round(thr, 4)
            except RestError as exc:
                problems.append(f"Could not read the fee rate: {exc}")
            try:
                bal = await self.rest.account_balance()
                report["account_equity"] = bal.get("equity")
                report["account_available"] = bal.get("available_margin")
            except RestError as exc:
                problems.append(f"Could not read the account balance: {exc}")
        else:
            report["keys"] = "not configured - paper mode only"

        report["problems"] = problems
        report["ready"] = not problems
        return report

    # ---------- main loop ----------
    async def _step(self) -> None:
        if not self.cfg.paper and not self.deadman.armed and self.state.position.is_open:
            self.risk.halt("the dead man's switch is not armed while a position is open")
        reason = self.risk.exit_reason()
        if reason:
            log.info("closing position: %s", reason)
            await self.exec.close_all(note=reason)
            return
        if self.risk.halted:
            return
        intent = self.strategy.on_tick()
        if intent.action in ("buy", "sell"):
            await self.exec.submit(intent.action, intent.volume, intent.order_type,
                                   intent.price, note=intent.reason)
        elif intent.action == "close":
            await self.exec.close_all(note=intent.reason)

    async def _trade_loop(self) -> None:
        await asyncio.wait_for(self.market.connected.wait(), timeout=60)
        log.info("strategy=%s mode=%s contract=%s", self.strategy.name,
                 "paper" if self.cfg.paper else "LIVE", self.cfg.contract_code)
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            await self._step()

    async def _status_loop(self, every: float = 30.0) -> None:
        path = Path(self.cfg.log_dir) / "status.json"
        while not self._stop.is_set():
            snap = {
                **self.state.snapshot(),
                "mode": "paper" if self.cfg.paper else "live",
                "strategy": self.strategy.name,
                "uptime_seconds": round(time.time() - self.started_at),
                "halted": self.risk.halted, "halt_reason": self.risk.halt_reason,
                "orders_sent": self.exec.sent, "orders_rejected": self.exec.rejected,
                "deadman_armed": self.deadman.armed,
                "market_feed_connected": self.market.connected.is_set(),
                "market_reconnects": self.market.reconnects,
                "private_feed_connected": self.private.connected.is_set(),
                "private_authenticated": self.private.authenticated.is_set(),
                "max_contracts_allowed": self.risk.max_contracts(),
            }
            path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("status: pos=%s orders=%s/%s halted=%s realized_today=%.6f",
                     snap["position_contracts"], snap["orders_sent"], snap["orders_rejected"],
                     snap["halted"], snap["realized_today"])
            await asyncio.sleep(every)

    async def run(self) -> int:
        report = await self.preflight()
        log.info("preflight:\n%s", json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ready"]:
            log.error("preflight failed; not trading. Fix the problems above.")
            await self.rest.aclose()
            return 1

        tasks = [asyncio.create_task(self.market.run(), name="market"),
                 asyncio.create_task(self._status_loop(), name="status"),
                 asyncio.create_task(self._trade_loop(), name="trade")]
        if self.cfg.access_key and self.cfg.secret_key:
            tasks.append(asyncio.create_task(self.private.run(), name="private"))
        if not self.cfg.paper:
            tasks.append(asyncio.create_task(self.deadman.run(), name="deadman"))

        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(s, self._stop.set)

        await self._stop.wait()
        log.info("shutting down")
        self.market.stop(); self.private.stop(); self.deadman.stop()
        if not self.cfg.paper:
            await self.exec.cancel_all(note="shutdown")
            await self.deadman.disarm()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.rest.aclose()
        return 0


async def amain() -> int:
    ap = argparse.ArgumentParser(description="SunX trading bot")
    ap.add_argument("--preflight", action="store_true",
                    help="run the startup checks and exit without trading")
    ap.add_argument("--seconds", type=float, default=0,
                    help="stop automatically after this many seconds (useful for a paper test)")
    args = ap.parse_args()

    cfg = Config()
    setup_logging(cfg.log_dir)
    bot = Bot(cfg)
    if args.preflight:
        print(json.dumps(await bot.preflight(), ensure_ascii=False, indent=2))
        await bot.rest.aclose()
        return 0
    if args.seconds:
        asyncio.get_running_loop().call_later(args.seconds, bot._stop.set)
    return await bot.run()


if __name__ == "__main__":
    import sys
    raise SystemExit(asyncio.run(amain()))
