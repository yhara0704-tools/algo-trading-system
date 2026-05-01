#!/usr/bin/env python3
"""D8d: 6752.T (Panasonic) に多戦略追加検証.

6752.T MacdRci は実測 +8,348 円/日 で current universe 単独最強。
この銘柄に BBShort / Pullback を追加できれば、6752.T 単独で更に押し上げられる。
MicroScalp は別途 D3 で per-symbol 1m 検証済みなので oos_daily を確認。

検証戦略:
  1. BbShort (BB upper touch short)  - 60d 5m
  2. Pullback (EMA trend pullback)   - 60d 5m
  3. Breakout                         - 60d 5m
  4. EnhancedMacdRci                  - 60d 5m

判定基準:
  - 60d 実測 PnL/日 >= 200 円/日
  - WR >= 40%
  - PF >= 1.1
  - n_trades >= 8

満たせば universe 追加候補。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.backtesting.engine import run_backtest
from backend.backtesting.strategy_factory import create as create_strategy

JST = timezone(timedelta(hours=9))

SYMBOL = "6752.T"

# 各戦略の主要パラメータ (D4 default-relaxed と同等の設定)
STRATEGY_CONFIGS = {
    "BbShort": {
        "interval": "5m",
        "bb_period": 20,
        "bb_std": 3.0,
        "tp_pct": 0.005,
        "sl_pct": 0.012,
        "exit_after_bars": 30,
    },
    "Pullback": {
        "interval": "5m",
        "ema_fast": 20,
        "ema_slow": 50,
        "tp_pct": 0.012,
        "sl_pct": 0.008,
        "pullback_pct": 0.003,
    },
    "Breakout": {
        "interval": "5m",
        "lookback_bars": 20,
        "tp_pct": 0.015,
        "sl_pct": 0.008,
    },
    "EnhancedMacdRci": {
        "interval": "5m",
    },
}


def fetch_5m(symbol: str) -> pd.DataFrame:
    end = datetime.now(JST) + timedelta(days=1)
    start = end - timedelta(days=59)
    df = yf.download(symbol, start=start, end=end, interval="5m",
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert("Asia/Tokyo")
    else:
        df.index = df.index.tz_convert("Asia/Tokyo")
    df = df[df.index.map(lambda t: 9 <= t.hour < 15 or (t.hour == 15 and t.minute < 30))]
    return df


def evaluate(df, strat_name, params):
    try:
        strat = create_strategy(strat_name, SYMBOL, params=params)
        result = run_backtest(strat, df, starting_cash=990_000, fee_pct=0.0,
                              position_pct=1.0, usd_jpy=1.0, lot_size=100,
                              limit_slip_pct=0.0008, eod_close_time=(15, 25))
    except Exception as e:
        return {"error": str(e)}
    trades = result.trades
    if not trades:
        return {"n": 0, "wr": 0, "pf": 0, "pnl": 0}
    wins = [t for t in trades if t.pnl > 0]
    gw = sum(t.pnl for t in wins)
    gl = abs(sum(t.pnl for t in trades if t.pnl <= 0))
    wr = len(wins) / len(trades) * 100
    pf = gw / gl if gl > 0 else 0
    total = sum(t.pnl for t in trades)
    return {"n": len(trades), "wr": round(wr, 1), "pf": round(pf, 2),
            "pnl": round(total, 0)}


def main() -> None:
    print(f"=== D8d: {SYMBOL} 多戦略追加検証 (60d 5m) ===\n")
    df = fetch_5m(SYMBOL)
    if df.empty:
        print(f"  no data")
        return
    n_days = len(set(df.index.date))
    print(f"data: {len(df)} bars / {n_days} days\n")

    results = {}
    for strat, params in STRATEGY_CONFIGS.items():
        print(f"--- {strat} ---")
        r = evaluate(df, strat, params)
        if "error" in r:
            print(f"  error: {r['error']}")
            continue
        if r["n"] == 0:
            print(f"  no trades")
            results[strat] = {"n": 0, "verdict": "NO_TRADES"}
            continue
        pnl_per_day = r["pnl"] / n_days
        verdict = "OK" if (pnl_per_day >= 200 and r["wr"] >= 40 and r["pf"] >= 1.1 and r["n"] >= 8) \
                  else "LOW_PERFORMANCE"
        print(f"  n={r['n']} wr={r['wr']:.1f}% pf={r['pf']:.2f} "
              f"pnl={r['pnl']:+.0f} pnl/d={pnl_per_day:+.0f} → {verdict}")
        results[strat] = {
            "n_trades": r["n"], "wr": r["wr"], "pf": r["pf"],
            "total_pnl": r["pnl"], "pnl_per_day": round(pnl_per_day, 0),
            "verdict": verdict,
        }

    # ── 推奨候補 ──
    print(f"\n=== universe 追加推奨 ===\n")
    add_list = []
    for strat, r in results.items():
        if r.get("verdict") == "OK":
            print(f"  {strat}: pnl/d={r['pnl_per_day']:+.0f} 円/日 (wr={r['wr']:.0f}%, pf={r['pf']:.2f})")
            add_list.append({"symbol": SYMBOL, "strategy": strat,
                            "params": STRATEGY_CONFIGS[strat],
                            "expected_value_per_day": r["pnl_per_day"]})
    if not add_list:
        print(f"  なし (どの戦略も基準未達)")

    Path("data/d8_6752_multi_strategy_test.json").write_text(
        json.dumps({"generated_at": datetime.now(JST).isoformat(),
                    "symbol": SYMBOL, "n_days": n_days, "results": results,
                    "add_candidates": add_list},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: data/d8_6752_multi_strategy_test.json")


if __name__ == "__main__":
    main()
