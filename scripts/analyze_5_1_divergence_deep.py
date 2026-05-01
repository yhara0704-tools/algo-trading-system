#!/usr/bin/env python3
"""5/1 paper の trade 単位 deep divergence 解析.

GW Day 1: 8 件の paper trade に対して以下を分析:
  1. 9:39-9:43 short cluster の市場コンテキスト (N225, TOPIX 同時動向)
  2. 各 trade の entry 時 5m/1m バー前後の動き
  3. 8316.T long が +5,500 円を取った瞬間の signal 構造
  4. backtest signal と実 paper の照合 (timing / px の差)

出力: data/divergence_analysis_5_1.json
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JST = timezone(timedelta(hours=9))

PAPER_TRADES = [
    {"entry": "2026-05-01 09:39:51", "exit": "2026-05-01 09:43:25", "sym": "4911.T", "side": "short", "qty": 200, "entry_px": 3140.0, "exit_px": 3154.0, "pnl": -2800, "reason": "stop"},
    {"entry": "2026-05-01 09:40:59", "exit": "2026-05-01 09:48:48", "sym": "9984.T", "side": "short", "qty": 100, "entry_px": 5356.0, "exit_px": 5362.0, "pnl": -600, "reason": "stop"},
    {"entry": "2026-05-01 09:43:19", "exit": "2026-05-01 09:46:30", "sym": "9433.T", "side": "short", "qty": 200, "entry_px": 2547.5, "exit_px": 2551.5, "pnl": -800, "reason": "stop"},
    {"entry": "2026-05-01 09:46:29", "exit": "2026-05-01 11:04:00", "sym": "8316.T", "side": "long", "qty": 100, "entry_px": 5474.0, "exit_px": 5529.0, "pnl": 5500, "reason": "target"},
    {"entry": "2026-05-01 10:16:47", "exit": "2026-05-01 14:19:30", "sym": "4911.T", "side": "long", "qty": 100, "entry_px": 3138.0, "exit_px": 3141.0, "pnl": 300, "reason": "regime_flip"},
    {"entry": "2026-05-01 11:04:09", "exit": "2026-05-01 15:05:05", "sym": "9433.T", "side": "long", "qty": 200, "entry_px": 2534.5, "exit_px": 2538.0, "pnl": 700, "reason": "session_close"},
    {"entry": "2026-05-01 14:20:31", "exit": "2026-05-01 14:38:02", "sym": "6723.T", "side": "long", "qty": 100, "entry_px": 3217.0, "exit_px": 3200.0, "pnl": -1700, "reason": "stop"},
    {"entry": "2026-05-01 14:38:03", "exit": "2026-05-01 15:05:05", "sym": "4911.T", "side": "long", "qty": 100, "entry_px": 3144.0, "exit_px": 3146.0, "pnl": 200, "reason": "session_close"},
]


def fetch_5m(symbol: str, start_date: str = "2026-04-30", end_date: str = "2026-05-02") -> pd.DataFrame:
    df = yf.download(symbol, start=start_date, end=end_date, interval="5m",
                     progress=False, auto_adjust=False)
    if df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    return df


def analyze_morning_short_cluster(df_n225: pd.DataFrame, dfs: dict[str, pd.DataFrame]) -> dict:
    """9:30-10:00 の市場・個別銘柄動向を分析."""
    out = {"n225_morning_5m": [], "per_symbol_morning": {}}
    for ts in df_n225.index:
        if ts.date() != pd.Timestamp("2026-05-01").date():
            continue
        if ts.hour != 9:
            continue
        row = df_n225.loc[ts]
        out["n225_morning_5m"].append({
            "ts": ts.strftime("%H:%M:%S"),
            "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
        })
    # short cluster の 4 銘柄 (4911, 9984, 9433) + 大勝 8316
    for sym in ["4911.T", "9984.T", "9433.T", "8316.T", "6723.T"]:
        df = dfs.get(sym)
        if df is None or df.empty:
            continue
        bars = []
        for ts in df.index:
            if ts.date() != pd.Timestamp("2026-05-01").date():
                continue
            if ts.hour != 9:
                continue
            row = df.loc[ts]
            bars.append({
                "ts": ts.strftime("%H:%M:%S"),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": int(row["volume"]),
            })
        out["per_symbol_morning"][sym] = bars
    return out


def main() -> None:
    print("=== 5/1 divergence deep analysis ===\n")
    n225 = fetch_5m("^N225")
    topix = fetch_5m("1306.T")  # TOPIX ETF

    symbols = sorted({t["sym"] for t in PAPER_TRADES})
    dfs = {sym: fetch_5m(sym) for sym in symbols}

    # 1. Morning cluster 分析
    print("--- 9:00-9:55 N225 5m bars ---")
    if not n225.empty:
        for ts, row in n225.iterrows():
            if ts.date() != pd.Timestamp("2026-05-01").date():
                continue
            if ts.hour != 9:
                continue
            change = (row["close"] - row["open"]) / row["open"] * 100
            print(f"  {ts.strftime('%H:%M')} O={row['open']:>8.1f} H={row['high']:>8.1f} L={row['low']:>8.1f} C={row['close']:>8.1f}  Δ={change:+5.2f}%")

    # 2. short cluster 4 銘柄の 9:00-10:00
    print("\n--- 9:00-9:55 short cluster 4 銘柄 (4911 / 9984 / 9433) ---")
    for sym in ["4911.T", "9984.T", "9433.T"]:
        df = dfs.get(sym)
        if df is None or df.empty:
            continue
        print(f"\n  {sym}:")
        for ts, row in df.iterrows():
            if ts.date() != pd.Timestamp("2026-05-01").date():
                continue
            if ts.hour != 9:
                continue
            change = (row["close"] - row["open"]) / row["open"] * 100
            in_loss_window = "← entry/stop window" if 9 * 60 + 35 <= ts.hour * 60 + ts.minute <= 9 * 60 + 50 else ""
            print(f"    {ts.strftime('%H:%M')} O={row['open']:>7.1f} H={row['high']:>7.1f} L={row['low']:>7.1f} C={row['close']:>7.1f}  Δ={change:+5.2f}% vol={int(row['volume']):>8} {in_loss_window}")

    # 3. 8316.T 大勝
    print("\n--- 8316.T 大勝 (entry 09:46 → exit 11:04 +5,500) ---")
    df = dfs.get("8316.T")
    if df is not None and not df.empty:
        for ts, row in df.iterrows():
            if ts.date() != pd.Timestamp("2026-05-01").date():
                continue
            if ts.hour < 9 or (ts.hour == 11 and ts.minute > 10):
                continue
            if ts.hour == 11 and ts.minute > 10:
                break
            change = (row["close"] - row["open"]) / row["open"] * 100
            mark = ""
            tm = ts.hour * 60 + ts.minute
            if tm == 9 * 60 + 45:
                mark = "← LONG entry next bar"
            if tm == 11 * 60:
                mark = "← TARGET hit"
            print(f"    {ts.strftime('%H:%M')} O={row['open']:>7.1f} H={row['high']:>7.1f} L={row['low']:>7.1f} C={row['close']:>7.1f}  Δ={change:+5.2f}%  {mark}")

    # 4. 6723.T late long
    print("\n--- 6723.T late long (entry 14:20 → stop 14:38 -1,700) ---")
    df = dfs.get("6723.T")
    if df is not None and not df.empty:
        for ts, row in df.iterrows():
            if ts.date() != pd.Timestamp("2026-05-01").date():
                continue
            tm = ts.hour * 60 + ts.minute
            if tm < 14 * 60 + 0 or tm > 14 * 60 + 50:
                continue
            change = (row["close"] - row["open"]) / row["open"] * 100
            mark = ""
            if tm == 14 * 60 + 20:
                mark = "← LONG entry"
            if tm == 14 * 60 + 35:
                mark = "← STOP"
            print(f"    {ts.strftime('%H:%M')} O={row['open']:>7.1f} H={row['high']:>7.1f} L={row['low']:>7.1f} C={row['close']:>7.1f}  Δ={change:+5.2f}%  {mark}")

    # save
    out = {
        "computed_at": datetime.now(JST).isoformat(),
        "trades": PAPER_TRADES,
        "morning_n225": [],
        "per_symbol_morning": {},
    }
    morning = analyze_morning_short_cluster(n225, dfs)
    out["morning_n225"] = morning["n225_morning_5m"]
    out["per_symbol_morning"] = morning["per_symbol_morning"]
    Path("data/divergence_analysis_5_1.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\nsaved: data/divergence_analysis_5_1.json")


if __name__ == "__main__":
    main()
