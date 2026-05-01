#!/usr/bin/env python3
"""MicroScalp 時間帯絞り込み + per-symbol final config 最終化.

D3a の結果から、**12:30-15:00 が圧倒的に高 WR** という発見を活用し、
top 6 銘柄に各銘柄の best config + 時間帯フィルタを掛けて最終化。

時間帯バリエーション:
  - all: 09:35-11:30 + 12:30-15:00 (既定の avoid_open_min=5)
  - afternoon_only: 12:30-15:00
  - morning_open_only: 09:00-09:30 (open_30min)
  - mixed_open_afternoon: 09:00-09:30 + 12:30-15:00

PNL 改善が +500-2,000 円/銘柄 期待。
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
from backend.strategies.jp_stock.jp_micro_scalp import JPMicroScalp

JST = timezone(timedelta(hours=9))


# Top 候補 + per-symbol best base config (D3a 結果より)
PER_SYMBOL_BASE = {
    "6613.T": {"tp_jpy": 8, "sl_jpy": 4, "entry_dev_jpy": 8},
    "6723.T": {"tp_jpy": 5, "sl_jpy": 3, "entry_dev_jpy": 6},
    "9984.T": {"tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 10},
    "4911.T": {"tp_jpy": 8, "sl_jpy": 4, "entry_dev_jpy": 8},
    "6501.T": {"tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 10, "open_bias_mode": True},
    "1605.T": {"tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 10, "open_bias_mode": True},
    "8316.T": {"tp_jpy": 8, "sl_jpy": 4, "entry_dev_jpy": 8},
    "6752.T": {"tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 10},
    "3103.T": {"tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 10, "open_bias_mode": True},
    "8058.T": {"tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 10, "open_bias_mode": True},
    "8136.T": {"tp_jpy": 5, "sl_jpy": 3, "entry_dev_jpy": 6},
    "4568.T": {"tp_jpy": 5, "sl_jpy": 3, "entry_dev_jpy": 6},
    "9433.T": {"tp_jpy": 5, "sl_jpy": 3, "entry_dev_jpy": 5, "avoid_open_min": 0},
    "8306.T": {"tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 10},
    "9468.T": {"tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 10, "open_bias_mode": True},
}

TIME_WINDOWS = {
    "all_default": [],
    "afternoon_only": ["12:30-15:00"],
    "morning_open_only": ["09:00-09:30"],
    "morning_session_only": ["09:30-11:30"],
    "open_plus_afternoon": ["09:00-09:30", "12:30-15:00"],
    "session_plus_afternoon": ["09:30-11:30", "12:30-15:00"],
}


def fetch_1m_30d(symbol: str) -> pd.DataFrame:
    end = datetime.now(JST)
    all_dfs = []
    for i in range(4):
        batch_end = end - timedelta(days=i * 7)
        batch_start = batch_end - timedelta(days=7)
        try:
            df = yf.download(symbol, start=batch_start, end=batch_end,
                             interval="1m", progress=False, auto_adjust=False)
            if df is not None and not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
                df.columns = ["open", "high", "low", "close", "volume"]
                if df.index.tz is None:
                    df.index = df.index.tz_localize("UTC").tz_convert("Asia/Tokyo")
                else:
                    df.index = df.index.tz_convert("Asia/Tokyo")
                all_dfs.append(df)
        except Exception:
            pass
        time.sleep(0.3)
    if not all_dfs:
        return pd.DataFrame()
    df = pd.concat(all_dfs).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[df.index.map(lambda t: 9 <= t.hour < 15 or (t.hour == 15 and t.minute < 30))]
    return df


def evaluate(df: pd.DataFrame, symbol: str, params: dict) -> dict:
    if df.empty or len(df) < 1000:
        return {"trades": 0, "wr": 0, "pnl": 0, "pf": 0}
    strat = JPMicroScalp(symbol=symbol, name=symbol, **params)
    result = run_backtest(strat, df, starting_cash=990_000, fee_pct=0.0,
                          position_pct=1.0, usd_jpy=1.0, lot_size=100,
                          limit_slip_pct=0.0005, eod_close_time=(15, 25))
    trades = result.trades
    if not trades:
        return {"trades": 0, "wr": 0, "pnl": 0, "pf": 0}
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_pnl = sum(t.pnl for t in trades)
    gross_win = sum(t.pnl for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl for t in losses)) if losses else 1e-6
    pf = gross_win / gross_loss if gross_loss > 0 else 0
    return {
        "trades": len(trades),
        "wr": round(len(wins) / len(trades) * 100, 1),
        "pnl": round(total_pnl, 0),
        "pf": round(pf, 2),
    }


def main() -> None:
    print(f"=== MicroScalp Time-Window Fine-tune (start: {datetime.now(JST):%H:%M:%S}) ===\n")
    all_results = []

    for sym, base in PER_SYMBOL_BASE.items():
        print(f"--- {sym} ---")
        df = fetch_1m_30d(sym)
        n_days = len(set(df.index.date)) if not df.empty else 0
        if n_days < 5:
            print(f"  insufficient data, skip")
            continue

        best = None
        for tw_label, tw in TIME_WINDOWS.items():
            params = dict(base)
            params["allowed_time_windows"] = tw if tw else None
            r = evaluate(df, sym, params)
            r["symbol"] = sym
            r["time_window"] = tw_label
            r["base"] = base
            r["n_days"] = n_days
            r["pnl_per_day"] = round(r["pnl"] / max(n_days, 1), 0)
            print(f"  {tw_label:25} trades={r['trades']:3d} wr={r['wr']:5.1f}% "
                  f"pf={r['pf']:5.2f} pnl/day={r['pnl_per_day']:6.0f}")
            all_results.append(r)
            if best is None or r["pnl_per_day"] > best["pnl_per_day"]:
                best = r
        if best:
            print(f"  ★ BEST: {best['time_window']:25} pnl/day={best['pnl_per_day']:6.0f}")
        print()

    out_path = Path("data/microscalp_time_window_finetune.json")
    out_path.write_text(json.dumps({
        "generated_at": datetime.now(JST).isoformat(),
        "n_symbols": len(set(r["symbol"] for r in all_results)),
        "n_windows": len(TIME_WINDOWS),
        "results": all_results,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # final ranking
    print("\n=== Final per-symbol best (sorted by pnl_per_day) ===")
    by_sym: dict[str, dict] = {}
    for r in all_results:
        s = r["symbol"]
        if s not in by_sym or r["pnl_per_day"] > by_sym[s]["pnl_per_day"]:
            by_sym[s] = r
    total = 0
    for s, r in sorted(by_sym.items(), key=lambda x: -x[1]["pnl_per_day"]):
        print(f"  {s:8} {r['time_window']:25} pnl/day={r['pnl_per_day']:6.0f} wr={r['wr']:.1f}% trades={r['trades']}")
        total += r["pnl_per_day"]
    print(f"\n  --- TOP 6 合計: {sum(r['pnl_per_day'] for s, r in sorted(by_sym.items(), key=lambda x: -x[1]['pnl_per_day'])[:6]):,} 円/日 (期待) ---")
    print(f"  --- 全銘柄合計: {total:,} 円/日 (期待) ---")
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    main()
