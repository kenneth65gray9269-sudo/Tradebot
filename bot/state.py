"""Live state of the market and of the account, fed by the WebSocket streams."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Book:
    bid: float = 0.0
    bid_size: float = 0.0
    ask: float = 0.0
    ask_size: float = 0.0
    ts: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid and self.ask else 0.0

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid * 100 if self.mid else 0.0

    @property
    def fresh(self) -> bool:
        return self.ts > 0 and (time.time() - self.ts) < 10


@dataclass
class Position:
    contract_code: str = ""
    position_side: str = ""
    volume: float = 0.0          # contracts
    entry_price: float = 0.0
    leverage: float = 0.0
    unrealized: float = 0.0
    opened_at: float = 0.0
    direction: str = ""          # "buy" long, "sell" short; used when position_side is "both"

    @property
    def is_open(self) -> bool:
        return self.volume > 0

    @property
    def is_long(self) -> bool:
        """True for a long. One-way mode reports position_side 'both' plus direction."""
        side = (self.position_side or "").lower()
        direction = (self.direction or "").lower()
        if side == "long":
            return True
        if side == "short":
            return False
        if direction == "sell":
            return False
        return True


def _first_float(*values) -> float:
    """First value that was actually sent. Zero is kept; missing keys are skipped."""
    for value in values:
        if value is None or value == "":
            continue
        return float(value)
    return 0.0


class State:
    def __init__(self, contract_code: str, trade_history: int = 600):
        self.contract_code = contract_code
        self.book = Book()
        self.trades: deque = deque(maxlen=trade_history)
        self.position = Position(contract_code=contract_code)
        self.open_orders: dict[str, dict] = {}       # client_order_id -> order
        self.equity: float = 0.0
        self.available: float = 0.0
        self.realized_today: float = 0.0
        self.day = time.strftime("%Y-%m-%d", time.gmtime())
        self.fee_taker: float | None = None
        self.fee_maker: float | None = None
        self.contract_size: float = 0.0
        self.price_tick: float = 0.0
        self.min_volume: float = 1.0
        self.max_leverage: float = 1.0
        self.maintenance_margin_rate: float = 0.0

    # ---------- market ----------
    def on_market(self, msg: dict) -> None:
        ch = msg.get("ch", "")
        tick = msg.get("tick") or {}
        if ch.endswith(".bbo"):
            bid, ask = tick.get("bid") or [0, 0], tick.get("ask") or [0, 0]
            self.book = Book(bid=float(bid[0]), bid_size=float(bid[1] if len(bid) > 1 else 0),
                             ask=float(ask[0]), ask_size=float(ask[1] if len(ask) > 1 else 0),
                             ts=time.time())
        elif ch.endswith(".trade.detail"):
            for t in tick.get("data", []) or []:
                self.trades.append({"price": float(t.get("price", 0)),
                                    "amount": float(t.get("amount", 0)),
                                    "direction": t.get("direction"),
                                    "ts": float(t.get("ts", 0))})

    # ---------- private ----------
    def on_private(self, msg: dict) -> None:
        topic = (msg.get("topic") or "").split(".")[0]
        if topic == "positions":
            for p in msg.get("data", []) or []:
                if p.get("contract_code") != self.contract_code:
                    continue
                vol = float(p.get("volume") or 0)
                was_open = self.position.is_open
                self.position = Position(
                    contract_code=self.contract_code,
                    position_side=p.get("position_side") or "",
                    direction=str(p.get("direction") or ""),
                    volume=vol,
                    entry_price=_first_float(p.get("open_avg_price"), p.get("cost_open"),
                                             p.get("entry_price")),
                    leverage=float(p.get("lever_rate") or 0),
                    unrealized=float(p.get("profit_unreal") or 0),
                    opened_at=self.position.opened_at if was_open and vol > 0 else time.time(),
                )
        elif topic == "orders":
            for o in ([msg.get("data")] if isinstance(msg.get("data"), dict) else msg.get("data") or []):
                if not isinstance(o, dict):
                    continue
                coid = str(o.get("client_order_id") or o.get("order_id") or "")
                status = str(o.get("status") or "")
                if status in ("6", "7", "11", "filled", "canceled", "cancelled"):
                    self.open_orders.pop(coid, None)
                    pnl = o.get("profit") or o.get("real_profit")
                    if pnl is not None:
                        self.add_realized(float(pnl))
                elif coid:
                    self.open_orders[coid] = o
        elif topic in ("accounts", "account"):
            for a in ([msg.get("data")] if isinstance(msg.get("data"), dict) else msg.get("data") or []):
                if isinstance(a, dict):
                    self.equity = float(a.get("equity") or self.equity)
                    self.available = float(a.get("available_margin") or a.get("available") or self.available)

    def add_realized(self, pnl: float) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self.day:
            self.day, self.realized_today = today, 0.0
        self.realized_today += pnl

    def snapshot(self) -> dict:
        return {
            "contract": self.contract_code,
            "bid": self.book.bid, "ask": self.book.ask, "mid": round(self.book.mid, 8),
            "spread_pct": round(self.book.spread_pct, 4), "book_fresh": self.book.fresh,
            "position_contracts": self.position.volume,
            "position_side": self.position.position_side,
            "entry_price": self.position.entry_price,
            "unrealized": self.position.unrealized,
            "open_orders": len(self.open_orders),
            "equity": self.equity, "available": self.available,
            "realized_today": round(self.realized_today, 6),
            "fee_taker": self.fee_taker, "fee_maker": self.fee_maker,
        }
