"""Shared fixtures. Each test gets its own log directory and does not touch the network."""
import time

from bot.config import Config
from bot.risk import RiskEngine
from bot.state import State


def make(tmp_path, capital=15.0, lev=3, contract_size=100.0, price=0.0151, **kw):
    kw.setdefault("access_key", "")
    kw.setdefault("secret_key", "")
    cfg = Config(capital_usdt=capital, max_leverage=lev, paper=True,
                 contract_code="SUN-USDT", log_dir=str(tmp_path), **kw)
    st = State("SUN-USDT")
    st.contract_size = contract_size
    st.min_volume = 1.0
    st.max_leverage = 20.0
    st.book.bid, st.book.ask, st.book.ts = price * 0.999, price * 1.001, time.time()
    return cfg, st, RiskEngine(cfg, st)
