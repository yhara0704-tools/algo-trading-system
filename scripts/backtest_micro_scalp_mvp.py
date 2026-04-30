#!/usr/bin/env python3
"""MicroScalp MVP backtest — 1m yfinance データで「+5円固定スキャル」の生存可能性を測る.

ユーザー提案 (2026-04-30): 三菱UFJ e スマート/松井のデイトレ信用 (手数料 0 円)
を前提に「+5円固定 / 1分以内 / 即損切り」を 1 日 20 回繰り返して +10,000 円/日。

このスクリプトは MVP 段階の評価:
  - yfinance 1m データ (過去 7 日, 単発 download) で複数銘柄を backtest
  - 取引数 / WR / PF / 平均保有時間 / +5円 TP 到達率 / 1日平均取引数 を JSON 出力
  - WR>=60% & 1 銘柄あたり 1 日 4 回以上の取引が見込めるか判定
  - パラメータ感度 (entry_dev_jpy / atr_min_jpy / timeout_bars) を 3-4 通り試す

注意:
  - yfinance 1m データは過去 7 日制限 (= 5 営業日程度)。サンプル少のため
    結論は仮説段階。WR と「シグナル発生数の桁感」を見るのが目的。
  - 本格評価は J-Quants Premium で 30-90 日の 1m を取れたら実施。
  - 手数料 0 円前提 (`fee_pct=0.0`)、信用倍率 3.3 で `starting_cash=990,000`。
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
    """yfinance で 1m データ取得 + JST tz 統一 + ザラ場フィルタ."""
    try:
        df = yf.download(symbol, period=f"{days}d", interval="1m",
                         auto_adjust=False, progress=False)
    except Exception as exc:
        print(f"  [{symbol}] fetch err: {exc}")
        return None
    if df is None or df.empty:
        return None
    # MultiIndex columns -> flat
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    df = df[["open", "high", "low", "close", "volume"]]
    df = df.dropna()
    if df.empty:
        return None
    # tz: yfinance は UTC で来る → JST 化
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(JST)
    # ザラ場 9:00 ~ 15:30 (前場 11:30-12:30 自動的に空)
    hh = df.index.hour
    mm = df.index.minute
    in_morning = ((hh == 9) & (mm >= 0)) | ((hh == 10)) | ((hh == 11) & (mm <= 30))
    in_afternoon = ((hh == 12) & (mm >= 30)) | ((hh == 13)) | ((hh == 14)) | ((hh == 15) & (mm <= 30))
    df = df[in_morning | in_afternoon]
    return df


def evaluate(symbol: str, df: pd.DataFrame, params: dict, label: str) -> dict:
    name = symbol.replace(".T", "")
    strat = create_strategy("MicroScalp", symbol, name=name, params=params)
    result = run_backtest(
        strat, df,
        starting_cash=990_000.0,   # 信用 99 万 (元本 30万 × 3.3)
        fee_pct=0.0,
        position_pct=0.30,         # ユーザー提案の余力 30%
        usd_jpy=1.0,
        lot_size=100,
        limit_slip_pct=0.001,      # 1m なので タイト
        latency_bars=0,
        eod_close_time=(15, 25),
        subsession_cooldown_min=2, # MicroScalp 用に短く
        daily_loss_limit_pct=-3.0, # 日次 3% 損失で 1 日停止 (元本 30万 ベース)
    )
    n = len(result.trades)
    if n == 0:
        return {
            "label": label, "symbol": symbol, "params": params,
            "trades": 0, "wr": None, "pf": None,
            "avg_hold_min": None, "tp_hit_pct": None,
            "long_n": 0, "short_n": 0,
            "total_pnl_jpy": 0.0, "avg_pnl_jpy": None,
            "days": None, "trades_per_day": 0.0,
        }
    wins = [t for t in result.trades if t.pnl > 0]
    losses = [t for t in result.trades if t.pnl <= 0]
    wr = len(wins) / n * 100
    pf = (sum(t.pnl for t in wins) / -sum(t.pnl for t in losses)) if losses else 999.0
    avg_hold = sum(t.duration_bars for t in result.trades) / n  # 1m なので bars=分
    tp_hits = sum(1 for t in result.trades if t.exit_reason == "take_profit")
    long_n = sum(1 for t in result.trades if t.side == "long")
    short_n = sum(1 for t in result.trades if t.side == "short")
    total_pnl = sum(t.pnl for t in result.trades)
    days = max(1, (df.index[-1].date() - df.index[0].date()).days + 1)
    return {
        "label": label, "symbol": symbol, "params": params,
        "trades": n,
        "wr": round(wr, 1),
        "pf": round(pf, 2),
        "avg_hold_min": round(avg_hold, 2),
        "tp_hit_pct": round(tp_hits / n * 100, 1),
        "long_n": long_n, "short_n": short_n,
        "total_pnl_jpy": round(float(total_pnl), 0),
        "avg_pnl_jpy": round(float(total_pnl) / n, 1),
        "days": days,
        "trades_per_day": round(n / days, 2),
        "exit_reasons": dict(_count_exit_reasons(result.trades)),
    }


def _count_exit_reasons(trades) -> dict:
    c = defaultdict(int)
    for t in trades:
        c[t.exit_reason or "unknown"] += 1
    return c


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--symbols",
        default="9984.T,8306.T,7203.T,6758.T,6723.T,4385.T,4568.T,9433.T,3382.T,8136.T,1605.T,3103.T",
        help="カンマ区切り",
    )
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="data/micro_scalp_mvp_latest.json")
    args = ap.parse_args()

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"=== MicroScalp MVP backtest === {len(syms)} symbols, {args.days} 日分 1m データ")

    # v2 改善版: cooldown / max_trades_per_day を追加して連発擬陽性を抑制
    configs = [
        # v1 ベースライン (cooldown=0)
        ("v1_baseline", {"tp_jpy": 5, "sl_jpy": 5, "entry_dev_jpy": 8.0,
                          "atr_min_jpy": 3.0, "timeout_bars": 2,
                          "cooldown_bars": 0, "max_trades_per_day": 0}),
        # v2-A: 損小利大 (TP=8, SL=4) + cooldown 5min + max 20/日
        ("v2_8_4_cd5", {"tp_jpy": 8, "sl_jpy": 4, "entry_dev_jpy": 8.0,
                          "atr_min_jpy": 3.0, "timeout_bars": 3,
                          "cooldown_bars": 5, "max_trades_per_day": 20}),
        # v2-B: 損小利大 + ATR レンジ (3-15円) + cooldown 5min
        ("v2_atr_band", {"tp_jpy": 8, "sl_jpy": 4, "entry_dev_jpy": 8.0,
                          "atr_min_jpy": 3.0, "atr_max_jpy": 15.0,
                          "timeout_bars": 3, "cooldown_bars": 5,
                          "max_trades_per_day": 20}),
        # v2-C: タイトエントリー (dev=12) + 大き目 TP/SL (10/5)
        ("v2_tight_10_5", {"tp_jpy": 10, "sl_jpy": 5, "entry_dev_jpy": 12.0,
                          "atr_min_jpy": 4.0, "timeout_bars": 4,
                          "cooldown_bars": 5, "max_trades_per_day": 15}),
    ]

    results = []
    for sym in syms:
        print(f"\n--- {sym} ---")
        df = fetch_1m(sym, days=args.days)
        if df is None or df.empty:
            print(f"  [{sym}] no data, skip")
            continue
        print(f"  rows={len(df)} range={df.index[0]} ~ {df.index[-1]}")
        for label, p in configs:
            r = evaluate(sym, df, p, label)
            print(
                f"  [{label:<8}] n={r['trades']:>3} "
                f"wr={r['wr'] if r['wr'] is not None else 0:>5}% "
                f"pf={r['pf'] if r['pf'] is not None else 0:>5} "
                f"avg_hold={r['avg_hold_min'] if r['avg_hold_min'] is not None else 0:>4}m "
                f"tp_hit={r['tp_hit_pct'] if r['tp_hit_pct'] is not None else 0:>5}% "
                f"trades/day={r['trades_per_day']:>5} "
                f"total_pnl={r['total_pnl_jpy']:+.0f}円"
            )
            results.append(r)

    # サマリ集計 (label 別)
    print("\n=== サマリ (label 別、銘柄合算) ===")
    by_label = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "tp_hit": 0, "n_syms_active": 0})
    for r in results:
        if r["trades"] == 0:
            continue
        b = by_label[r["label"]]
        b["trades"] += r["trades"]
        b["wins"] += int(round(r["wr"] / 100 * r["trades"])) if r["wr"] else 0
        b["pnl"] += r["total_pnl_jpy"]
        b["tp_hit"] += int(round((r["tp_hit_pct"] or 0) / 100 * r["trades"]))
        b["n_syms_active"] += 1
    print(f'{"label":<10} {"n_syms":>7} {"trades":>7} {"wr%":>6} {"tp_hit%":>8} {"total_pnl":>11} {"avg/day":>8}')
    for label, _ in configs:
        b = by_label[label]
        if b["trades"] == 0:
            print(f'{label:<10} {0:>7} {0:>7} {"-":>6} {"-":>8} {0:>+11} {"-":>8}')
            continue
        wr = b["wins"] / b["trades"] * 100
        tp = b["tp_hit"] / b["trades"] * 100
        # 平均 trades/day/銘柄
        days = max(1, (max(d for d in [r["days"] for r in results if r["days"]]) or 1))
        avg_per_day_per_sym = b["trades"] / b["n_syms_active"] / days if b["n_syms_active"] else 0
        print(
            f'{label:<10} {b["n_syms_active"]:>7} {b["trades"]:>7} '
            f'{wr:>5.1f}% {tp:>7.1f}% {b["pnl"]:>+11.0f}円 {avg_per_day_per_sym:>5.1f}/日'
        )

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "symbols": syms,
        "days": args.days,
        "configs": configs,
        "results": results,
        "summary": {
            label: {
                "trades": by_label[label]["trades"],
                "wr_pct": (by_label[label]["wins"] / by_label[label]["trades"] * 100) if by_label[label]["trades"] else None,
                "tp_hit_pct": (by_label[label]["tp_hit"] / by_label[label]["trades"] * 100) if by_label[label]["trades"] else None,
                "total_pnl_jpy": by_label[label]["pnl"],
                "n_syms_active": by_label[label]["n_syms_active"],
            }
            for label, _ in configs
        },
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=> 結果保存: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
