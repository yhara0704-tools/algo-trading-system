#!/usr/bin/env python3
"""D8a: 全戦略 60日健全性監査 (Day 6 を全戦略に拡大).

universe_active.json の active entry のうち、MacdRci 以外 (Breakout, BBShort,
Pullback, EnhancedMacdRci, Momentum5Min, ORB 等) について 60日 5m で
実測 PnL を取得し、universe oos_daily と比較する。

MicroScalp は 1m のみ動作するため、別途 D3 で per-symbol 30d 1m 検証済み
(信頼できる値) なのでスキップ。SwingDonchianD は 1d なので別扱い。

出力:
  data/d8_all_strategies_health_check.json
  data/d8_unhealthy_candidates.json (demote / lot_mult 縮小候補)
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.backtesting.engine import run_backtest
from backend.backtesting.strategy_factory import create as create_strategy
from backend.backtesting.strategy_factory import resolve_jp_ohlcv_interval

JST = timezone(timedelta(hours=9))

# 監査対象戦略 (5m で動作するもの)
TARGET_STRATEGIES = {"Breakout", "BbShort", "Pullback", "EnhancedMacdRci"}

# 60d 5m データ取得 (D6 と同じ)
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


def evaluate(df: pd.DataFrame, strat_name: str, sym: str, params: dict) -> dict:
    try:
        strat = create_strategy(strat_name, sym, params=params)
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
    long_pnl = sum(t.pnl for t in trades if t.side == "long")
    short_pnl = sum(t.pnl for t in trades if t.side == "short")
    return {"n": len(trades), "wr": round(wr, 1), "pf": round(pf, 2),
            "pnl": round(total, 0),
            "long_pnl": round(long_pnl, 0), "short_pnl": round(short_pnl, 0)}


def main() -> None:
    print(f"=== D8a: 全戦略 60日健全性監査 (5m) ===\n")
    universe = json.load(open("data/universe_active.json"))
    syms = universe["symbols"]
    # active かつ 5m で動く戦略のみ対象
    targets = []
    for s in syms:
        strat = s["strategy"]
        if strat not in TARGET_STRATEGIES:
            continue
        # observation_only でも force_paper があるなら検証
        if s.get("observation_only", False) and not s.get("force_paper", False):
            continue
        # interval が 5m の確認
        iv = resolve_jp_ohlcv_interval(strat, s.get("params") or {})
        if iv != "5m":
            print(f"  skip {s['symbol']} {strat} (interval={iv})")
            continue
        targets.append(s)

    print(f"監査対象: {len(targets)} entries\n")

    results = []
    df_cache = {}
    for s in targets:
        sym = s["symbol"]
        strat = s["strategy"]
        params = s.get("params") or {}
        oos_daily = float(s.get("oos_daily", 0) or 0)
        print(f"--- {sym} {strat} (oos_daily={oos_daily:.0f}) ---")

        if sym not in df_cache:
            df = fetch_5m_60d(sym)
            if df.empty or len(df) < 500:
                print(f"  skip (insufficient data)")
                df_cache[sym] = None
                continue
            df_cache[sym] = df
            time.sleep(0.3)
        df = df_cache[sym]
        if df is None:
            continue
        n_days = len(set(df.index.date))

        r = evaluate(df, strat, sym, params)
        if "error" in r:
            print(f"  error: {r['error']}")
            continue
        if r["n"] == 0:
            print(f"  no trades")
            results.append({
                "symbol": sym, "strategy": strat, "oos_daily": oos_daily,
                "n_days": n_days, "n_trades": 0, "pnl_per_day": 0,
                "real_pnl": 0, "ratio": 0, "status": "NO_TRADES",
            })
            continue
        pnl_per_day = r["pnl"] / n_days
        ratio = pnl_per_day / oos_daily if oos_daily > 0 else 0
        # 判定
        status = "OK"
        if pnl_per_day < 0:
            status = "UNHEALTHY"
        elif oos_daily > 200 and ratio < 0.3:
            status = "OVERESTIMATE"
        elif oos_daily > 0 and ratio > 2.0:
            status = "UNDERESTIMATE"  # 上振れ
        print(f"  n={r['n']:3d} wr={r['wr']:5.1f}% pf={r['pf']:5.2f} "
              f"pnl={r['pnl']:+8.0f} pnl/d={pnl_per_day:+7.0f} "
              f"ratio={ratio:>5.2f} [{status}]")

        results.append({
            "symbol": sym, "strategy": strat, "oos_daily": oos_daily,
            "n_days": n_days, "n_trades": r["n"],
            "wr": r["wr"], "pf": r["pf"], "total_pnl": r["pnl"],
            "long_pnl": r.get("long_pnl", 0), "short_pnl": r.get("short_pnl", 0),
            "pnl_per_day": round(pnl_per_day, 0), "ratio": round(ratio, 2),
            "status": status,
        })

    # ── 集計 ──
    print(f"\n=== 戦略別集計 ===\n")
    by_strat = {}
    for r in results:
        by_strat.setdefault(r["strategy"], []).append(r)

    print(f"{'strategy':18} {'n':>3} {'real':>8} {'oos_sum':>8} {'ratio':>6}")
    for strat, rows in by_strat.items():
        real_sum = sum(r.get("pnl_per_day", 0) for r in rows)
        oos_sum = sum(r.get("oos_daily", 0) for r in rows)
        ratio = real_sum / oos_sum if oos_sum > 0 else 0
        print(f"  {strat:16} {len(rows):>3} {real_sum:>+8.0f} {oos_sum:>+8.0f} {ratio:>6.2f}")

    # ── UNHEALTHY 候補 ──
    unhealthy = [r for r in results if r["status"] == "UNHEALTHY"]
    overest = [r for r in results if r["status"] == "OVERESTIMATE"]
    underest = [r for r in results if r["status"] == "UNDERESTIMATE"]

    print(f"\n=== UNHEALTHY (実測 PnL/日 < 0、demote 推奨) ===")
    for u in unhealthy:
        print(f"  {u['symbol']:8} {u['strategy']:16} pnl/d={u['pnl_per_day']:+.0f} "
              f"oos_daily={u['oos_daily']:+.0f}")

    print(f"\n=== OVERESTIMATE (実測 < oos の 30%) ===")
    for o in overest:
        print(f"  {o['symbol']:8} {o['strategy']:16} pnl/d={o['pnl_per_day']:+.0f} / "
              f"oos={o['oos_daily']:+.0f} ratio={o['ratio']:.2f}")

    print(f"\n=== UNDERESTIMATE (実測 > oos の 200%、上振れ) ===")
    for u in underest:
        print(f"  {u['symbol']:8} {u['strategy']:16} pnl/d={u['pnl_per_day']:+.0f} / "
              f"oos={u['oos_daily']:+.0f} ratio={u['ratio']:.2f}")

    Path("data/d8_all_strategies_health_check.json").write_text(
        json.dumps({"generated_at": datetime.now(JST).isoformat(),
                    "results": results,
                    "n_unhealthy": len(unhealthy),
                    "n_overestimate": len(overest),
                    "n_underestimate": len(underest)},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    Path("data/d8_unhealthy_candidates.json").write_text(
        json.dumps({"unhealthy": unhealthy, "overestimate": overest,
                    "underestimate": underest},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: data/d8_all_strategies_health_check.json")
    print(f"saved: data/d8_unhealthy_candidates.json")


if __name__ == "__main__":
    main()
