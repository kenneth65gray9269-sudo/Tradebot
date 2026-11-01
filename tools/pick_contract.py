"""Rank contracts by what actually matters for a small account: can you afford one
contract, and is the spread small enough to trade through.

    python tools/pick_contract.py --capital 15 --leverage 3
All numbers come from the live public API; nothing is assumed.
"""
from __future__ import annotations

import argparse
import json
import urllib.request

BASE = "https://api.sunx.io"


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=45) as r:
        return json.loads(r.read())


def rank_contracts(contracts: list, ticks: list, *, capital: float, leverage: float,
                   min_contracts: int, max_spread: float) -> list[dict]:
    """Contracts you can afford, tightest spread first. Pure: no network."""
    info = {c["contract_code"]: c for c in contracts}
    budget = capital * leverage
    rows = []
    for t in ticks:
        code = t["contract_code"]
        a, b = t.get("ask"), t.get("bid")
        c = info.get(code)
        if not (a and b and c) or c.get("contract_status") != 1:
            continue
        ask, bid = a[0], b[0]
        mid = (ask + bid) / 2
        if not mid:
            continue
        spread = (ask - bid) / mid * 100
        notional = ask * c["contract_size"]
        if notional <= 0:
            continue
        affordable = int(budget // notional)
        if affordable < min_contracts or spread > max_spread:
            continue
        rows.append({"contract": code, "spread_pct": round(spread, 4),
                     "notional_per_contract": round(notional, 4),
                     "contracts_affordable": affordable,
                     "break_even_move_pct": round(spread, 4)})
    rows.sort(key=lambda r: r["spread_pct"])
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=15.0)
    ap.add_argument("--leverage", type=float, default=3.0)
    ap.add_argument("--min-contracts", type=int, default=5,
                    help="how many contracts you want to be able to hold, for position sizing")
    ap.add_argument("--max-spread", type=float, default=0.25, help="percent")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    contracts = get("/sapi/v1/public/contract_info?business_type=all")["data"]
    ticks = get("/sapi/v1/market/bbo")["ticks"]
    rows = rank_contracts(contracts, ticks, capital=args.capital, leverage=args.leverage,
                          min_contracts=args.min_contracts, max_spread=args.max_spread)
    budget = args.capital * args.leverage
    print(f"capital={args.capital} leverage={args.leverage}x budget={budget} USDT")
    print(f"filters: at least {args.min_contracts} contracts affordable, spread <= {args.max_spread}%\n")
    if not rows:
        print("No contract passes these filters. Raise --max-spread, lower --min-contracts, "
              "or increase the capital.")
        return
    print(f"{'contract':16}{'spread%':>9}{'1 contract $':>14}{'affordable':>12}")
    for r in rows[:args.top]:
        print(f"{r['contract']:16}{r['spread_pct']:9.3f}{r['notional_per_contract']:14.4f}"
              f"{r['contracts_affordable']:12d}")
    print("\nbreak_even_move_pct is the spread alone; add the round-trip fee from your own "
          "fee_rate on top before deciding a profit target.")


if __name__ == "__main__":
    main()
