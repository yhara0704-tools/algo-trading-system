#!/usr/bin/env python3
"""銘柄別寄り付きプロファイル — observe_min ごとの予測力を可視化.

ユーザー提案 (2026-04-30 17:17):
  - 9:00-9:10 で動き切る銘柄もあれば、9:00-9:15 までかかる銘柄もある
  - 銘柄別の癖をデータとして持っておくべき

出力 (`data/symbol_open_profile.json`):
  各銘柄について:
    - n_days: サンプル数
    - gap_stats: ギャップの平均 / std / min / max (絶対値含む)
    - observe_predict: 各 observe_min (3/5/8/10/15/20) で
        「初動方向 → 9:N-9:30 ドリフト」の方向一致率と相関係数
    - yoriten_pct: 寄り天 (= 9:30 が当日高値の 95% 以上) の発生率
    - vol_decay: 9:00 → 9:N の累積価格変動標準偏差 (ボラ持続)
    - best_observe_min: 予測力 (一致率) が最も高い observe_min
    - bias_template: 銘柄別の推奨バイアス決定ルール
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

JST = "Asia/Tokyo"

OBSERVE_MINS = [3, 5, 8, 10, 15, 20]


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
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                              "Close": "close", "Volume": "volume"})
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(JST)
    return df


def fetch_prev_close_map(symbol: str, days: int = 14) -> dict:
    try:
        d = yf.download(symbol, period=f"{days}d", interval="1d",
                        auto_adjust=False, progress=False)
    except Exception:
        return {}
    if d is None or d.empty:
        return {}
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
    closes = d["Close"].tolist() if "Close" in d.columns else d["close"].tolist()
    dates = [t.date() for t in d.index]
    return {dates[i]: float(closes[i - 1]) for i in range(1, len(dates))}


def analyze_symbol(symbol: str, days: int = 7) -> dict | None:
    df1m = fetch_1m(symbol, days=days)
    if df1m is None or df1m.empty:
        return None
    prev_map = fetch_prev_close_map(symbol, days=days + 7)
    days_in_data = sorted({d for d in df1m.index.date})

    day_records: list[dict] = []
    for day in days_in_data:
        df_day = df1m[df1m.index.date == day]
        prev_close = prev_map.get(day)
        if prev_close is None:
            continue
        morning = df_day[df_day.index.hour == 9]
        if morning.empty:
            continue
        open_price = float(morning["open"].iloc[0])
        gap_pct = (open_price - prev_close) / prev_close * 100

        # 当日 9:00 ~ 15:25 の高値 / 安値
        intraday = df_day[
            ((df_day.index.hour >= 9) & (df_day.index.hour < 15))
            | ((df_day.index.hour == 15) & (df_day.index.minute <= 25))
        ]
        if intraday.empty:
            continue
        day_high = float(intraday["high"].max())
        day_low = float(intraday["low"].min())

        # 9:00 から各観察分後の close および 9:00-9:N の最大変動幅
        rec: dict = {
            "date": str(day),
            "prev_close": prev_close,
            "open": open_price,
            "gap_pct": gap_pct,
            "day_high": day_high,
            "day_low": day_low,
            "observe": {},
        }
        for n in OBSERVE_MINS + [30]:
            cutoff_min = 9 * 60 + n
            sub = df_day[
                (df_day.index.hour == 9)
                & ((df_day.index.hour * 60 + df_day.index.minute) <= cutoff_min)
            ]
            if sub.empty:
                continue
            close_at_n = float(sub["close"].iloc[-1])
            high_at_n = float(sub["high"].max())
            low_at_n = float(sub["low"].min())
            rec["observe"][n] = {
                "close": close_at_n,
                "drift_pct": (close_at_n - open_price) / open_price * 100,
                "max_up_pct": (high_at_n - open_price) / open_price * 100,
                "max_dn_pct": (low_at_n - open_price) / open_price * 100,
            }

        # 9:N-9:30 の平均ドリフト (= 予測対象)
        for n in OBSERVE_MINS:
            ob = rec["observe"].get(n)
            ob30 = rec["observe"].get(30)
            if ob and ob30:
                rec.setdefault("post", {})[n] = {
                    "drift_to_930_pct": ob30["drift_pct"] - ob["drift_pct"],
                }
        day_records.append(rec)

    if not day_records:
        return None

    # ── 統計化 ─────────────────────────────────────────────────────────
    gaps = np.array([r["gap_pct"] for r in day_records])
    gap_stats = {
        "mean": round(float(gaps.mean()), 3),
        "std": round(float(gaps.std()), 3),
        "abs_mean": round(float(np.abs(gaps).mean()), 3),
        "min": round(float(gaps.min()), 3),
        "max": round(float(gaps.max()), 3),
    }

    # observe_min 別予測力
    observe_predict: dict = {}
    for n in OBSERVE_MINS:
        init_drifts = []
        post_drifts = []
        for r in day_records:
            ob = r["observe"].get(n)
            post = r.get("post", {}).get(n)
            if ob and post:
                init_drifts.append(ob["drift_pct"])
                post_drifts.append(post["drift_to_930_pct"])
        if len(init_drifts) < 2:
            observe_predict[n] = {"sample": len(init_drifts), "corr": None,
                                   "same_dir_pct": None}
            continue
        init_arr = np.array(init_drifts)
        post_arr = np.array(post_drifts)
        # 「初動と 9:N-9:30 ドリフトが同方向か」 (= トレンド継続の確率)
        same_dir = np.sum(np.sign(init_arr) * np.sign(post_arr) > 0)
        non_zero = np.sum((np.abs(init_arr) > 0.05) & (np.abs(post_arr) > 0.05))
        # 相関係数
        if init_arr.std() > 1e-9 and post_arr.std() > 1e-9:
            corr = float(np.corrcoef(init_arr, post_arr)[0, 1])
        else:
            corr = None
        observe_predict[n] = {
            "sample": len(init_drifts),
            "corr": round(corr, 3) if corr is not None else None,
            "same_dir_pct": round(same_dir / len(init_drifts) * 100, 1),
            "non_zero_sample": int(non_zero),
        }

    # 寄り天率 (= 9:30 高値が当日高値の 95% 以上で、9:30 以降は下げ)
    yoriten = 0
    for r in day_records:
        ob30 = r["observe"].get(30)
        if not ob30:
            continue
        # 9:30 までの高値 (open との比) を当日全体高値と比較
        max_up_to_930 = ob30["max_up_pct"]
        full_max_up = (r["day_high"] - r["open"]) / r["open"] * 100
        # 9:30 までの高値が当日全体高値の 95% 以上 = 寄り天
        if full_max_up > 0 and max_up_to_930 / full_max_up >= 0.95 and ob30["drift_pct"] >= 0.1:
            yoriten += 1
    yoriten_pct = round(yoriten / len(day_records) * 100, 1)

    # ボラ減衰 (= 9:00 から N 分の (max_up - max_dn) の平均 = 1本あたりレンジ %)
    vol_decay = {}
    for n in OBSERVE_MINS + [30]:
        ranges = []
        for r in day_records:
            ob = r["observe"].get(n)
            if ob:
                ranges.append(ob["max_up_pct"] - ob["max_dn_pct"])
        if ranges:
            vol_decay[n] = round(float(np.mean(ranges)), 3)

    # 最良 observe_min: same_dir_pct が最大のもの (sample>=3 で)
    valid = [(n, observe_predict[n]) for n in OBSERVE_MINS
             if observe_predict[n]["same_dir_pct"] is not None
             and observe_predict[n]["sample"] >= 3]
    best_observe = None
    if valid:
        best_observe = max(valid, key=lambda kv: kv[1]["same_dir_pct"])
        best_observe = {"observe_min": best_observe[0],
                         "same_dir_pct": best_observe[1]["same_dir_pct"],
                         "corr": best_observe[1]["corr"]}

    # 銘柄別の推奨バイアス: 例えば寄り天率 > 60% ならショート優位、ボラ < 0.3% なら除外候補
    bias_recommendation = "neutral"
    notes = []
    if yoriten_pct >= 60.0:
        bias_recommendation = "short_pref_open"
        notes.append(f"yoriten_pct={yoriten_pct} >= 60%, ショート優位")
    elif yoriten_pct <= 25.0 and gap_stats["abs_mean"] > 0.5:
        bias_recommendation = "trend_follow"
        notes.append(f"yoriten_pct={yoriten_pct} <= 25%, トレンド継続型")
    if vol_decay.get(30, 0) < 0.3:
        bias_recommendation = "exclude"
        notes.append(f"vol_decay@30min={vol_decay.get(30)}% < 0.3%, ボラ不足")
    if abs(gap_stats["abs_mean"]) > 2.0:
        notes.append(f"abs_gap_mean={gap_stats['abs_mean']}%, ギャップ大きい銘柄")

    return {
        "symbol": symbol,
        "n_days": len(day_records),
        "gap_stats": gap_stats,
        "observe_predict": observe_predict,
        "best_observe_min": best_observe,
        "yoriten_pct": yoriten_pct,
        "vol_decay_range_pct": vol_decay,
        "bias_recommendation": bias_recommendation,
        "notes": notes,
        "day_records": day_records,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="4568.T,8306.T,9433.T,3103.T,6723.T,8136.T,3382.T,9984.T,6758.T")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="data/symbol_open_profile.json")
    args = ap.parse_args()

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"=== 銘柄別寄り付きプロファイル === {len(syms)} symbols, {args.days}d")

    profiles: dict = {}
    for sym in syms:
        prof = analyze_symbol(sym, days=args.days)
        if prof is None:
            print(f"  [{sym}] no usable data")
            continue
        profiles[sym] = prof

        # 簡易表示
        bg = prof["best_observe_min"]
        bg_str = (f"observe={bg['observe_min']}min same_dir={bg['same_dir_pct']}% "
                  f"corr={bg['corr']}") if bg else "n/a"
        print(f"\n[{sym}] n_days={prof['n_days']} "
              f"gap(abs_mean)={prof['gap_stats']['abs_mean']:+.2f}% "
              f"yoriten_pct={prof['yoriten_pct']}% "
              f"vol@30={prof['vol_decay_range_pct'].get(30, 0):.2f}% "
              f"bias={prof['bias_recommendation']}")
        print(f"  best: {bg_str}")
        print("  observe_min  sample  same_dir%   corr    vol_range%")
        for n in OBSERVE_MINS:
            op = prof["observe_predict"][n]
            vr = prof["vol_decay_range_pct"].get(n, 0)
            print(f"    {n:>3}min     {op['sample']:>3}     "
                  f"{op['same_dir_pct'] if op['same_dir_pct'] is not None else 0:>5}%    "
                  f"{op['corr'] if op['corr'] is not None else 0:>+6}   "
                  f"{vr:>5.2f}%")
        if prof["notes"]:
            print("  notes:")
            for note in prof["notes"]:
                print(f"    - {note}")

    # ── 推奨銘柄分類サマリ ───────────────────────────────────────────────
    print("\n=== 銘柄別バイアス推奨 ===")
    by_bias = defaultdict(list)
    for sym, p in profiles.items():
        by_bias[p["bias_recommendation"]].append(sym)
    for b, syms_in in by_bias.items():
        print(f"  {b}: {syms_in}")

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "symbols": syms, "days": args.days,
        "profiles": profiles,
        "summary_by_bias": dict(by_bias),
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=> 結果保存: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
