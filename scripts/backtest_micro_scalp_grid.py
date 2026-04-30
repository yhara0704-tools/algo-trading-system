#!/usr/bin/env python3
"""MicroScalp グリッドサーチ — 銘柄 × TP値幅 × 時間帯 で最適設定を探索.

ユーザー提案 (2026-04-30 16:53):
  - 銘柄ごとに最適 TP は違うはず (株価帯 / ボラ依存)
  - +3円 〜 +10円 で WR を比較したい
  - 9:00-9:30 (寄り直後) は別の挙動を見せる可能性あり
    (オープニング・ボラティリティで MicroScalp 戦略が異常に効くor効かない)

評価軸:
  - 12 銘柄 (yfinance 1m, 過去 7 日)
  - TP/SL 6 通り: (3,3) (4,3) (5,3) (5,5) (8,4) (10,5)
  - 時間帯 4 バケット:
      morning_open  09:00-09:30 (寄り直後)
      morning_mid   09:30-11:30 (前場後半)
      afternoon     12:30-15:00 (後場)
      closing       15:00-15:25 (大引け前)
  - 各 (銘柄, TP/SL, 時間帯) の trades / WR / PF / PnL を集計
  - PnL Top 20 + WR>=55% かつ trades>=10 をロック銘柄として推奨

backtest 構成:
  - cooldown_bars=5 / max_trades_per_day=20 / atr_min_jpy=3.0
    (v2_8_4_cd5 で実証済の連発擬陽性抑制パラメータをベースライン)
  - avoid_open_min=0 (= 9:00 直後も評価対象。実本番で除外したい場合は別途設定)
  - position_pct=0.30 (余力 30%)
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

# (TP, SL) のグリッド
TP_SL_GRID: list[tuple[float, float]] = [
    (3.0, 3.0),
    (4.0, 3.0),
    (5.0, 3.0),
    (5.0, 5.0),
    (8.0, 4.0),
    (10.0, 5.0),
]

# 時間帯バケット (h, m_start, m_end の半開区間)
TIME_BUCKETS: list[tuple[str, tuple[int, int], tuple[int, int]]] = [
    ("morning_open",  (9, 0),   (9, 30)),
    ("morning_mid",   (9, 30),  (11, 30)),
    ("afternoon",     (12, 30), (15, 0)),
    ("closing",       (15, 0),  (15, 25)),
]


def fetch_1m(symbol: str, days: int = 7) -> pd.DataFrame | None:
    try:
        df = yf.download(symbol, period=f"{days}d", interval="1m",
                         auto_adjust=False, progress=False)
    except Exception as exc:
        print(f"  [{symbol}] fetch err: {exc}")
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
    if df.empty:
        return None
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(JST)
    hh, mm = df.index.hour, df.index.minute
    in_morning = ((hh == 9) & (mm >= 0)) | ((hh == 10)) | ((hh == 11) & (mm <= 30))
    in_afternoon = ((hh == 12) & (mm >= 30)) | ((hh == 13)) | ((hh == 14)) | ((hh == 15) & (mm <= 30))
    df = df[in_morning | in_afternoon]
    return df


def in_bucket(ts_str: str, b_start: tuple[int, int], b_end: tuple[int, int]) -> bool:
    """ts_str (e.g. '2026-04-25 09:15:00+09:00') が時間帯バケット内か."""
    try:
        ts = pd.Timestamp(ts_str)
    except Exception:
        return False
    if ts.tz is not None:
        ts = ts.tz_convert(JST)
    hh, mm = ts.hour, ts.minute
    cur = hh * 60 + mm
    s = b_start[0] * 60 + b_start[1]
    e = b_end[0] * 60 + b_end[1]
    return s <= cur < e


def aggregate_trades(trades: list, bucket_start: tuple[int, int], bucket_end: tuple[int, int]) -> dict:
    sel = [t for t in trades if in_bucket(str(t.entry_time), bucket_start, bucket_end)]
    n = len(sel)
    if n == 0:
        return {"trades": 0, "wr": None, "pf": None, "pnl_jpy": 0.0,
                "tp_hit_pct": None, "avg_hold_min": None,
                "long_n": 0, "short_n": 0}
    wins = [t for t in sel if t.pnl > 0]
    losses = [t for t in sel if t.pnl <= 0]
    wr = len(wins) / n * 100
    pf = (sum(t.pnl for t in wins) / -sum(t.pnl for t in losses)) if losses else 999.0
    tp_hits = sum(1 for t in sel if t.exit_reason == "take_profit")
    long_n = sum(1 for t in sel if t.side == "long")
    short_n = sum(1 for t in sel if t.side == "short")
    avg_hold = sum(t.duration_bars for t in sel) / n
    pnl = sum(t.pnl for t in sel)
    return {
        "trades": n, "wr": round(wr, 1), "pf": round(pf, 2),
        "pnl_jpy": round(float(pnl), 0),
        "tp_hit_pct": round(tp_hits / n * 100, 1),
        "avg_hold_min": round(avg_hold, 2),
        "long_n": long_n, "short_n": short_n,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--symbols",
        default="9984.T,8306.T,7203.T,6758.T,6723.T,4385.T,4568.T,9433.T,3382.T,8136.T,1605.T,3103.T",
    )
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="data/micro_scalp_grid_latest.json")
    args = ap.parse_args()

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"=== MicroScalp grid backtest === {len(syms)} symbols × {len(TP_SL_GRID)} TP/SL × {len(TIME_BUCKETS)} buckets")
    print(f"    days={args.days} (yfinance 1m)")

    rows: list[dict] = []
    summary_per_bucket: dict[tuple[float, float, str], dict] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "tp_hit": 0, "n_syms_active": set()}
    )

    for sym in syms:
        df = fetch_1m(sym, days=args.days)
        if df is None or df.empty:
            print(f"\n--- {sym} (no data) ---")
            continue
        print(f"\n--- {sym} (rows={len(df)}) ---")
        for tp_jpy, sl_jpy in TP_SL_GRID:
            params = {
                "tp_jpy": tp_jpy, "sl_jpy": sl_jpy,
                "entry_dev_jpy": 8.0, "atr_period": 10, "atr_min_jpy": 3.0,
                "timeout_bars": 3, "cooldown_bars": 5,
                "avoid_open_min": 0,  # ← 9:00-9:30 も評価対象に含める
                "avoid_close_min": 30,
                "morning_only": False, "allow_short": True,
                "max_trades_per_day": 30,
            }
            strat = create_strategy("MicroScalp", sym, name=sym.replace(".T", ""), params=params)
            result = run_backtest(
                strat, df,
                starting_cash=990_000.0, fee_pct=0.0,
                position_pct=0.30, usd_jpy=1.0, lot_size=100,
                limit_slip_pct=0.001, eod_close_time=(15, 25),
                subsession_cooldown_min=2, daily_loss_limit_pct=-3.0,
            )
            for bucket_name, bs, be in TIME_BUCKETS:
                agg = aggregate_trades(result.trades, bs, be)
                row = {
                    "symbol": sym,
                    "tp_jpy": tp_jpy, "sl_jpy": sl_jpy,
                    "bucket": bucket_name,
                    **agg,
                }
                rows.append(row)
                if agg["trades"] > 0:
                    s = summary_per_bucket[(tp_jpy, sl_jpy, bucket_name)]
                    s["trades"] += agg["trades"]
                    s["wins"] += int(round((agg["wr"] or 0) / 100 * agg["trades"]))
                    s["pnl"] += agg["pnl_jpy"]
                    s["tp_hit"] += int(round((agg["tp_hit_pct"] or 0) / 100 * agg["trades"]))
                    s["n_syms_active"].add(sym)

            # 銘柄ごとの簡易表示 (TP/SL 単位、全時間帯合算)
            all_n = sum(r["trades"] for r in rows
                        if r["symbol"] == sym and r["tp_jpy"] == tp_jpy and r["sl_jpy"] == sl_jpy)
            all_pnl = sum(r["pnl_jpy"] for r in rows
                          if r["symbol"] == sym and r["tp_jpy"] == tp_jpy and r["sl_jpy"] == sl_jpy)
            print(f"  TP={tp_jpy:>4}/SL={sl_jpy:<4} n={all_n:>3} pnl={all_pnl:+8.0f}")

    # ── サマリ 1: 時間帯バケット別 (TP/SL 別、銘柄合算) ──────────────────────
    print("\n=== 時間帯バケット別 (TP/SL 別、銘柄合算) ===")
    print(f'{"TP/SL":<7} {"bucket":<14} {"n_syms":>7} {"trades":>7} {"WR%":>6} {"TP_hit%":>8} {"PnL":>10}')
    sorted_keys = sorted(summary_per_bucket.keys(),
                         key=lambda k: (-summary_per_bucket[k]["pnl"]))
    for (tp, sl, bucket), s in sorted_keys[:80].__iter__() if False else \
            ((k, summary_per_bucket[k]) for k in sorted_keys):
        if s["trades"] == 0:
            continue
        wr = s["wins"] / s["trades"] * 100
        tp_hit = s["tp_hit"] / s["trades"] * 100
        print(f'{tp:>3}/{sl:<3} {bucket:<14} {len(s["n_syms_active"]):>7} '
              f'{s["trades"]:>7} {wr:>5.1f}% {tp_hit:>7.1f}% {s["pnl"]:>+10.0f}')

    # ── サマリ 2: (銘柄, TP/SL, 時間帯) の Top 20 PnL ────────────────────────
    print("\n=== Top 20 (銘柄, TP/SL, 時間帯) PnL 降順 ===")
    rows_sorted = sorted([r for r in rows if r["trades"] >= 5],
                         key=lambda r: -r["pnl_jpy"])[:20]
    print(f'{"sym":<8} {"TP/SL":<7} {"bucket":<14} {"n":>4} {"WR%":>6} {"PF":>6} {"PnL":>9} {"TP_hit%":>8}')
    for r in rows_sorted:
        wr = r["wr"] or 0
        pf = r["pf"] or 0
        tp_hit = r["tp_hit_pct"] or 0
        print(f'{r["symbol"]:<8} {r["tp_jpy"]:>3}/{r["sl_jpy"]:<3} {r["bucket"]:<14} '
              f'{r["trades"]:>4} {wr:>5.1f}% {pf:>5.2f} {r["pnl_jpy"]:>+9.0f} {tp_hit:>7.1f}%')

    # ── サマリ 3: WR>=55 + trades>=10 の (銘柄, TP/SL, 時間帯) ──────────────
    print("\n=== ロック候補 (WR>=55% & trades>=10 & PnL>0) ===")
    locked = [r for r in rows
              if r["trades"] >= 10
              and (r["wr"] or 0) >= 55.0
              and r["pnl_jpy"] > 0]
    locked.sort(key=lambda r: -r["pnl_jpy"])
    if not locked:
        print("  (該当なし)")
    else:
        print(f'{"sym":<8} {"TP/SL":<7} {"bucket":<14} {"n":>4} {"WR%":>6} {"PF":>6} {"PnL":>9}')
        for r in locked:
            print(f'{r["symbol"]:<8} {r["tp_jpy"]:>3}/{r["sl_jpy"]:<3} {r["bucket"]:<14} '
                  f'{r["trades"]:>4} {r["wr"]:>5.1f}% {r["pf"]:>5.2f} {r["pnl_jpy"]:>+9.0f}')

    # ── 出力 JSON ───────────────────────────────────────────────────────────
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    summary_serializable = {}
    for (tp, sl, bucket), s in summary_per_bucket.items():
        if s["trades"] == 0:
            continue
        summary_serializable[f"{tp}/{sl}|{bucket}"] = {
            "tp_jpy": tp, "sl_jpy": sl, "bucket": bucket,
            "n_syms_active": sorted(s["n_syms_active"]),
            "trades": s["trades"],
            "wr_pct": round(s["wins"] / s["trades"] * 100, 1),
            "tp_hit_pct": round(s["tp_hit"] / s["trades"] * 100, 1),
            "pnl_jpy": s["pnl"],
        }

    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "symbols": syms, "days": args.days,
        "tp_sl_grid": TP_SL_GRID,
        "time_buckets": [{"name": n, "start": s, "end": e} for n, s, e in TIME_BUCKETS],
        "rows": rows,
        "summary_per_bucket": summary_serializable,
        "locked_candidates": locked,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=> 結果保存: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
