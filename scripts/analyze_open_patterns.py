#!/usr/bin/env python3
"""寄り付きパターン分析 — ギャップ × 9:00-9:10 初動 × 9:10-9:30 挙動.

ユーザー提案 (2026-04-30 17:07):
  - 始値が前日終値から何 % 離れて始まるかでエントリー方向を決められる
  - 寄り天疑い (大幅 GU) → ショート優位
  - 同値スタート + 上昇トレンド継続 → ロング優位
  - 9:10 時点で +X% なら利益確定売りでショート優位

このスクリプトの目的:
  - 過去 7 日 × 9 銘柄で「寄りパターン → 9:10-9:30 の挙動」を集計
  - ギャップ別バケット (-1%超, -1〜-0.3%, ±0.3%, +0.3〜+1%, +1%超)
    × 9:00-9:10 初動 (続伸 / 反落 / 横ばい) で 9:10-9:30 のドリフトを測る
  - 「ショート優位」「ロング優位」「中立」を判定する閾値候補を抽出
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
    return df


def fetch_daily_prev_close(symbol: str, days: int = 14) -> dict:
    """日足から前日終値マップ {date: prev_close} を作る."""
    try:
        d = yf.download(symbol, period=f"{days}d", interval="1d",
                        auto_adjust=False, progress=False)
    except Exception:
        return {}
    if d is None or d.empty:
        return {}
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
    out: dict = {}
    closes = d["Close"].tolist() if "Close" in d.columns else d["close"].tolist()
    dates = [t.date() for t in d.index]
    for i in range(1, len(dates)):
        out[dates[i]] = float(closes[i - 1])
    return out


def gap_bucket(gap_pct: float) -> str:
    if gap_pct <= -1.0:
        return "GD_big"          # 大幅ギャップダウン
    if gap_pct <= -0.3:
        return "GD_mid"
    if gap_pct < 0.3:
        return "flat"
    if gap_pct < 1.0:
        return "GU_mid"
    return "GU_big"              # 大幅ギャップアップ


def init_dir(open_to_910: float) -> str:
    """9:00-9:10 の動き方向 (open に対する 9:10 close の %)."""
    if open_to_910 <= -0.3:
        return "down"
    if open_to_910 < 0.3:
        return "flat"
    return "up"


def analyze_day(sym: str, day: datetime.date, df_day: pd.DataFrame, prev_close: float) -> dict | None:
    """1 日分の 1m データから寄りパターンを抽出."""
    if df_day.empty or prev_close is None:
        return None

    # 9:00-9:30 の bar を抽出
    # yfinance 1m は 9:00 ジャストのバーが無い日が多いので、9 時台の最初のバーを始値に使う
    day_morning_all = df_day[df_day.index.hour == 9]
    if day_morning_all.empty:
        return None
    open_price = float(day_morning_all["open"].iloc[0])

    bars_900_910 = df_day[
        (df_day.index.hour == 9) & (df_day.index.minute < 10)
    ]
    bars_910_930 = df_day[
        (df_day.index.hour == 9) & (df_day.index.minute >= 10) & (df_day.index.minute < 30)
    ]
    bars_930_1500 = df_day[
        ((df_day.index.hour == 9) & (df_day.index.minute >= 30))
        | ((df_day.index.hour >= 10) & (df_day.index.hour < 15))
        | ((df_day.index.hour == 15) & (df_day.index.minute <= 25))
    ]
    if bars_900_910.empty or bars_910_930.empty:
        return None

    close_910 = float(bars_900_910["close"].iloc[-1])
    high_910 = float(bars_900_910["high"].max())
    low_910 = float(bars_900_910["low"].min())

    close_930 = float(bars_910_930["close"].iloc[-1])
    high_930 = float(bars_910_930["high"].max())
    low_930 = float(bars_910_930["low"].min())

    close_1500 = (
        float(bars_930_1500["close"].iloc[-1]) if not bars_930_1500.empty else close_930
    )

    gap_pct = (open_price - prev_close) / prev_close * 100
    drift_900_910_pct = (close_910 - open_price) / open_price * 100
    drift_910_930_pct = (close_930 - close_910) / close_910 * 100
    drift_900_1500_pct = (close_1500 - open_price) / open_price * 100

    # 9:10-9:30 の最大上昇 / 下落 (close_910 起点)
    max_up_910_930 = (high_930 - close_910) / close_910 * 100
    max_dn_910_930 = (low_930 - close_910) / close_910 * 100

    return {
        "symbol": sym,
        "date": str(day),
        "prev_close": round(prev_close, 1),
        "open": round(open_price, 1),
        "gap_pct": round(gap_pct, 2),
        "gap_bucket": gap_bucket(gap_pct),
        "close_910": round(close_910, 1),
        "drift_900_910_pct": round(drift_900_910_pct, 2),
        "init_dir_910": init_dir(drift_900_910_pct),
        "close_930": round(close_930, 1),
        "drift_910_930_pct": round(drift_910_930_pct, 2),
        "max_up_910_930_pct": round(max_up_910_930, 2),
        "max_dn_910_930_pct": round(max_dn_910_930, 2),
        "close_1500": round(close_1500, 1),
        "drift_900_1500_pct": round(drift_900_1500_pct, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols",
                    default="4568.T,8306.T,9433.T,3103.T,6723.T,8136.T,3382.T,9984.T,6758.T")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="data/micro_scalp_open_patterns.json")
    args = ap.parse_args()

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"=== 寄り付きパターン分析 === {len(syms)} symbols, {args.days}d")

    rows: list[dict] = []
    for sym in syms:
        df1m = fetch_1m(sym, days=args.days)
        if df1m is None or df1m.empty:
            print(f"  [{sym}] no 1m data")
            continue
        prev_close_map = fetch_daily_prev_close(sym, days=args.days + 7)
        days_in_data = sorted({d for d in df1m.index.date})
        events = []
        for day in days_in_data:
            df_day = df1m[df1m.index.date == day]
            prev = prev_close_map.get(day)
            if prev is None:
                continue
            r = analyze_day(sym, day, df_day, prev)
            if r is None:
                continue
            events.append(r)
        rows.extend(events)
        if events:
            print(f"  [{sym}] {len(events)} day events (gap range "
                  f"{min(e['gap_pct'] for e in events):+.2f}% ~ "
                  f"{max(e['gap_pct'] for e in events):+.2f}%)")

    if not rows:
        print("\n!! no events collected, abort !!")
        return

    # ── サマリ 1: ギャップ × 初動 別の 9:10-9:30 平均ドリフト ────────────
    print("\n=== ギャップ × 9:00-9:10 初動 × 9:10-9:30 ドリフト平均 ===")
    print(f'{"gap":<8} {"init_910":<8} {"n":>4} '
          f'{"drift_910_930%":>15} {"hit_short_>=0.2":>17} {"hit_long_>=0.2":>16}')
    summary: dict = defaultdict(lambda: {"n": 0, "drift_sum": 0.0,
                                          "hit_short": 0, "hit_long": 0,
                                          "max_up_sum": 0.0, "max_dn_sum": 0.0})
    for r in rows:
        key = (r["gap_bucket"], r["init_dir_910"])
        s = summary[key]
        s["n"] += 1
        s["drift_sum"] += r["drift_910_930_pct"]
        s["max_up_sum"] += r["max_up_910_930_pct"]
        s["max_dn_sum"] += r["max_dn_910_930_pct"]
        if r["drift_910_930_pct"] <= -0.2:
            s["hit_short"] += 1
        if r["drift_910_930_pct"] >= 0.2:
            s["hit_long"] += 1
    sorted_keys = sorted(summary.keys(), key=lambda k: (k[0], k[1]))
    for (gap, init), s in ((k, summary[k]) for k in sorted_keys):
        if s["n"] == 0:
            continue
        avg_drift = s["drift_sum"] / s["n"]
        print(f'{gap:<8} {init:<8} {s["n"]:>4} '
              f'{avg_drift:>+14.2f}% '
              f'{s["hit_short"]/s["n"]*100:>15.1f}% '
              f'{s["hit_long"]/s["n"]*100:>15.1f}%')

    # ── サマリ 2: ギャップだけ別 (= init を統合) ──────────────────────────
    print("\n=== ギャップ別の 9:10-9:30 ドリフト平均 (全初動合算) ===")
    print(f'{"gap":<10} {"n":>4} {"drift_910_930%":>15} '
          f'{"max_up_910_930%":>16} {"max_dn_910_930%":>16} '
          f'{"hit_short":>10} {"hit_long":>10}')
    by_gap: dict = defaultdict(lambda: {"n": 0, "drift_sum": 0.0,
                                         "max_up_sum": 0.0, "max_dn_sum": 0.0,
                                         "hit_short": 0, "hit_long": 0})
    for r in rows:
        b = by_gap[r["gap_bucket"]]
        b["n"] += 1
        b["drift_sum"] += r["drift_910_930_pct"]
        b["max_up_sum"] += r["max_up_910_930_pct"]
        b["max_dn_sum"] += r["max_dn_910_930_pct"]
        if r["drift_910_930_pct"] <= -0.2:
            b["hit_short"] += 1
        if r["drift_910_930_pct"] >= 0.2:
            b["hit_long"] += 1
    for gap in ["GD_big", "GD_mid", "flat", "GU_mid", "GU_big"]:
        b = by_gap.get(gap)
        if not b or b["n"] == 0:
            continue
        print(f'{gap:<10} {b["n"]:>4} '
              f'{b["drift_sum"]/b["n"]:>+14.2f}% '
              f'{b["max_up_sum"]/b["n"]:>+15.2f}% '
              f'{b["max_dn_sum"]/b["n"]:>+15.2f}% '
              f'{b["hit_short"]/b["n"]*100:>9.1f}% '
              f'{b["hit_long"]/b["n"]*100:>9.1f}%')

    # ── サマリ 3: 寄り天 / トレンド継続の判定 ──────────────────────────
    print("\n=== 寄り天 / 続伸 判定 (9:00-9:30 が当日レンジの上限/下限) ===")
    yoritenmaru = 0
    yorisokoaru = 0
    trend_up = 0
    trend_down = 0
    for r in rows:
        # 9:30 高値 = 当日全体高値に近いなら寄り天傾向
        if r["max_up_910_930_pct"] > 0 and r["drift_900_1500_pct"] < r["drift_900_910_pct"] - 0.1:
            yoritenmaru += 1
        elif r["drift_900_1500_pct"] > 0.5:
            trend_up += 1
        elif r["drift_900_1500_pct"] < -0.5:
            trend_down += 1
    print(f"  寄り天 (9:30 で天井 → ズルズル下げ) : {yoritenmaru} / {len(rows)}")
    print(f"  上昇トレンド継続 (9:00→15:00 +0.5%超): {trend_up} / {len(rows)}")
    print(f"  下降トレンド継続 (9:00→15:00 -0.5%超): {trend_down} / {len(rows)}")

    # ── 出力 JSON ────────────────────────────────────────────────────────
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "symbols": syms, "days": args.days,
        "events": rows,
        "summary_by_gap_init": {
            f"{k[0]}|{k[1]}": {
                "n": s["n"],
                "avg_drift_910_930_pct": round(s["drift_sum"] / s["n"], 3),
                "avg_max_up_910_930_pct": round(s["max_up_sum"] / s["n"], 3),
                "avg_max_dn_910_930_pct": round(s["max_dn_sum"] / s["n"], 3),
                "hit_short_pct": round(s["hit_short"] / s["n"] * 100, 1),
                "hit_long_pct": round(s["hit_long"] / s["n"] * 100, 1),
            } for k, s in summary.items() if s["n"] > 0
        },
        "summary_by_gap": {
            gap: {
                "n": b["n"],
                "avg_drift_910_930_pct": round(b["drift_sum"] / b["n"], 3),
                "avg_max_up_910_930_pct": round(b["max_up_sum"] / b["n"], 3),
                "avg_max_dn_910_930_pct": round(b["max_dn_sum"] / b["n"], 3),
                "hit_short_pct": round(b["hit_short"] / b["n"] * 100, 1),
                "hit_long_pct": round(b["hit_long"] / b["n"] * 100, 1),
            } for gap, b in by_gap.items() if b["n"] > 0
        },
        "headline": {
            "events_total": len(rows),
            "yoriten_count": yoritenmaru,
            "trend_up_count": trend_up,
            "trend_down_count": trend_down,
        },
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=> 結果保存: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
