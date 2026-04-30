#!/usr/bin/env python3
"""MicroScalp v3 — グリッドサーチで判明した最適設定をロックして再評価.

設定:
  - 時間帯: 09:00-09:30 + 12:30-15:00 のみ許可 (9:30-11:30 を除外)
  - 銘柄: 4568.T / 8306.T / 9433.T を主軸 (ロック候補に含まれる銘柄)
  - TP/SL バリエーション 3 通り (10/5, 8/4, 5/5)
  - cooldown_bars=5, max_trades_per_day=20

目的:
  - 9:30-11:30 を除外した時の合計 PnL がどう変わるか
  - 「銘柄 × TP/SL × 時間帯」の最適スイッチで本番投入準備
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
                "long_n": 0, "short_n": 0, "tp_hit_pct": None,
                "trades_per_day": 0, "days": None}
    wins = [t for t in result.trades if t.pnl > 0]
    losses = [t for t in result.trades if t.pnl <= 0]
    pf = (sum(t.pnl for t in wins) / -sum(t.pnl for t in losses)) if losses else 999.0
    tp_hits = sum(1 for t in result.trades if t.exit_reason == "take_profit")
    long_n = sum(1 for t in result.trades if t.side == "long")
    short_n = sum(1 for t in result.trades if t.side == "short")
    days = max(1, (df.index[-1].date() - df.index[0].date()).days + 1)
    return {
        "label": label, "symbol": symbol,
        "trades": n,
        "wr": round(len(wins) / n * 100, 1),
        "pf": round(pf, 2),
        "tp_hit_pct": round(tp_hits / n * 100, 1),
        "long_n": long_n, "short_n": short_n,
        "pnl_jpy": round(float(sum(t.pnl for t in result.trades)), 0),
        "days": days,
        "trades_per_day": round(n / days, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="4568.T,8306.T,9433.T,9984.T,3382.T,8136.T,1605.T,3103.T,6723.T")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="data/micro_scalp_v3_latest.json")
    args = ap.parse_args()

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"=== MicroScalp v3 (時間帯ロック版) === {len(syms)} symbols, {args.days}d")
    print("    時間帯: 09:00-09:30 + 12:30-15:00 のみ許可")

    configs = [
        # ロック候補ベース (9:00-9:30 + 12:30-15:00)
        ("v3_locked_10_5", {
            "tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 8.0,
            "atr_min_jpy": 3.0, "timeout_bars": 3,
            "cooldown_bars": 5, "max_trades_per_day": 20,
            "avoid_open_min": 0, "avoid_close_min": 30,
            "allowed_time_windows": ["09:00-09:30", "12:30-15:00"],
        }),
        ("v3_locked_8_4", {
            "tp_jpy": 8, "sl_jpy": 4, "entry_dev_jpy": 8.0,
            "atr_min_jpy": 3.0, "timeout_bars": 3,
            "cooldown_bars": 5, "max_trades_per_day": 20,
            "avoid_open_min": 0, "avoid_close_min": 30,
            "allowed_time_windows": ["09:00-09:30", "12:30-15:00"],
        }),
        # 9:00-9:30 のみ (寄り直後のみ)
        ("v3_open_only_10_5", {
            "tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 8.0,
            "atr_min_jpy": 3.0, "timeout_bars": 3,
            "cooldown_bars": 5, "max_trades_per_day": 20,
            "avoid_open_min": 0, "avoid_close_min": 30,
            "allowed_time_windows": ["09:00-09:30"],
        }),
        # 比較用: フィルタ無し (= grid v2 baseline)
        ("v2_no_filter_8_4", {
            "tp_jpy": 8, "sl_jpy": 4, "entry_dev_jpy": 8.0,
            "atr_min_jpy": 3.0, "timeout_bars": 3,
            "cooldown_bars": 5, "max_trades_per_day": 20,
            "avoid_open_min": 0, "avoid_close_min": 30,
            "allowed_time_windows": [],
        }),
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
            print(f"  [{label:<20}] n={r['trades']:>3} "
                  f"wr={r['wr'] if r['wr'] is not None else 0:>5}% "
                  f"pf={r['pf'] if r['pf'] is not None else 0:>5} "
                  f"trades/day={r['trades_per_day']:>5} "
                  f"pnl={r['pnl_jpy']:+8.0f}")
            results.append(r)

    # サマリ (label 別、銘柄合算)
    print("\n=== サマリ (label 別、銘柄合算) ===")
    by_label: dict = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0,
                                          "tp_hit": 0, "n_syms": 0})
    for r in results:
        if r["trades"] == 0:
            continue
        b = by_label[r["label"]]
        b["trades"] += r["trades"]
        b["wins"] += int(round((r["wr"] or 0) / 100 * r["trades"]))
        b["pnl"] += r["pnl_jpy"]
        b["tp_hit"] += int(round((r["tp_hit_pct"] or 0) / 100 * r["trades"]))
        b["n_syms"] += 1

    print(f'{"label":<22} {"n_syms":>7} {"trades":>7} {"WR%":>6} '
          f'{"TP_hit%":>8} {"PnL/7d":>10} {"PnL/day":>9}')
    for label, _ in configs:
        b = by_label[label]
        if b["trades"] == 0:
            print(f'{label:<22} {0:>7} {0:>7} {"-":>6} {"-":>8} {0:>10} {"-":>9}')
            continue
        wr = b["wins"] / b["trades"] * 100
        tp = b["tp_hit"] / b["trades"] * 100
        days = max(r["days"] for r in results if r["days"]) or 1
        print(f'{label:<22} {b["n_syms"]:>7} {b["trades"]:>7} {wr:>5.1f}% '
              f'{tp:>7.1f}% {b["pnl"]:>+10.0f} {b["pnl"]/days:>+8.0f}')

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
                "tp_hit_pct": (by_label[label]["tp_hit"] / by_label[label]["trades"] * 100)
                              if by_label[label]["trades"] else None,
                "pnl_jpy": by_label[label]["pnl"],
                "n_syms_active": by_label[label]["n_syms"],
            } for label, _ in configs
        }
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=> 結果保存: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
