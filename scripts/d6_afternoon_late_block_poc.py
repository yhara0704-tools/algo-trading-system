#!/usr/bin/env python3
"""D6b: afternoon_late_long_block の効果検証 (60日 5m).

3103.T / 6723.T / 9468.T を baseline (現行 universe params) と
afternoon_late_long_block=1 で比較し、後場終盤 long 抑制が PnL を改善するか検証。
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

# D6a 解析で long_pnl が大幅敗北だった T_afternoon_b 持つ銘柄
TARGET_SYMBOLS = ["3103.T", "6723.T", "9468.T", "9433.T", "8306.T"]


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


def evaluate(df: pd.DataFrame, sym: str, params: dict) -> dict:
    strat = create_strategy("MacdRci", sym, params=params)
    result = run_backtest(strat, df, starting_cash=990_000, fee_pct=0.0,
                          position_pct=1.0, usd_jpy=1.0, lot_size=100,
                          limit_slip_pct=0.0008, eod_close_time=(15, 25))
    trades = result.trades
    if not trades:
        return {"n": 0, "wr": 0, "pf": 0, "pnl": 0, "long_pnl": 0, "short_pnl": 0}
    wins = [t for t in trades if t.pnl > 0]
    gw = sum(t.pnl for t in wins)
    gl = abs(sum(t.pnl for t in trades if t.pnl <= 0))
    wr = len(wins) / len(trades) * 100
    pf = gw / gl if gl > 0 else 0
    long_pnl = sum(t.pnl for t in trades if t.side == "long")
    short_pnl = sum(t.pnl for t in trades if t.side == "short")
    long_n = sum(1 for t in trades if t.side == "long")
    short_n = sum(1 for t in trades if t.side == "short")
    return {
        "n": len(trades), "wr": round(wr, 1), "pf": round(pf, 2),
        "pnl": round(sum(t.pnl for t in trades), 0),
        "long_pnl": round(long_pnl, 0), "short_pnl": round(short_pnl, 0),
        "long_n": long_n, "short_n": short_n,
    }


def main() -> None:
    print(f"=== D6b: afternoon_late_long_block PoC (60d 5m) ===\n")
    u = json.load(open("data/universe_active.json"))
    macd_by_sym = {s["symbol"]: s for s in u["symbols"] if s["strategy"] == "MacdRci"}
    
    out = {}
    for sym in TARGET_SYMBOLS:
        if sym not in macd_by_sym:
            print(f"  {sym}: skip (not in universe)")
            continue
        print(f"--- {sym} ---")
        df = fetch_5m(sym)
        if df.empty:
            print(f"  no data")
            continue
        n_days = len(set(df.index.date))
        base_params = dict(macd_by_sym[sym].get("params", {}))
        # Baseline
        baseline = evaluate(df, sym, base_params)
        # With afternoon_late_long_block=1 from 14:00
        params_blk = dict(base_params)
        params_blk["afternoon_late_long_block"] = 1
        params_blk["afternoon_late_block_from_min"] = 14 * 60
        blk_14 = evaluate(df, sym, params_blk)
        # With block from 13:30 (more aggressive)
        params_blk2 = dict(base_params)
        params_blk2["afternoon_late_long_block"] = 1
        params_blk2["afternoon_late_block_from_min"] = 13 * 60 + 30
        blk_1330 = evaluate(df, sym, params_blk2)

        delta_14 = blk_14["pnl"] - baseline["pnl"]
        delta_1330 = blk_1330["pnl"] - baseline["pnl"]
        print(f"  base       n={baseline['n']:3d} wr={baseline['wr']:5.1f}% pf={baseline['pf']:5.2f} "
              f"pnl={baseline['pnl']:+8.0f} long={baseline['long_pnl']:+.0f} short={baseline['short_pnl']:+.0f}")
        print(f"  blk@14:00  n={blk_14['n']:3d} wr={blk_14['wr']:5.1f}% pf={blk_14['pf']:5.2f} "
              f"pnl={blk_14['pnl']:+8.0f} long={blk_14['long_pnl']:+.0f} short={blk_14['short_pnl']:+.0f} "
              f"Δ={delta_14:+.0f}")
        print(f"  blk@13:30  n={blk_1330['n']:3d} wr={blk_1330['wr']:5.1f}% pf={blk_1330['pf']:5.2f} "
              f"pnl={blk_1330['pnl']:+8.0f} long={blk_1330['long_pnl']:+.0f} short={blk_1330['short_pnl']:+.0f} "
              f"Δ={delta_1330:+.0f}")

        # 推奨判定
        best_delta = max(delta_14, delta_1330)
        recommend = None
        if best_delta > 5000:  # 60日で +5000 円以上の改善 = 適用候補
            if delta_14 >= delta_1330:
                recommend = {"afternoon_late_long_block": 1, "afternoon_late_block_from_min": 14 * 60}
            else:
                recommend = {"afternoon_late_long_block": 1, "afternoon_late_block_from_min": 13 * 60 + 30}

        out[sym] = {
            "n_days": n_days,
            "baseline": baseline,
            "blk_14": blk_14,
            "blk_1330": blk_1330,
            "delta_14": delta_14,
            "delta_1330": delta_1330,
            "recommend": recommend,
        }

    Path("data/d6_afternoon_late_block_poc.json").write_text(
        json.dumps({"generated_at": datetime.now(JST).isoformat(), "results": out},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("\n=== 推奨適用 ===")
    total_improvement = 0
    apply_list = []
    for sym, r in out.items():
        if r.get("recommend"):
            best = max(r["delta_14"], r["delta_1330"])
            print(f"  {sym}: apply {r['recommend']} (delta {best:+.0f} 円 / {r['n_days']}d)")
            total_improvement += best
            apply_list.append({"symbol": sym, "params": r["recommend"], "delta_pnl": best})
    print(f"\n total improvement: {total_improvement:+.0f} 円 / 60d  (= {total_improvement/60:+.0f} 円/日)")

    Path("data/d6_afternoon_late_block_recommend.json").write_text(
        json.dumps({"apply": apply_list, "expected_total_pnl_improvement": total_improvement,
                   "expected_per_day": total_improvement / 60.0},
                  ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: data/d6_afternoon_late_block_poc.json")
    print(f"saved: data/d6_afternoon_late_block_recommend.json")


if __name__ == "__main__":
    main()
