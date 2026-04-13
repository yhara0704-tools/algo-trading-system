"""MACD×RCI グリッドサーチ + アウトオブサンプル検証.

ロジック:
  1. IS (In-Sample)  : 過去60日の前半30日でグリッドサーチ → ベストパラメータを選択
  2. OOS (Out-of-Sample): 後半30日で同パラメータを検証 → 過学習チェック
  3. IS と OOS の両方でプラスなら「信頼できるエッジ」と判断する

実行:
    cd /root/algo-trading-system
    .venv/bin/python3 scripts/grid_search_macd_rci.py
"""
from __future__ import annotations

import asyncio
import itertools
import pathlib
import sys
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pandas as pd

from backend.backtesting.engine import run_backtest
from backend.lab.runner import fetch_ohlcv, JP_CAPITAL_JPY, MARGIN_RATIO, POSITION_PCT, LOT_SIZE
from backend.strategies.jp_stock.jp_macd_rci import JPMacdRci

# ── 対象銘柄 ─────────────────────────────────────────────────────────────────
TARGETS = [
    ("2413.T", "M3"),
    ("6758.T", "Sony"),
    ("8136.T", "Sanrio"),
    ("3103.T", "Unitika"),
]

# ── グリッド定義 ──────────────────────────────────────────────────────────────
GRID = {
    "tp_pct":        [0.002, 0.003, 0.004, 0.005, 0.006],
    "sl_pct":        [0.001, 0.0015, 0.002, 0.003],
    "rci_min_agree": [1, 2, 3],
    "macd_signal":   [7, 9, 11],
}

IS_DAYS  = 60   # In-Sample: 60日取得
OOS_SPLIT = 30  # 前半30日=IS、後半30日=OOS
MIN_TRADES = 15


@dataclass
class GridResult:
    symbol:        str
    tp_pct:        float
    sl_pct:        float
    rci_min_agree: int
    macd_signal:   int
    # IS成績
    is_trades:     int
    is_win_rate:   float
    is_pf:         float
    is_daily:      float
    is_score:      float
    # OOS成績
    oos_trades:    int
    oos_win_rate:  float
    oos_pf:        float
    oos_daily:     float
    oos_score:     float
    # 総合判定
    robust:        bool   # IS・OOS 両方プラスならTrue


def _backtest(strat, df):
    return run_backtest(
        strat, df,
        starting_cash=JP_CAPITAL_JPY * MARGIN_RATIO,
        fee_pct=0.0,
        position_pct=POSITION_PCT,
        usd_jpy=1.0,
        lot_size=LOT_SIZE,
        limit_slip_pct=0.003,
        eod_close_time=(15, 20),
    )


async def main() -> None:
    print("データ取得中（60日）...")
    dfs: dict[str, pd.DataFrame] = {}
    for sym, name in TARGETS:
        df = await fetch_ohlcv(sym, "5m", IS_DAYS)
        dfs[sym] = df
        print(f"  {name}: {len(df)} bars")

    # IS/OOS 分割（バー数で前半/後半）
    dfs_is:  dict[str, pd.DataFrame] = {}
    dfs_oos: dict[str, pd.DataFrame] = {}
    for sym, df in dfs.items():
        split = len(df) // 2
        dfs_is[sym]  = df.iloc[:split]
        dfs_oos[sym] = df.iloc[split:]

    keys   = list(GRID.keys())
    combos = list(itertools.product(*GRID.values()))
    total  = len(combos) * len(TARGETS)
    print(f"\n{len(combos)} combinations × {len(TARGETS)} symbols = {total} IS backtests\n")

    results: list[GridResult] = []
    done = 0

    for sym, name in TARGETS:
        df_is  = dfs_is[sym]
        df_oos = dfs_oos[sym]

        for combo in combos:
            params = dict(zip(keys, combo))
            if params["sl_pct"] >= params["tp_pct"]:
                done += 1
                continue

            strat = JPMacdRci(sym, name, interval="5m",
                              macd_signal=params["macd_signal"],
                              rci_min_agree=params["rci_min_agree"],
                              tp_pct=params["tp_pct"],
                              sl_pct=params["sl_pct"])

            r_is  = _backtest(strat, df_is)
            r_oos = _backtest(strat, df_oos)
            done += 1

            if r_is.num_trades < MIN_TRADES:
                continue

            results.append(GridResult(
                symbol=name,
                tp_pct=params["tp_pct"], sl_pct=params["sl_pct"],
                rci_min_agree=params["rci_min_agree"], macd_signal=params["macd_signal"],
                is_trades=r_is.num_trades, is_win_rate=r_is.win_rate,
                is_pf=r_is.profit_factor, is_daily=r_is.daily_pnl_jpy,
                is_score=r_is.score,
                oos_trades=r_oos.num_trades, oos_win_rate=r_oos.win_rate,
                oos_pf=r_oos.profit_factor, oos_daily=r_oos.daily_pnl_jpy,
                oos_score=r_oos.score,
                robust=(r_is.daily_pnl_jpy > 0 and r_oos.daily_pnl_jpy > 0),
            ))

            if done % 100 == 0:
                print(f"  {done}/{total} 完了...")

    # IS スコア順
    results.sort(key=lambda x: -x.is_score)

    print(f"\n=== 全{len(results)}件 — IS+OOS 両方プラス（Robust）上位 ===\n")

    robust = [r for r in results if r.robust]
    print(f"Robust（IS・OOS両方プラス）: {len(robust)}/{len(results)} ({len(robust)/len(results)*100:.0f}%)\n")

    header = (f"{'銘柄':7s} {'tp%':5s} {'sl%':5s} {'rci':3s} {'sig':3s} | "
              f"{'IS日次':7s} {'IS-PF':5s} | {'OOS日次':7s} {'OOS-PF':5s} | {'判定':4s}")
    print(header)
    print("-" * 75)
    for r in robust[:20]:
        verdict = "OK" if r.robust else "--"
        print(
            f"{r.symbol:7s} {r.tp_pct*100:4.2f}% {r.sl_pct*100:4.3f}% "
            f"{r.rci_min_agree:3d} {r.macd_signal:3d} | "
            f"{r.is_daily:+7,.0f} {r.is_pf:5.2f} | "
            f"{r.oos_daily:+7,.0f} {r.oos_pf:5.2f} | {verdict}"
        )

    print("\n=== 銘柄別 Robust ベストパラメータ ===")
    for _, name in TARGETS:
        sym_robust = [r for r in robust if r.symbol == name]
        if not sym_robust:
            print(f"\n  {name}: Robustな結果なし（IS単独ベストを参考表示）")
            sym_all = [r for r in results if r.symbol == name]
            if sym_all:
                b = sym_all[0]
                print(f"    IS: tp={b.tp_pct}, sl={b.sl_pct}, rci={b.rci_min_agree}, sig={b.macd_signal}"
                      f" → IS {b.is_daily:+,.0f}JPY / OOS {b.oos_daily:+,.0f}JPY")
            continue
        b = sym_robust[0]
        print(f"\n  {name} [Robust]:")
        print(f"    tp={b.tp_pct}, sl={b.sl_pct}, rci_agree={b.rci_min_agree}, macd_signal={b.macd_signal}")
        print(f"    IS  → trades={b.is_trades},  win={b.is_win_rate:.1f}%,  PF={b.is_pf:.2f}, daily={b.is_daily:+,.0f}JPY")
        print(f"    OOS → trades={b.oos_trades}, win={b.oos_win_rate:.1f}%, PF={b.oos_pf:.2f}, daily={b.oos_daily:+,.0f}JPY")


if __name__ == "__main__":
    asyncio.run(main())
