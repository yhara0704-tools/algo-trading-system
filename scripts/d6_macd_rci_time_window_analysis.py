#!/usr/bin/env python3
"""D6a: MacdRci per-symbol 時間帯別 WR 分析 (60日 5m).

universe の MacdRci 銘柄について、entry_time 時間帯ごとの WR/PnL を集計し、
寄り直後 (09:00-09:30) で WR が極端に低い銘柄を特定する。

これらの銘柄には universe 設定で `morning_block_until_min` を有効化し、
高ボラ寄りで簡単に stop hit する事故を構造的に避ける。

時間帯定義:
  T_open       09:00-09:30 (寄り直後)
  T_morning_a  09:30-10:30 (前場前半)
  T_morning_b  10:30-11:30 (前場後半)
  T_afternoon_a 12:30-13:30 (後場前半)
  T_afternoon_b 13:30-15:00 (後場後半 = 大引け前)

出力:
  data/d6_macd_rci_time_window_wr.json
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

JST = timezone(timedelta(hours=9))


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


def classify_window(ts) -> str:
    if isinstance(ts, str):
        try:
            ts = pd.to_datetime(ts)
        except Exception:
            return "unknown"
    if hasattr(ts, "tz_convert"):
        ts = ts.tz_convert("Asia/Tokyo") if ts.tz else ts
    h, m = ts.hour, ts.minute
    cur = h * 60 + m
    if 9 * 60 <= cur < 9 * 60 + 30:
        return "T_open"
    elif 9 * 60 + 30 <= cur < 10 * 60 + 30:
        return "T_morning_a"
    elif 10 * 60 + 30 <= cur < 11 * 60 + 30:
        return "T_morning_b"
    elif 12 * 60 + 30 <= cur < 13 * 60 + 30:
        return "T_afternoon_a"
    elif 13 * 60 + 30 <= cur < 15 * 60:
        return "T_afternoon_b"
    return "T_other"


def main() -> None:
    print(f"=== D6a: MacdRci per-symbol 時間帯別 WR 分析 ===\n")

    # universe から MacdRci entries を読み込む
    u = json.load(open("data/universe_active.json"))
    macd_syms = [s for s in u.get("symbols", []) if s["strategy"] == "MacdRci"]
    print(f"対象: {len(macd_syms)} MacdRci 銘柄\n")

    all_results = {}
    for entry in macd_syms:
        sym = entry["symbol"]
        params = entry.get("params", {})
        print(f"--- {sym} ---")
        df = fetch_5m_60d(sym)
        if df.empty or len(df) < 500:
            print(f"  skip (insufficient data)")
            continue
        n_days = len(set(df.index.date))
        print(f"  bars={len(df)} days={n_days}")

        try:
            strat = create_strategy("MacdRci", sym, params=params)
            result = run_backtest(strat, df, starting_cash=990_000, fee_pct=0.0,
                                  position_pct=1.0, usd_jpy=1.0, lot_size=100,
                                  limit_slip_pct=0.0008, eod_close_time=(15, 25))
        except Exception as e:
            print(f"  backtest error: {e}")
            continue

        trades = result.trades
        if not trades:
            print(f"  no trades")
            continue

        # 時間帯別 集計
        by_window = {}
        for t in trades:
            w = classify_window(t.entry_time)
            if w not in by_window:
                by_window[w] = []
            by_window[w].append(t)

        windows_summary = {}
        for w, ts in by_window.items():
            wins = [x for x in ts if x.pnl > 0]
            losses = [x for x in ts if x.pnl <= 0]
            total = sum(x.pnl for x in ts)
            gw = sum(x.pnl for x in wins) if wins else 0
            gl = abs(sum(x.pnl for x in losses)) if losses else 1e-6
            wr = len(wins) / len(ts) * 100 if ts else 0
            pf = gw / gl if gl > 0 else 0
            windows_summary[w] = {
                "n": len(ts),
                "wr": round(wr, 1),
                "pf": round(pf, 2),
                "pnl": round(total, 0),
                "long_n": sum(1 for x in ts if x.side == "long"),
                "short_n": sum(1 for x in ts if x.side == "short"),
                "long_pnl": round(sum(x.pnl for x in ts if x.side == "long"), 0),
                "short_pnl": round(sum(x.pnl for x in ts if x.side == "short"), 0),
            }

        # ── 表示 ──
        print(f"  total trades={len(trades)}")
        for w in ["T_open", "T_morning_a", "T_morning_b", "T_afternoon_a", "T_afternoon_b", "T_other"]:
            if w not in windows_summary:
                continue
            ws = windows_summary[w]
            print(f"    {w:15} n={ws['n']:3d} wr={ws['wr']:5.1f}% pf={ws['pf']:5.2f} "
                  f"pnl={ws['pnl']:+7.0f} long={ws['long_n']}({ws['long_pnl']:+.0f}) "
                  f"short={ws['short_n']}({ws['short_pnl']:+.0f})")

        all_results[sym] = {
            "n_days": n_days,
            "total_trades": len(trades),
            "by_window": windows_summary,
        }
        time.sleep(0.3)

    out_path = Path("data/d6_macd_rci_time_window_wr.json")
    out_path.write_text(json.dumps({
        "generated_at": datetime.now(JST).isoformat(),
        "n_symbols": len(all_results),
        "results": all_results,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ── 改善候補 ──
    print("\n=== 寄り直後 (T_open) で WR/PnL 悪化銘柄 ===\n")
    print("(WR < 35% または short_pnl < -1000 円: morning_block 適用候補)\n")
    block_candidates = []
    for sym, r in all_results.items():
        ow = r.get("by_window", {}).get("T_open", {})
        if not ow:
            continue
        if ow["n"] < 3:
            continue  # sample 不足
        wr = ow.get("wr", 0)
        short_pnl = ow.get("short_pnl", 0)
        long_pnl = ow.get("long_pnl", 0)
        # 判定: 寄り直後 short が大きく負けている、または全体 WR が低い
        if short_pnl < -1000 or wr < 35:
            print(f"  {sym:8} T_open n={ow['n']:3d} wr={wr:5.1f}% pf={ow['pf']:5.2f} "
                  f"long={ow['long_n']}({long_pnl:+.0f}) short={ow['short_n']}({short_pnl:+.0f})")
            block_candidates.append({
                "symbol": sym,
                "window": "T_open",
                "wr": wr,
                "short_pnl": short_pnl,
                "long_pnl": long_pnl,
                "recommendation": "morning_first_30min_short_block=1" if short_pnl < -1000 else "morning_block_until_min=30 (両方向)",
            })

    Path("data/d6_morning_block_candidates.json").write_text(
        json.dumps({"candidates": block_candidates}, ensure_ascii=False, indent=2,
                   default=str), encoding="utf-8")
    print(f"\nsaved: data/d6_macd_rci_time_window_wr.json")
    print(f"saved: data/d6_morning_block_candidates.json")
    print(f"\nblock 候補: {len(block_candidates)} 銘柄")


if __name__ == "__main__":
    main()
