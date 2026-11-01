"""Order execution: paper mode and live mode behind the same interface.

Every order carries a client_order_id so a retry after a network error cannot
create a second position (the docs let the client set and keep that id).
"""
from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

from .config import Config
from .rest import RestError, SunXRest
from .risk import RiskEngine
from .state import State

log = logging.getLogger("bot.exec")


class Executor:
    def __init__(self, cfg: Config, state: State, risk: RiskEngine, rest: SunXRest):
        self.cfg, self.state, self.risk, self.rest = cfg, state, risk, rest
        self._seq = 0
        self.sent = 0
        self.rejected = 0
        Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
        self._journal = Path(cfg.log_dir) / "orders.jsonl"

    def _next_client_order_id(self) -> str:
        # Integer in [1, 9223372036854775807]. One million distinct ids per millisecond.
        self._seq += 1
        limit = 9223372036854775807
        n = int(time.time() * 1000) * 1_000_000 + (self._seq % 1_000_000)
        if n < 1 or n > limit:
            n = (self._seq % limit) or 1
        return str(n)

    def _write(self, record: dict) -> None:
        record["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record["mode"] = "paper" if self.cfg.paper else "live"
        with self._journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _whole_volume(self, volume: float) -> int:
        raw = float(volume)
        if not math.isfinite(raw) or not raw.is_integer() or int(raw) < 1:
            raise ValueError(f"volume {volume} must be a positive whole number of contracts")
        return int(raw)

    def _pnl(self, fill: float, volume: float) -> float:
        p = self.state.position
        move = (fill - p.entry_price) if p.is_long else (p.entry_price - fill)
        return move * volume * self.state.contract_size

    def _flatten(self) -> None:
        p = self.state.position
        p.volume = 0.0
        p.entry_price = 0.0
        p.unrealized = 0.0
        p.opened_at = 0.0
        p.direction = ""
        p.position_side = ""
        p.leverage = 0.0

    def _apply_paper_fill(self, side: str, volume: int, fill: float, reduce_only: bool) -> None:
        """Mirror a paper fill onto the position so stops and caps can fire."""
        p = self.state.position
        if not p.is_open:
            if reduce_only:
                return
            p.contract_code = self.cfg.contract_code
            p.position_side = "both"
            p.direction = "buy" if side == "buy" else "sell"
            p.volume = float(volume)
            p.entry_price = fill
            p.opened_at = time.time()
            p.unrealized = 0.0
            return

        adding = (p.is_long and side == "buy") or ((not p.is_long) and side == "sell")
        if adding and not reduce_only:
            new_vol = p.volume + volume
            p.entry_price = (p.entry_price * p.volume + fill * volume) / new_vol
            p.volume = new_vol
            return

        closing = min(float(volume), p.volume)
        self.state.add_realized(self._pnl(fill, closing))
        left = p.volume - closing
        extra = float(volume) - closing
        if left > 0:
            p.volume = left
            return
        if extra > 0 and not reduce_only:
            p.contract_code = self.cfg.contract_code
            p.position_side = "both"
            p.direction = "buy" if side == "buy" else "sell"
            p.volume = extra
            p.entry_price = fill
            p.opened_at = time.time()
            p.unrealized = 0.0
            return
        self._flatten()

    def build_order(self, side: str, volume: float, order_type: str = "market",
                    price: float | None = None, reduce_only: bool = False) -> dict:
        contracts = self._whole_volume(volume)
        order: dict = {
            "contract_code": self.cfg.contract_code,
            "margin_mode": self.cfg.margin_mode,
            "position_side": "both",
            "side": side,
            "type": order_type,
            "volume": str(contracts),
            "client_order_id": self._next_client_order_id(),
        }
        if order_type != "market":
            if price is None:
                raise ValueError("a limit order needs a price")
            order["price"] = str(price)
            order["time_in_force"] = "gtc"
        if reduce_only:
            order["reduce_only"] = 1
        return order

    async def submit(self, side: str, volume: float, order_type: str = "market",
                     price: float | None = None, reduce_only: bool = False,
                     note: str = "") -> dict:
        try:
            contracts = self._whole_volume(volume)
        except (TypeError, ValueError) as exc:
            self.rejected += 1
            self._write({"event": "rejected", "reason": str(exc),
                         "order": {"side": side, "volume": volume}, "note": note})
            log.warning("order rejected: %s", exc)
            return {"ok": False, "reason": str(exc)}
        order = self.build_order(side, contracts, order_type, price, reduce_only)
        decision = self.risk.check({**order, "volume": contracts, "reduce_only": reduce_only})
        if not decision:
            self.rejected += 1
            self._write({"event": "rejected", "reason": decision.reason, "order": order, "note": note})
            log.warning("order rejected: %s", decision.reason)
            return {"ok": False, "reason": decision.reason, "order": order}

        self.risk.record(order)
        self.sent += 1
        if self.cfg.paper:
            fill = self.state.book.ask if side == "buy" else self.state.book.bid
            self._apply_paper_fill(side, contracts, fill, reduce_only)
            self._write({"event": "paper_fill", "order": order, "fill_price": fill, "note": note})
            log.info("PAPER %s %s @ %s (%s)", side, order["volume"], fill, note)
            return {"ok": True, "paper": True, "order": order, "fill_price": fill}

        try:
            data = await self.rest.place_order(order)
            self._write({"event": "sent", "order": order, "response": data, "note": note})
            log.info("LIVE %s %s -> order_id=%s (%s)", side, order["volume"],
                     data.get("order_id"), note)
            return {"ok": True, "order": order, "response": data}
        except RestError as exc:
            self._write({"event": "failed", "order": order, "error": str(exc),
                         "payload": exc.payload, "note": note})
            log.error("order failed: %s", exc)
            return {"ok": False, "reason": str(exc), "order": order}

    async def close_all(self, note: str = "") -> dict:
        if self.cfg.paper:
            p = self.state.position
            if p.is_open and p.entry_price:
                fill = self.state.book.bid if p.is_long else self.state.book.ask
                if not fill:
                    fill = self.state.book.mid or p.entry_price
                self.state.add_realized(self._pnl(fill, p.volume))
            self._flatten()
            self._write({"event": "paper_close_all", "note": note})
            log.info("PAPER close all (%s)", note)
            return {"ok": True, "paper": True}
        try:
            r = await self.rest.close_position_market(self.cfg.contract_code)
            self._write({"event": "close_all", "response": r, "note": note})
            return {"ok": True, "response": r}
        except RestError as exc:
            self._write({"event": "close_all_failed", "error": str(exc), "note": note})
            log.error("close all failed: %s", exc)
            return {"ok": False, "reason": str(exc)}

    async def cancel_all(self, note: str = "") -> dict:
        if self.cfg.paper:
            self._write({"event": "paper_cancel_all", "note": note})
            return {"ok": True, "paper": True}
        try:
            r = await self.rest.cancel_all(self.cfg.contract_code)
            self._write({"event": "cancel_all", "response": r, "note": note})
            return {"ok": True, "response": r}
        except RestError as exc:
            self._write({"event": "cancel_all_failed", "error": str(exc), "note": note})
            log.error("cancel all failed: %s", exc)
            return {"ok": False, "reason": str(exc)}
