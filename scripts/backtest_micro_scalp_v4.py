#!/usr/bin/env python3
"""MicroScalp v4 — 寄り付きバイアス (open_bias_mode) を組み込んだバージョン.

ユーザー提案 (2026-04-30 17:07):
  - 始値ギャップ × 9:00-9:10 初動 で方向バイアスを変える
  - 寄り天疑い → ショート、トレンド継続 → ロング

事前検証 (data/micro_scalp_open_patterns.json):
  - GD 系 (gap<-0.3%) はショート 64-87%
  - GU_big (>1%) × 初動 flat はロング 100% (ただし n=2)
  - GU_big × 初動 up/down は寄り天 75% ショート

このスクリプトの設定:
  v3_locked_10_5     (v3 ベースライン, bias OFF)
  v4_bias_10_5       (v3 + open_bias_mode=True)
  v4_bias_8_4        (TP/SL 8/4 + open_bias_mode=True)
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.backtesting.engine import run_backtest  # noqa: E402
from backend.backtesting.strategy_factory import create as create_strategy  # noqa: E402

JST = "Asia/Tokyo"


def fetch_1m(symbol: str, days: int = 7) -> pd.DataFrame | None:
    try:
        df = yf.download(symbol, period=f"{days}d", interval="1m",
                         auto_adjust=False, progress=False)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(JST)
    hh, mm = df.index.hour, df.index.minute
    in_morning = ((hh == 9) & (mm >= 0)) | ((hh == 10)) | ((hh == 11) & (mm <= 30))
    in_afternoon = ((hh == 12) & (mm >= 30)) | ((hh == 13)) | ((hh == 14)) | ((hh == 15) & (mm <= 30))
    return df[in_morning | in_afternoon]


def evaluate(symbol: str, df: pd.DataFrame, params: dict, label: str) -> dict:
    name = symbol.replace(".T", "")
    strat = create_strategy("MicroScalp", symbol, name=name, params=params)
    result = run_backtest(
        strat, df,
        starting_cash=990_000.0, fee_pct=0.0,
        position_pct=0.30, usd_jpy=1.0, lot_size=100,
        limit_slip_pct=0.001, eod_close_time=(15, 25),
        subsession_cooldown_min=2, daily_loss_limit_pct=-3.0,
    )
    n = len(result.trades)
    if n == 0:
        return {"label": label, "symbol": symbol, "trades": 0,
                "wr": None, "pf": None, "pnl_jpy": 0.0,
                "long_n": 0, "short_n": 0,
                "long_pnl": 0.0, "short_pnl": 0.0,
                "trades_per_day": 0, "days": None}
    wins = [t for t in result.trades if t.pnl > 0]
    losses = [t for t in result.trades if t.pnl <= 0]
    pf = (sum(t.pnl for t in wins) / -sum(t.pnl for t in losses)) if losses else 999.0
    long_trades = [t for t in result.trades if t.side == "long"]
    short_trades = [t for t in result.trades if t.side == "short"]
    days = max(1, (df.index[-1].date() - df.index[0].date()).days + 1)
    return {
        "label": label, "symbol": symbol,
        "trades": n,
        "wr": round(len(wins) / n * 100, 1),
        "pf": round(pf, 2),
        "long_n": len(long_trades), "short_n": len(short_trades),
        "long_pnl": round(float(sum(t.pnl for t in long_trades)), 0),
        "short_pnl": round(float(sum(t.pnl for t in short_trades)), 0),
        "long_wr": round(sum(1 for t in long_trades if t.pnl > 0) / max(1, len(long_trades)) * 100, 1) if long_trades else None,
        "short_wr": round(sum(1 for t in short_trades if t.pnl > 0) / max(1, len(short_trades)) * 100, 1) if short_trades else None,
        "pnl_jpy": round(float(sum(t.pnl for t in result.trades)), 0),
        "days": days,
        "trades_per_day": round(n / days, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="4568.T,8306.T,9433.T,3103.T,6723.T,8136.T,3382.T,9984.T,6758.T")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="data/micro_scalp_v4_latest.json")
    args = ap.parse_args()

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"=== MicroScalp v4 (open_bias 版) === {len(syms)} symbols, {args.days}d")

    base = {
        "entry_dev_jpy": 8.0, "atr_min_jpy": 3.0, "timeout_bars": 3,
        "cooldown_bars": 5, "max_trades_per_day": 20,
        "avoid_open_min": 0, "avoid_close_min": 30,
        "allowed_time_windows": ["09:00-09:30", "12:30-15:00"],
    }

    configs = [
        ("v3_locked_10_5",   {**base, "tp_jpy": 10, "sl_jpy": 5, "open_bias_mode": False}),
        ("v3_locked_8_4",    {**base, "tp_jpy": 8,  "sl_jpy": 4, "open_bias_mode": False}),
        ("v4_bias_10_5",     {**base, "tp_jpy": 10, "sl_jpy": 5, "open_bias_mode": True,
                               "bias_observe_min": 10, "bias_apply_until_min": 30}),
        ("v4_bias_8_4",      {**base, "tp_jpy": 8,  "sl_jpy": 4, "open_bias_mode": True,
                               "bias_observe_min": 10, "bias_apply_until_min": 30}),
        ("v4_bias_obs5_10_5",{**base, "tp_jpy": 10, "sl_jpy": 5, "open_bias_mode": True,
                               "bias_observe_min": 5,  "bias_apply_until_min": 30}),
    ]

    results = []
    for sym in syms:
        df = fetch_1m(sym, days=args.days)
        if df is None or df.empty:
            print(f"\n--- {sym} (no data) ---")
            continue
        print(f"\n--- {sym} (rows={len(df)}) ---")
        for label, params in configs:
            r = evaluate(sym, df, params, label)
            extra = ""
            if r["trades"] > 0:
                extra = (f" | long {r['long_n']} (wr {r['long_wr']}%, pnl {r['long_pnl']:+.0f}) "
                         f"short {r['short_n']} (wr {r['short_wr']}%, pnl {r['short_pnl']:+.0f})")
            print(f"  [{label:<22}] n={r['trades']:>3} "
                  f"wr={r['wr'] if r['wr'] is not None else 0:>5}% "
                  f"pnl={r['pnl_jpy']:+8.0f}{extra}")
            results.append(r)

    # サマリ (label 別、銘柄合算)
    print("\n=== サマリ (label 別、銘柄合算) ===")
    by_label: dict = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0,
                                          "long_n": 0, "short_n": 0,
                                          "long_pnl": 0.0, "short_pnl": 0.0,
                                          "long_wins": 0, "short_wins": 0,
                                          "n_syms": 0})
    for r in results:
        if r["trades"] == 0:
            continue
        b = by_label[r["label"]]
        b["trades"] += r["trades"]
        b["wins"] += int(round((r["wr"] or 0) / 100 * r["trades"]))
        b["pnl"] += r["pnl_jpy"]
        b["long_n"] += r["long_n"]
        b["short_n"] += r["short_n"]
        b["long_pnl"] += r["long_pnl"]
        b["short_pnl"] += r["short_pnl"]
        b["long_wins"] += int(round((r["long_wr"] or 0) / 100 * r["long_n"])) if r["long_n"] else 0
        b["short_wins"] += int(round((r["short_wr"] or 0) / 100 * r["short_n"])) if r["short_n"] else 0
        b["n_syms"] += 1

    print(f'{"label":<22} {"n_syms":>6} {"trades":>6} {"WR%":>6} '
          f'{"PnL/7d":>9} {"PnL/day":>8} | '
          f'{"long_n":>6} {"L_WR%":>6} {"L_PnL":>8} | '
          f'{"short_n":>7} {"S_WR%":>6} {"S_PnL":>8}')
    for label, _ in configs:
        b = by_label[label]
        if b["trades"] == 0:
            continue
        wr = b["wins"] / b["trades"] * 100
        days = max(r["days"] for r in results if r["days"]) or 1
        l_wr = (b["long_wins"] / b["long_n"] * 100) if b["long_n"] else 0
        s_wr = (b["short_wins"] / b["short_n"] * 100) if b["short_n"] else 0
        print(f'{label:<22} {b["n_syms"]:>6} {b["trades"]:>6} {wr:>5.1f}% '
              f'{b["pnl"]:>+9.0f} {b["pnl"]/days:>+8.0f} | '
              f'{b["long_n"]:>6} {l_wr:>5.1f}% {b["long_pnl"]:>+8.0f} | '
              f'{b["short_n"]:>7} {s_wr:>5.1f}% {b["short_pnl"]:>+8.0f}')

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "symbols": syms, "days": args.days,
        "configs": [{"label": l, "params": p} for l, p in configs],
        "results": results,
        "summary_by_label": {
            label: {
                "trades": by_label[label]["trades"],
                "wr_pct": (by_label[label]["wins"] / by_label[label]["trades"] * 100)
                          if by_label[label]["trades"] else None,
                "pnl_jpy": by_label[label]["pnl"],
                "long_n": by_label[label]["long_n"],
                "long_pnl": by_label[label]["long_pnl"],
                "short_n": by_label[label]["short_n"],
                "short_pnl": by_label[label]["short_pnl"],
                "n_syms_active": by_label[label]["n_syms"],
            } for label, _ in configs
        }
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=> 結果保存: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
