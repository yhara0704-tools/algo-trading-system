#!/usr/bin/env python3
"""MicroScalp per-symbol 30d 1m データ最適化.

Day 3 D3a/D3b の中核スクリプト。

目的:
  1. yfinance 1m 30 日分を universe 全銘柄でフェッチ
  2. 各銘柄について TP/SL/entry_dev のグリッドサーチ
  3. 時間帯別 (09:00-09:30, 09:30-11:30, 12:30-15:00) の WR 比較
  4. open_bias_mode の有無による効果比較
  5. 結果を data/microscalp_per_symbol_30d.json に保存
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
from backend.strategies.jp_stock.jp_micro_scalp import JPMicroScalp

JST = timezone(timedelta(hours=9))


# 候補銘柄: universe_active.json から取得 + 流動性高い既知 MicroScalp 候補
CANDIDATES = [
    "4568.T",  # 第一三共 (v4 で +7,950)
    "8306.T",  # MUFG (v4 で +4,000)
    "3103.T",  # ユニチカ (v4 で +3,700)
    "6723.T",  # ルネサス (v4 で +2,300)
    "9433.T",  # KDDI (v4 で +2,200, sample 少)
    "8136.T",  # サンリオ (v4 で +1,000)
    "9984.T",  # ソフトバンクグループ
    "8316.T",  # 三井住友
    "6613.T",  # QD レーザ
    "1605.T",  # INPEX
    "9468.T",  # 角川
    "4911.T",  # 資生堂
    "9432.T",  # NTT
    "6501.T",  # 日立
    "6752.T",  # パナソニック
    "8058.T",  # 三菱商事
]


def fetch_1m_30d(symbol: str) -> pd.DataFrame:
    """yfinance 1m 30 日 (7 日 × 4-5 batch) フェッチ."""
    end = datetime.now(JST)
    all_dfs = []
    for i in range(4):  # 7×4 = 28 日 (yfinance 30 日制限内)
        batch_end = end - timedelta(days=i * 7)
        batch_start = batch_end - timedelta(days=7)
        try:
            df = yf.download(
                symbol, start=batch_start, end=batch_end,
                interval="1m", progress=False, auto_adjust=False,
            )
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
        except Exception as e:
            print(f"  {symbol} batch {i}: {e}", file=sys.stderr)
        time.sleep(0.3)
    if not all_dfs:
        return pd.DataFrame()
    df_all = pd.concat(all_dfs).sort_index()
    df_all = df_all[~df_all.index.duplicated(keep="first")]
    # 9:00-15:30 のセッションのみ
    df_all = df_all[df_all.index.map(lambda t: 9 <= t.hour < 15 or (t.hour == 15 and t.minute < 30))]
    return df_all


def evaluate_config(df: pd.DataFrame, symbol: str, params: dict) -> dict:
    """1 設定で run_backtest し統計を返す."""
    if df.empty or len(df) < 1000:
        return {"trades": 0, "wr": 0, "pnl": 0, "pf": 0, "n_bars": len(df)}
    strat = JPMicroScalp(symbol=symbol, name=symbol, **params)
    result = run_backtest(
        strat, df,
        starting_cash=990_000,
        fee_pct=0.0,
        position_pct=1.0,  # 全余力 (1 ポジで MicroScalp)
        usd_jpy=1.0,
        lot_size=100,
        limit_slip_pct=0.0005,
        eod_close_time=(15, 25),
    )
    trades = result.trades
    if not trades:
        return {"trades": 0, "wr": 0, "pnl": 0, "pf": 0, "n_bars": len(df)}
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_pnl = sum(t.pnl for t in trades)
    gross_win = sum(t.pnl for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl for t in losses)) if losses else 1e-6
    pf = gross_win / gross_loss if gross_loss > 0 else 0
    wr = len(wins) / len(trades) * 100 if trades else 0

    # 時間帯別 WR
    by_window = {"09:00-09:30": [], "09:30-11:30": [], "12:30-15:00": []}
    for t in trades:
        if not hasattr(t, "entry_time"):
            continue
        ts = t.entry_time
        if isinstance(ts, str):
            try:
                ts = pd.to_datetime(ts)
            except Exception:
                continue
        if hasattr(ts, "tz_convert"):
            ts = ts.tz_convert("Asia/Tokyo") if ts.tz else ts
        elif hasattr(ts, "tz_localize"):
            ts = ts.tz_localize("Asia/Tokyo") if ts.tz is None else ts.tz_convert("Asia/Tokyo")
        h, m = ts.hour, ts.minute
        cur = h * 60 + m
        if 9 * 60 <= cur < 9 * 60 + 30:
            by_window["09:00-09:30"].append(t)
        elif 9 * 60 + 30 <= cur < 11 * 60 + 30:
            by_window["09:30-11:30"].append(t)
        elif 12 * 60 + 30 <= cur:
            by_window["12:30-15:00"].append(t)
    window_stats = {}
    for w, ts in by_window.items():
        if not ts:
            window_stats[w] = {"n": 0, "wr": 0, "pnl": 0}
            continue
        wn = [x for x in ts if x.pnl > 0]
        window_stats[w] = {
            "n": len(ts),
            "wr": round(len(wn) / len(ts) * 100, 1),
            "pnl": round(sum(x.pnl for x in ts), 0),
        }

    return {
        "trades": len(trades),
        "wins": len(wins),
        "wr": round(wr, 1),
        "pnl": round(total_pnl, 0),
        "pf": round(pf, 2),
        "n_bars": len(df),
        "long_n": sum(1 for t in trades if t.side == "long"),
        "short_n": sum(1 for t in trades if t.side == "short"),
        "by_window": window_stats,
    }


def main() -> None:
    print(f"=== MicroScalp 30d Optimization (start: {datetime.now(JST):%H:%M:%S}) ===\n")

    # configs to evaluate
    CONFIGS = [
        {"label": "tp10_sl5_dev10",
         "params": {"tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 10}},
        {"label": "tp8_sl4_dev8",
         "params": {"tp_jpy": 8, "sl_jpy": 4, "entry_dev_jpy": 8}},
        {"label": "tp5_sl3_dev6",
         "params": {"tp_jpy": 5, "sl_jpy": 3, "entry_dev_jpy": 6}},
        {"label": "tp10_sl5_bias",
         "params": {"tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 10, "open_bias_mode": True}},
        {"label": "morning_only_tp8_sl4",
         "params": {"tp_jpy": 8, "sl_jpy": 4, "entry_dev_jpy": 8,
                    "allowed_time_windows": ["09:30-11:30"]}},
        {"label": "afternoon_only_tp10_sl5",
         "params": {"tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 10,
                    "allowed_time_windows": ["12:30-15:00"]}},
        {"label": "open_30min_only_tp5_sl3",
         "params": {"tp_jpy": 5, "sl_jpy": 3, "entry_dev_jpy": 5,
                    "avoid_open_min": 0,
                    "allowed_time_windows": ["09:00-09:30"]}},
    ]

    all_results = []
    cache = {}

    for sym in CANDIDATES:
        print(f"--- {sym} ---")
        if sym not in cache:
            print(f"  fetching 1m 30d ...", end=" ", flush=True)
            df = fetch_1m_30d(sym)
            cache[sym] = df
            n_days = len(set(df.index.date)) if not df.empty else 0
            print(f"bars={len(df)} days={n_days}")
            if df.empty or n_days < 5:
                print(f"  skip (insufficient data)")
                continue
        else:
            df = cache[sym]

        for cfg in CONFIGS:
            r = evaluate_config(df, sym, cfg["params"])
            r["symbol"] = sym
            r["label"] = cfg["label"]
            r["params"] = cfg["params"]
            r["n_days"] = len(set(df.index.date)) if not df.empty else 0
            r["pnl_per_day"] = round(r["pnl"] / max(r["n_days"], 1), 0)
            all_results.append(r)
            print(f"  {cfg['label']:35} trades={r['trades']:3d} wr={r['wr']:5.1f}% "
                  f"pf={r['pf']:5.2f} pnl={r['pnl']:7.0f} /day={r['pnl_per_day']:6.0f}")

    out_path = Path("data/microscalp_per_symbol_30d.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generated_at": datetime.now(JST).isoformat(),
        "n_symbols": len(set(r["symbol"] for r in all_results)),
        "n_configs": len(CONFIGS),
        "results": all_results,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: {out_path}")

    # summary: per-symbol best
    print("\n=== Per-symbol best config (sorted by pnl_per_day) ===")
    by_sym: dict[str, dict] = {}
    for r in all_results:
        s = r["symbol"]
        if s not in by_sym or r.get("pnl_per_day", 0) > by_sym[s].get("pnl_per_day", 0):
            by_sym[s] = r
    for s, r in sorted(by_sym.items(), key=lambda x: -x[1].get("pnl_per_day", 0)):
        wstats = r.get("by_window", {})
        wstr = " | ".join(f"{w}:n={ws['n']},wr={ws['wr']}" for w, ws in wstats.items() if ws['n'] > 0)
        print(f"  {s:8} {r['label']:30} trades={r['trades']:3d} wr={r['wr']:5.1f}% "
              f"pnl/day={r['pnl_per_day']:6.0f}  [{wstr}]")


if __name__ == "__main__":
    main()
