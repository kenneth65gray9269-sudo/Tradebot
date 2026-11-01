"""Hard risk engine. Every order passes through check(); a rejection is final.

The strategy cannot widen these limits - it only proposes orders.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque

from .config import Config
from .state import State

log = logging.getLogger("bot.risk")


class Decision:
    def __init__(self, ok: bool, reason: str = ""):
        self.ok, self.reason = ok, reason

    def __bool__(self) -> bool:
        return self.ok


class RiskEngine:
    def __init__(self, cfg: Config, state: State):
        self.cfg, self.state = cfg, state
        self._order_times: deque = deque(maxlen=200)
        self.halted = False
        self.halt_reason = ""

    # ---------- global gates ----------
    def halt(self, reason: str) -> None:
        if not self.halted:
            self.halted = True
            self.halt_reason = reason
            log.error("TRADING HALTED: %s", reason)

    def daily_loss_breached(self) -> bool:
        total = self.state.realized_today + min(0.0, self.state.position.unrealized)
        return total <= -abs(self.cfg.max_daily_loss_usdt)

    def max_contracts(self) -> int:
        """Largest position the limits allow, from notional and leverage."""
        if self.cfg.max_position_contracts > 0:
            return self.cfg.max_position_contracts
        price = self.state.book.mid or self.state.book.ask
        notional_per_contract = price * self.state.contract_size
        if notional_per_contract <= 0:
            return 0
        allowed = min(self.cfg.max_notional_usdt,
                      self.cfg.capital_usdt * self.cfg.max_leverage)
        return int(allowed // notional_per_contract)

    def fee_threshold_pct(self) -> float | None:
        """Round-trip taker fee in percent, times the required edge multiple."""
        if self.state.fee_taker is None:
            return None
        return abs(self.state.fee_taker) * 2 * 100 * self.cfg.min_edge_over_fees

    # ---------- per-order check ----------
    def check(self, intent: dict) -> Decision:
        c, s = self.cfg, self.state
        # Flattening stays possible after a daily-loss halt, a stale book, or the rate limit.
        # A new order still has to pass every gate below.
        if intent.get("reduce_only") or intent.get("close"):
            return Decision(True)

        if self.halted:
            return Decision(False, f"halted: {self.halt_reason}")
        if not s.book.fresh:
            return Decision(False, "order book is stale (no WebSocket update in 10s)")
        if self.daily_loss_breached():
            self.halt(f"daily loss limit {c.max_daily_loss_usdt} USDT reached")
            return Decision(False, self.halt_reason)

        now = time.time()
        while self._order_times and now - self._order_times[0] > 60:
            self._order_times.popleft()
        if len(self._order_times) >= c.max_orders_per_minute:
            return Decision(False, f"rate limit: {c.max_orders_per_minute} orders/minute")

        if len(s.open_orders) >= c.max_open_orders:
            return Decision(False, f"already {len(s.open_orders)} open orders")

        try:
            raw = float(intent.get("volume") or 0)
        except (TypeError, ValueError):
            return Decision(False, "volume is not a number")
        if not math.isfinite(raw) or not raw.is_integer():
            return Decision(False, f"volume {intent.get('volume')} must be a whole number of contracts")
        vol = float(int(raw))
        if vol < s.min_volume:
            return Decision(False, f"volume {vol} below the exchange minimum {s.min_volume}")
        cap = self.max_contracts()
        if cap < 1:
            return Decision(False,
                            f"capital {c.capital_usdt} USDT at {c.max_leverage}x cannot afford one "
                            f"contract of {s.contract_code if hasattr(s,'contract_code') else ''} "
                            f"(1 contract = {(s.book.mid or 0) * s.contract_size:.4f} USDT)")
        if s.position.volume + vol > cap:
            return Decision(False, f"position {s.position.volume}+{vol} exceeds the cap {cap}")

        lev = (s.position.volume + vol) * (s.book.mid * s.contract_size) / max(c.capital_usdt, 1e-9)
        if lev > c.max_leverage + 1e-9:
            return Decision(False, f"effective leverage {lev:.2f}x exceeds {c.max_leverage}x")
        if s.max_leverage and c.max_leverage > s.max_leverage:
            return Decision(False, f"configured leverage {c.max_leverage}x exceeds the "
                                   f"exchange maximum {s.max_leverage}x")
        return Decision(True)

    def record(self, intent: dict) -> None:
        self._order_times.append(time.time())

    # ---------- exits ----------
    def exit_reason(self) -> str | None:
        """Why an open position should be closed now, if it should."""
        s, c = self.state, self.cfg
        p = s.position
        if not p.is_open or not s.book.mid or not p.entry_price:
            return None
        long = p.is_long
        change = (s.book.mid - p.entry_price) / p.entry_price * 100
        move = change if long else -change
        if move <= -abs(c.stop_loss_pct):
            return f"stop loss: {move:.3f}% <= -{c.stop_loss_pct}%"
        if move >= abs(c.take_profit_pct):
            return f"take profit: {move:.3f}% >= {c.take_profit_pct}%"
        if p.opened_at and (time.time() - p.opened_at) > c.max_hold_seconds:
            return f"max hold time {c.max_hold_seconds}s reached"
        if self.daily_loss_breached():
            return "daily loss limit reached"
        return None
