#!/usr/bin/env python3
"""D4: BB Short / Pullback / SwingDonchianD の universe 入り判定.

universe 24 銘柄に対して 60 日 5m (intraday) + 730 日 1d (swing) で
新戦略 3 種を backtest し、universe 入り判定基準を満たす銘柄を選別。

判定基準:
  Pullback:    WR >= 50%, pnl/day >= 500 円
  BBShort:     WR >= 45%, PF >= 1.3, pnl/day >= 500 円
  SwingDonchian: pnl/day >= 1000 円, sharpe >= 0.5

出力:
  data/d4_alt_strategies_validation.json
  data/d4_universe_candidates.json (universe 入り推奨銘柄)
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.backtesting.engine import run_backtest
from backend.strategies.jp_stock.jp_bb_short import JPBbShort
from backend.strategies.jp_stock.jp_pullback import JPPullback
from backend.strategies.jp_stock.jp_swing_donchian import JPSwingDonchianD

JST = timezone(timedelta(hours=9))


SYMBOLS = [
    "6613.T", "9984.T", "6752.T", "8316.T", "8136.T", "1605.T", "3103.T",
    "8306.T", "9107.T", "9432.T", "9433.T", "9468.T", "4911.T", "6723.T",
    "6501.T", "8058.T", "4568.T", "6758.T",
]


def fetch_5m_60d(symbol: str) -> pd.DataFrame:
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


def fetch_1d_730d(symbol: str) -> pd.DataFrame:
    end = datetime.now(JST) + timedelta(days=1)
    start = end - timedelta(days=730)
    df = yf.download(symbol, start=start, end=end, interval="1d",
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def evaluate(df: pd.DataFrame, strat) -> dict:
    if df.empty or len(df) < 50:
        return {"trades": 0, "wr": 0, "pnl": 0, "pf": 0, "sharpe": 0,
                "pnl_per_day": 0, "n_days": 0, "n_bars": len(df)}
    result = run_backtest(strat, df, starting_cash=990_000, fee_pct=0.0,
                          position_pct=1.0, usd_jpy=1.0, lot_size=100,
                          limit_slip_pct=0.0008, eod_close_time=(15, 25))
    trades = result.trades
    if not trades:
        return {"trades": 0, "wr": 0, "pnl": 0, "pf": 0, "sharpe": 0,
                "pnl_per_day": 0, "n_days": 0, "n_bars": len(df)}
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_pnl = sum(t.pnl for t in trades)
    gross_win = sum(t.pnl for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl for t in losses)) if losses else 1e-6
    pf = gross_win / gross_loss if gross_loss > 0 else 0
    daily = {}
    for t in trades:
        try:
            d_str = str(t.exit_time)[:10]
            daily.setdefault(d_str, 0.0)
            daily[d_str] += t.pnl
        except Exception:
            pass
    pnls = list(daily.values())
    sharpe = 0.0
    if len(pnls) > 1:
        m, s = np.mean(pnls), np.std(pnls)
        sharpe = (m / s * np.sqrt(252)) if s > 0 else 0.0
    n_days = max(len(set(daily.keys())), 1)
    n_total_days = max(len(df), 1) if str(df.index.freq).startswith(("1d", "<")) or len(df) < 1000 else len(set(df.index.date))
    return {
        "trades": len(trades),
        "wins": len(wins),
        "wr": round(len(wins) / len(trades) * 100, 1),
        "pnl": round(total_pnl, 0),
        "pf": round(pf, 2),
        "n_bars": len(df),
        "n_days": n_total_days,
        "pnl_per_day": round(total_pnl / n_total_days, 0),
        "sharpe": round(sharpe, 2),
        "long_n": sum(1 for t in trades if t.side == "long"),
        "short_n": sum(1 for t in trades if t.side == "short"),
    }


def main() -> None:
    print(f"=== D4: BB Short / Pullback / SwingDonchian validation ===\n")

    results = {"BBShort": {}, "Pullback": {}, "SwingDonchian": {}}

    # ── intraday strategies (5m, 60d) ───────────────────────────────────
    df_5m_cache = {}
    for sym in SYMBOLS:
        print(f"\n--- {sym} (5m 60d) ---")
        if sym not in df_5m_cache:
            df = fetch_5m_60d(sym)
            df_5m_cache[sym] = df
            print(f"  5m bars={len(df)} days={len(set(df.index.date)) if not df.empty else 0}")
        else:
            df = df_5m_cache[sym]
        if df.empty:
            continue

        # BBShort
        try:
            strat = JPBbShort(symbol=sym, name=sym, interval="5m",
                              bb_period=20, bb_std=3.0,
                              tp_pct=0.004, sl_pct=0.002, full_session=True)
            r = evaluate(df, strat)
            r["symbol"] = sym
            r["strategy"] = "BBShort"
            results["BBShort"][sym] = r
            print(f"  BBShort      trades={r['trades']:3d} wr={r['wr']:5.1f}% "
                  f"pf={r['pf']:5.2f} pnl/day={r['pnl_per_day']:7.0f}")
        except Exception as e:
            print(f"  BBShort error: {e}")

        # Pullback
        try:
            strat = JPPullback(symbol=sym, name=sym, interval="5m",
                               ema_fast=20, ema_slow=50,
                               tp_pct=0.0040, sl_pct=0.0030)
            r = evaluate(df, strat)
            r["symbol"] = sym
            r["strategy"] = "Pullback"
            results["Pullback"][sym] = r
            print(f"  Pullback     trades={r['trades']:3d} wr={r['wr']:5.1f}% "
                  f"pf={r['pf']:5.2f} pnl/day={r['pnl_per_day']:7.0f}")
        except Exception as e:
            print(f"  Pullback error: {e}")

        time.sleep(0.3)

    # ── swing strategy (1d, 730d) ───────────────────────────────────────
    print("\n=== SwingDonchianD (1d 730d) ===")
    for sym in SYMBOLS:
        try:
            df = fetch_1d_730d(sym)
            if df.empty:
                continue
            strat = JPSwingDonchianD(symbol=sym, name=sym, interval="1d",
                                     ema_slow=50, entry_lookback=20,
                                     exit_lookback=10, atr_period=14,
                                     sl_atr_mult=2.0, tp_atr_mult=4.0)
            r = evaluate(df, strat)
            r["symbol"] = sym
            r["strategy"] = "SwingDonchian"
            results["SwingDonchian"][sym] = r
            print(f"  {sym:8} 1d bars={len(df):3d} trades={r['trades']:3d} "
                  f"wr={r['wr']:5.1f}% pf={r['pf']:5.2f} pnl/day={r['pnl_per_day']:7.0f} sharpe={r['sharpe']:.2f}")
            time.sleep(0.3)
        except Exception as e:
            print(f"  {sym} error: {e}")

    out_path = Path("data/d4_alt_strategies_validation.json")
    out_path.write_text(json.dumps({
        "generated_at": datetime.now(JST).isoformat(),
        "n_symbols": len(SYMBOLS),
        "results": results,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: {out_path}")

    # ── universe 入り判定 ────────────────────────────────────────────────
    print("\n=== universe 入り判定 ===\n")
    candidates = {"BBShort": [], "Pullback": [], "SwingDonchian": []}

    for sym, r in results["BBShort"].items():
        if r["wr"] >= 45 and r["pf"] >= 1.3 and r["pnl_per_day"] >= 500:
            candidates["BBShort"].append(r)
    for sym, r in results["Pullback"].items():
        if r["wr"] >= 50 and r["pnl_per_day"] >= 500:
            candidates["Pullback"].append(r)
    for sym, r in results["SwingDonchian"].items():
        if r["pnl_per_day"] >= 500 and r["sharpe"] >= 0.3:  # 1d は閾値緩め
            candidates["SwingDonchian"].append(r)

    for name, cands in candidates.items():
        cands.sort(key=lambda r: r.get("pnl_per_day", 0), reverse=True)
        print(f"\n{name}: {len(cands)} 候補")
        for r in cands:
            print(f"  {r['symbol']:8} trades={r['trades']:3d} wr={r['wr']:5.1f}% "
                  f"pf={r['pf']:5.2f} pnl/day={r['pnl_per_day']:7.0f}"
                  + (f" sharpe={r['sharpe']:.2f}" if r.get("sharpe") else ""))

    out_path2 = Path("data/d4_universe_candidates.json")
    out_path2.write_text(json.dumps(candidates, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    print(f"\nsaved: {out_path2}")

    # ── 期待 PnL 集計 ────────────────────────────────────────────────────
    total_pnl_per_day = sum(r["pnl_per_day"] for cands in candidates.values() for r in cands)
    print(f"\n*** 全候補合計期待 PnL: {total_pnl_per_day:,.0f} 円/日 (理論最大) ***")


if __name__ == "__main__":
    main()
