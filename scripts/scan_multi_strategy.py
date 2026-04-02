"""6手法 × ターゲット銘柄 横断比較スキャン — Block B.

IS=直近30日 / OOS=その前30日（時系列を守るため OOS が古い方）
判定: IS日次PnL > 0 かつ OOS日次PnL > 0 → Robust

実行:
    cd /root/algo-trading-system
    .venv/bin/python3 scripts/scan_multi_strategy.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import pathlib
from datetime import date

sys.path.insert(0, "/root/algo-trading-system")

import pandas as pd

from backend.backtesting.engine import run_backtest
from backend.lab.runner import fetch_ohlcv, JP_CAPITAL_JPY, MARGIN_RATIO, POSITION_PCT, LOT_SIZE
from backend.strategies.jp_stock.jp_macd_rci import JPMacdRci
from backend.strategies.jp_stock.jp_breakout import JPBreakout
from backend.strategies.jp_stock.jp_scalp import JPScalp
from backend.strategies.jp_stock.jp_momentum_5min import JPMomentum5Min
from backend.strategies.jp_stock.jp_orb import JPOpeningRangeBreakout
from backend.strategies.jp_stock.jp_vwap import JPVwapReversion

TARGETS = [
    ("2413.T", "M3"),
    ("6758.T", "Sony"),
    ("8136.T", "Sanrio"),
    ("3103.T", "Unitika"),
    ("6613.T", "QD Laser"),
]

FETCH_DAYS = 60

OUT_FILE = pathlib.Path("/root/algo-trading-system/data/scan_multi_strategy_result.json")


def _run(strat, df):
    return run_backtest(
        strat, df,
        starting_cash=JP_CAPITAL_JPY * MARGIN_RATIO,
        fee_pct=0.0, position_pct=POSITION_PCT,
        usd_jpy=1.0, lot_size=LOT_SIZE,
        limit_slip_pct=0.003, eod_close_time=(15, 20),
    )


def make_strategies(sym: str, name: str) -> list:
    return [
        ("MacdRci",      JPMacdRci(sym, name, interval="5m")),
        ("Breakout",     JPBreakout(sym, name, interval="5m")),
        ("Scalp",        JPScalp(sym, name, interval="5m")),
        ("Momentum5Min", JPMomentum5Min(sym, name)),
        ("ORB",          JPOpeningRangeBreakout(sym, name)),
        ("VwapReversion",JPVwapReversion(sym, name)),
    ]


async def main():
    print("=== Block B: 6手法 × ターゲット銘柄 横断比較 ===\n")

    dfs: dict[str, pd.DataFrame] = {}
    for sym, name in TARGETS:
        df = await fetch_ohlcv(sym, "5m", FETCH_DAYS)
        dfs[sym] = df
        print(f"  {name}: {len(df)} bars")

    results = {}
    header = f"{'銘柄':8s} {'手法':14s} | {'IS日次':8s} {'IS-PF':5s} | {'OOS日次':8s} {'OOS-PF':5s} | {'判定':6s}"
    print(f"\n{header}")
    print("-" * 70)

    for sym, name in TARGETS:
        df = dfs[sym]
        if df.empty or len(df) < 100:
            print(f"  {name}: データ不足スキップ")
            continue
        split = len(df) // 2
        df_is  = df.iloc[split:]   # 新しい方がIS
        df_oos = df.iloc[:split]   # 古い方がOOS

        results[sym] = {"name": name, "strategies": {}}

        for strat_name, strat in make_strategies(sym, name):
            try:
                r_is  = _run(strat, df_is)
                r_oos = _run(strat, df_oos)
                robust = r_is.daily_pnl_jpy > 0 and r_oos.daily_pnl_jpy > 0
                verdict = "Robust" if robust else ("IS-ok" if r_is.daily_pnl_jpy > 0 else "NG")
                print(
                    f"{name:8s} {strat_name:14s} | "
                    f"{r_is.daily_pnl_jpy:+8,.0f} {r_is.profit_factor:5.2f} | "
                    f"{r_oos.daily_pnl_jpy:+8,.0f} {r_oos.profit_factor:5.2f} | "
                    f"{verdict:6s}"
                )
                results[sym]["strategies"][strat_name] = {
                    "is_daily": round(float(r_is.daily_pnl_jpy), 1),
                    "is_pf": round(float(r_is.profit_factor), 3),
                    "is_trades": int(r_is.num_trades),
                    "oos_daily": round(float(r_oos.daily_pnl_jpy), 1),
                    "oos_pf": round(float(r_oos.profit_factor), 3),
                    "oos_trades": int(r_oos.num_trades),
                    "robust": bool(robust),
                }
            except Exception as e:
                print(f"{name:8s} {strat_name:14s} | ERROR: {e}")

    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps({"date": str(date.today()), "results": results}, ensure_ascii=False, indent=2))
    print(f"\n結果保存: {OUT_FILE}")

    # サマリー: Robust ベスト手法 per 銘柄
    print("\n=== 銘柄別 Robust ベスト手法 ===")
    for sym, name in TARGETS:
        if sym not in results:
            continue
        strats = results[sym]["strategies"]
        robust_list = [(k, v) for k, v in strats.items() if v["robust"]]
        if robust_list:
            best = max(robust_list, key=lambda x: x[1]["is_daily"])
            print(f"  {name}: {best[0]} (IS {best[1]['is_daily']:+,.0f} / OOS {best[1]['oos_daily']:+,.0f})")
        else:
            is_ok = [(k, v) for k, v in strats.items() if v["is_daily"] > 0]
            if is_ok:
                best = max(is_ok, key=lambda x: x[1]["is_daily"])
                print(f"  {name}: {best[0]} [IS-only] (IS {best[1]['is_daily']:+,.0f})")
            else:
                print(f"  {name}: 全手法NG")


if __name__ == "__main__":
    asyncio.run(main())
