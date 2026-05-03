#!/usr/bin/env python3
"""N5: 独立バックテスト PnL × lot_multiplier 集計で真の理論複利値を試算.

`portfolio_sim.simulate` は lot_multiplier を読まない古いロジック。
そのため N4 の期待値駆動配分の効果は portfolio_sim では測れない。

このスクリプトは:
  1. 各 active entry を独立に 14 日 5m (or 30 日 1m) で個別バックテスト
  2. 個別 PnL を計算
  3. lot_multiplier を掛けて重みづけ集計
  4. 余力競合は係数 0.6-0.8 で割引 (経験則)

これは「上限見積り」 = 余力競合無視の理論最大値だが、
portfolio_sim の機械分散下振れと組み合わせて Phase D gate
+200,000 円 / 10 営業日の達成可能性を判断する材料となる。

出力: data/n5_independent_pnl_estimate.json
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.backtesting.engine import run_backtest  # noqa: E402
from backend.backtesting.strategy_factory import create as create_strategy  # noqa: E402
from backend.backtesting.strategy_factory import resolve_jp_ohlcv_interval  # noqa: E402
from backend.lab.runner import fetch_ohlcv, _fetch_file_cache  # noqa: E402
from backend.lab.runner import JP_CAPITAL_JPY, MARGIN_RATIO  # noqa: E402

JST = timezone(timedelta(hours=9))

BUYING_POWER = JP_CAPITAL_JPY * MARGIN_RATIO  # 990k * 3.3 = 3,267,000
POSITION_PCT_BASE = 0.30  # 1 銘柄 30% (lot_multiplier=1.0 のとき)


async def fetch_data_for(sym: str, iv: str, days: int):
    df = _fetch_file_cache(sym, iv, days)
    if df is None or df.empty:
        df = await fetch_ohlcv(sym, iv, days)
    return df


async def evaluate_one(entry: dict, days: int = 14) -> dict | None:
    sym = entry["symbol"]
    strat = entry["strategy"]
    params = entry.get("params", {}) or {}
    iv = resolve_jp_ohlcv_interval(strat, params)
    days_use = 30 if iv == "1m" else days

    try:
        df = await fetch_data_for(sym, iv, days_use)
    except Exception as e:
        return {"error": f"fetch failed: {e}"}
    if df is None or df.empty:
        return {"error": "no data"}

    n_days = len(set(df.index.date)) if hasattr(df.index, "date") else len(df) // 78
    if n_days < 5:
        return {"error": f"too few days: {n_days}"}

    try:
        s = create_strategy(strat, sym, params=params)
        # 単独バックテスト: 余力 990k * MARGIN_RATIO で 30% を 1 銘柄に投入
        result = run_backtest(
            s, df,
            starting_cash=BUYING_POWER,
            fee_pct=0.0,
            position_pct=POSITION_PCT_BASE,
            usd_jpy=1.0,
            lot_size=100,
            limit_slip_pct=0.003,
            eod_close_time=(15, 25),
        )
    except Exception as e:
        return {"error": f"backtest failed: {e}"}

    trades = result.trades or []
    n = len(trades)
    if n == 0:
        return {
            "symbol": sym, "strategy": strat, "lot_multiplier": entry.get("lot_multiplier", 1.0),
            "n_days": n_days, "n_trades": 0, "total_pnl": 0, "pnl_per_day": 0,
            "wr": 0, "pf": 0,
        }
    wins = [t for t in trades if t.pnl > 0]
    gw = sum(t.pnl for t in wins)
    gl = abs(sum(t.pnl for t in trades if t.pnl <= 0))
    wr = len(wins) / n * 100
    pf = gw / gl if gl > 0 else 0
    total = sum(t.pnl for t in trades)
    pnl_per_day = total / n_days if n_days else 0

    return {
        "symbol": sym, "strategy": strat,
        "lot_multiplier": entry.get("lot_multiplier", 1.0),
        "interval": iv,
        "n_days": n_days, "n_trades": n,
        "total_pnl": round(total, 0), "pnl_per_day": round(pnl_per_day, 0),
        "wr": round(wr, 1), "pf": round(pf, 2),
    }


async def main():
    universe = json.load(open("data/universe_active.json"))
    syms = universe["symbols"]
    active = [s for s in syms
              if not s.get("observation_only", False) or s.get("force_paper", False)]

    print(f"=== N5: 独立 PnL × lot_multiplier 集計 試算 ===\n")
    print(f"active entries: {len(active)}")
    print()

    rows = []
    for entry in active:
        r = await evaluate_one(entry)
        if r is None or "error" in r:
            print(f"  {entry['symbol']:8} {entry['strategy']:18} skip: {r.get('error') if r else 'None'}")
            continue
        rows.append(r)
        print(f"  {r['symbol']:8} {r['strategy']:18} mult={r['lot_multiplier']:>4} "
              f"n_days={r['n_days']:>3} trades={r['n_trades']:>3} "
              f"wr={r['wr']:>5.1f}% pf={r['pf']:>4.2f} "
              f"pnl={r['total_pnl']:>+8.0f} pnl/d={r['pnl_per_day']:>+6.0f}")

    print(f"\n=== 集計 ===\n")

    # 機械分散シナリオ: lot_mult 無視
    raw_total = sum(r["total_pnl"] for r in rows)
    raw_per_day = sum(r["pnl_per_day"] for r in rows)

    # 期待値駆動シナリオ: lot_multiplier 適用 (上限値、余力競合無視)
    weighted_total = sum(r["total_pnl"] * r["lot_multiplier"] for r in rows)
    weighted_per_day = sum(r["pnl_per_day"] * r["lot_multiplier"] for r in rows)

    # 余力競合補正係数: 0.6-0.8 (経験則 = 同時並走 5+ 銘柄での約定機会の制約)
    realistic_low = weighted_per_day * 0.6
    realistic_high = weighted_per_day * 0.8

    n_days_avg = sum(r["n_days"] for r in rows) / len(rows) if rows else 1

    print(f"  独立合計 (mult=1)      : 累計 {raw_total:>+10,.0f} 円 / 平均 {raw_per_day:>+8,.0f} 円/日")
    print(f"  期待値駆動 上限 (mult適用): 累計 {weighted_total:>+10,.0f} 円 / 平均 {weighted_per_day:>+8,.0f} 円/日")
    print(f"  期待値駆動 現実的 (×0.6): {realistic_low:>+8,.0f} 円/日 (lower bound)")
    print(f"  期待値駆動 現実的 (×0.8): {realistic_high:>+8,.0f} 円/日 (upper bound)")
    print()

    # 10 営業日換算
    target_10d = 200_000
    print(f"=== 10 営業日累積 試算 (Phase D gate: +{target_10d:,} 円) ===\n")
    p10_low = realistic_low * 10
    p10_high = realistic_high * 10
    p10_mid = (p10_low + p10_high) / 2
    print(f"  期待値駆動 lower (×0.6): +{p10_low:>10,.0f} 円 ({p10_low / target_10d * 100:.1f}%)")
    print(f"  期待値駆動 mid   (×0.7): +{p10_mid:>10,.0f} 円 ({p10_mid / target_10d * 100:.1f}%)")
    print(f"  期待値駆動 upper (×0.8): +{p10_high:>10,.0f} 円 ({p10_high / target_10d * 100:.1f}%)")
    print()
    print(f"  [参考] portfolio_sim.simulate (機械分散): +118,788 円 / 10 日 = 59.4%")

    out = {
        "generated_at": datetime.now(JST).isoformat(),
        "n_active": len(active),
        "n_evaluated": len(rows),
        "n_days_avg": round(n_days_avg, 1),
        "raw_total": raw_total,
        "raw_per_day": raw_per_day,
        "weighted_total": weighted_total,
        "weighted_per_day": weighted_per_day,
        "realistic_low_per_day": realistic_low,
        "realistic_high_per_day": realistic_high,
        "p10_low": p10_low, "p10_high": p10_high, "p10_mid": p10_mid,
        "phase_d_target": target_10d,
        "rows": rows,
    }
    Path("data/n5_independent_pnl_estimate.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: data/n5_independent_pnl_estimate.json")


if __name__ == "__main__":
    asyncio.run(main())
