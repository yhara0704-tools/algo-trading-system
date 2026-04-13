"""全58銘柄 × MACD×RCI 一括適性スキャン — Block C.

価格フィルター後の全銘柄にデフォルトパラメータでIS/OOSを走らせ
銘柄適性マップを作成する。グリッドサーチなし（速度優先）。

IS=直近30日 / OOS=その前30日

実行:
    cd /root/algo-trading-system
    .venv/bin/python3 scripts/scan_full_pool.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import pathlib
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pandas as pd

from backend.backtesting.engine import run_backtest
from backend.lab.runner import fetch_ohlcv, JP_CAPITAL_JPY, MARGIN_RATIO, POSITION_PCT, LOT_SIZE, MAX_STOCK_PRICE
from backend.strategies.jp_stock.jp_macd_rci import JPMacdRci
from backend.strategies.jp_stock.pts_screener import PTS_CANDIDATE_POOL

FETCH_DAYS = 60
MIN_BARS   = 80
OUT_FILE   = pathlib.Path(__file__).resolve().parent.parent / "data" / "scan_full_pool_result.json"


def _run(strat, df):
    return run_backtest(
        strat, df,
        starting_cash=JP_CAPITAL_JPY * MARGIN_RATIO,
        fee_pct=0.0, position_pct=POSITION_PCT,
        usd_jpy=1.0, lot_size=LOT_SIZE,
        limit_slip_pct=0.003, eod_close_time=(15, 20),
    )


async def scan_one(sym: str, name: str) -> dict | None:
    try:
        df = await fetch_ohlcv(sym, "5m", FETCH_DAYS)
        if df.empty or len(df) < MIN_BARS:
            return {"symbol": sym, "name": name, "skip": "データ不足"}

        # 最新終値で価格フィルター
        latest_price = float(df["close"].iloc[-1])
        if latest_price > MAX_STOCK_PRICE:
            return {"symbol": sym, "name": name, "skip": f"価格超過 {latest_price:.0f}円 > {MAX_STOCK_PRICE:.0f}円"}

        split  = len(df) // 2
        df_is  = df.iloc[split:]
        df_oos = df.iloc[:split]

        strat = JPMacdRci(sym, name, interval="5m")
        r_is  = _run(strat, df_is)
        r_oos = _run(strat, df_oos)
        robust = bool(r_is.daily_pnl_jpy > 0 and r_oos.daily_pnl_jpy > 0)

        return {
            "symbol": sym, "name": name, "price": round(float(latest_price), 0),
            "is_daily": round(float(r_is.daily_pnl_jpy), 1),
            "is_pf": round(float(r_is.profit_factor), 3),
            "is_trades": int(r_is.num_trades),
            "oos_daily": round(float(r_oos.daily_pnl_jpy), 1),
            "oos_pf": round(float(r_oos.profit_factor), 3),
            "oos_trades": int(r_oos.num_trades),
            "robust": robust,
            "verdict": "Robust" if robust else ("IS-ok" if r_is.daily_pnl_jpy > 0 else "NG"),
        }
    except Exception as e:
        return {"symbol": sym, "name": name, "skip": str(e)}


async def main():
    print(f"=== Block C: 全{len(PTS_CANDIDATE_POOL)}銘柄 × MACD×RCI 適性スキャン ===")
    print(f"価格フィルター: ≤{MAX_STOCK_PRICE:.0f}円\n")

    results = []
    for i, c in enumerate(PTS_CANDIDATE_POOL, 1):
        sym, name = c["symbol"], c["name"]
        print(f"[{i:2d}/{len(PTS_CANDIDATE_POOL)}] {name} ({sym})...", end=" ", flush=True)
        r = await scan_one(sym, name)
        results.append(r)
        if r.get("skip"):
            print(f"スキップ: {r['skip']}")
        else:
            print(f"{r['verdict']:6s}  IS {r['is_daily']:+7,.0f}  OOS {r['oos_daily']:+7,.0f}  trades={r['is_trades']}")

    # 結果保存
    OUT_FILE.parent.mkdir(exist_ok=True)
    def _default(o):
        if isinstance(o, bool):
            return bool(o)
        raise TypeError(f"Object of type {type(o)} is not JSON serializable")
    OUT_FILE.write_text(json.dumps({"date": str(date.today()), "results": results}, ensure_ascii=False, indent=2, default=_default))
    print(f"\n結果保存: {OUT_FILE}")

    # サマリー
    valid = [r for r in results if not r.get("skip")]
    robust_list = [r for r in valid if r.get("robust")]
    is_ok_list  = [r for r in valid if not r.get("robust") and r.get("is_daily", 0) > 0]
    ng_list     = [r for r in valid if r.get("is_daily", 0) <= 0]

    print(f"\n=== サマリー: {len(valid)}銘柄スキャン ===")
    print(f"  Robust (IS+OOS両方プラス): {len(robust_list)}銘柄")
    print(f"  IS-ok  (ISのみプラス):     {len(is_ok_list)}銘柄")
    print(f"  NG     (ISもマイナス):     {len(ng_list)}銘柄")

    if robust_list:
        robust_list.sort(key=lambda x: -x["is_daily"])
        print("\n  Robust 上位10:")
        for r in robust_list[:10]:
            print(f"    {r['name']:14s} ({r['symbol']}) IS {r['is_daily']:+7,.0f} / OOS {r['oos_daily']:+7,.0f}  price={r['price']:.0f}円")

    # 次回グリッドサーチ候補（Robust + IS-ok 上位）
    next_candidates = sorted(
        [r for r in valid if r.get("is_daily", 0) > 0],
        key=lambda x: -x["is_daily"]
    )[:15]
    next_file = pathlib.Path(__file__).resolve().parent.parent / "data" / "next_grid_targets.json"
    next_file.write_text(json.dumps(
        {"date": str(date.today()), "candidates": next_candidates},
        ensure_ascii=False, indent=2
    ))
    print(f"\n次回グリッドサーチ候補 → {next_file}")


if __name__ == "__main__":
    asyncio.run(main())
