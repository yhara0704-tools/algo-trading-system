#!/usr/bin/env python3
"""MTFRA 時間足組み合わせ全探索 (D9 Phase 1.5).

ユーザー提案 (2026-04-30 18:19):
> この手法でこの時間足のレジームを確認していたけど、あまり意味はなくて
> もっと大事なのはこの時間足のレジームだったとか、
> A時間足とB時間足のレジーム組み合わせだったとかがわかるよね。
> 銘柄によって癖があるかもしれないし、意外と共通だったりするかもしれない。

D9 Phase 1 は 5m+15m+60m 固定で 3 軸整合を試したが、実際には
各時間足単独・各組み合わせで予測力が違うはず。本スクリプトで全探索する。

設計:
  Stage 1 — 特徴量蓄積: 各時刻に 4 軸 direction (1m,5m,15m,60m) と
            forward return (5m,15m,30m,60m) を記録 → parquet 保存
  Stage 2 — フィルタ集計: 後で何回でも組み合わせを試せる構造
            (単独・2軸・3軸・4軸 = 全部で 50+ 組み合わせ)

入力: data/ohlcv_1m/<symbol>/*.parquet
出力: data/mtfra_combination_features.parquet (生データ)
     data/mtfra_combination_results.json (組み合わせ集計)
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from itertools import combinations

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.market_regime import _detect, _calc_adx  # noqa: E402

OHLCV_DIR = ROOT / "data/ohlcv_1m"
FEATURES_PATH = ROOT / "data/mtfra_combination_features.parquet"
RESULTS_PATH = ROOT / "data/mtfra_combination_results.json"

# 投資家層別の代表時間足:
#   1m, 3m  → スキャラー
#   5m, 15m → デイトレーダー
#   30m, 60m → スイング初動派
#   240m (4H) → 中期トレンド派 / 機関投資家
TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "60m", "240m"]
TF_RULE = {
    "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min",
    "30m": "30min", "60m": "60min", "240m": "240min",
}
FWD_MIN = [5, 15, 30, 60]


def load_symbol_data(symbol: str) -> pd.DataFrame:
    sdir = OHLCV_DIR / symbol
    if not sdir.exists():
        return pd.DataFrame()
    parts = []
    for p in sorted(sdir.glob("*.parquet")):
        try:
            d = pd.read_parquet(p)
            if not d.empty:
                parts.append(d)
        except Exception:
            continue
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df.empty:
        return df
    agg = {"open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum"}
    return df.resample(rule, label="right", closed="right").agg(agg).dropna()


def detect_dir(df: pd.DataFrame) -> str:
    """簡易方向判定: up / down / flat / unknown.

    各時間足のレジームを 3 値に集約 (詳細レジームは後で再分類しやすい)。
    """
    if len(df) < 14:
        return "unknown"
    if len(df) >= 50:
        try:
            r = _detect("X", df).regime
            if r == "trending_up":
                return "up"
            if r == "trending_down":
                return "down"
            return "flat"
        except Exception:
            pass
    close = df["close"]
    high = df["high"]
    low = df["low"]
    ema20 = close.ewm(span=20, adjust=False).mean()
    if len(ema20) < 5:
        return "unknown"
    slope = (ema20.iloc[-1] - ema20.iloc[-5]) / max(1e-9, ema20.iloc[-5]) * 100
    adx = _calc_adx(high, low, close, period=min(14, len(df) - 1))
    if adx > 25:
        return "up" if slope > 0 else "down"
    if adx < 18:
        return "flat"
    return "up" if slope > 0.05 else ("down" if slope < -0.05 else "flat")


def build_features_for_symbol(symbol: str) -> pd.DataFrame:
    df_1m = load_symbol_data(symbol)
    if df_1m.empty or len(df_1m) < 200:
        return pd.DataFrame()
    # 全時間足を resample
    tf_dfs = {tf: resample(df_1m, TF_RULE[tf]) if tf != "1m" else df_1m
              for tf in TIMEFRAMES}
    # 各時間足の最低必要バー数 (簡易判定 14 本、240m は前日数日分しかないので緩く 4)
    min_bars = {"1m": 14, "3m": 14, "5m": 14, "15m": 14,
                "30m": 10, "60m": 5, "240m": 3}

    eval_times = df_1m.index[df_1m.index.minute % 5 == 0]
    rows = []
    for ts in eval_times:
        cur_close = float(df_1m.loc[ts, "close"]) if ts in df_1m.index else None
        if cur_close is None:
            continue
        # 各時間足で direction を計算
        dirs = {}
        skip = False
        for tf in TIMEFRAMES:
            d = tf_dfs[tf]
            d_sub = d[d.index <= ts].tail(60)
            if len(d_sub) < min_bars[tf]:
                skip = True
                break
            dirs[tf] = detect_dir(d_sub)
        if skip:
            continue
        row = {"symbol": symbol, "ts": ts, "close": cur_close}
        for tf, di in dirs.items():
            row[f"dir_{tf}"] = di
        for fm in FWD_MIN:
            tgt = ts + pd.Timedelta(minutes=fm)
            fwd = df_1m[df_1m.index >= tgt].head(1)
            if fwd.empty:
                row[f"fwd_{fm}m_ret"] = np.nan
            else:
                fwd_close = float(fwd["close"].iloc[0])
                row[f"fwd_{fm}m_ret"] = (fwd_close - cur_close) / cur_close * 100
        rows.append(row)
    return pd.DataFrame(rows)


def stage1_build_features(symbols: list[str]) -> pd.DataFrame:
    """各銘柄の特徴量を蓄積して 1 つの DataFrame に結合."""
    print(f"=== Stage 1: 特徴量蓄積 ({len(symbols)} 銘柄) ===")
    parts = []
    for sym in symbols:
        print(f"  処理中: {sym} ...", end=" ", flush=True)
        df = build_features_for_symbol(sym)
        if df.empty:
            print("skip")
            continue
        print(f"n={len(df)}")
        parts.append(df)
    if not parts:
        return pd.DataFrame()
    feat = pd.concat(parts, ignore_index=True)
    feat.to_parquet(FEATURES_PATH, compression="zstd")
    print(f"\n=> 特徴量保存: {FEATURES_PATH.relative_to(ROOT)}  (n={len(feat)})")
    return feat


def evaluate_filter(df: pd.DataFrame, conditions: dict[str, str],
                    direction: str, fwd_col: str) -> dict:
    """フィルタ条件を満たす行の forward return を集計.

    Args:
        conditions: {"dir_5m": "up", "dir_15m": "up"} など
        direction: "long" or "short" (long なら return そのまま、short なら反転)
        fwd_col: "fwd_5m_ret" など
    """
    mask = pd.Series([True] * len(df), index=df.index)
    for col, val in conditions.items():
        mask &= (df[col] == val)
    sub = df[mask][fwd_col].dropna()
    if len(sub) == 0:
        return {"n": 0}
    if direction == "short":
        sub = -sub
    return {
        "n": int(len(sub)),
        "mean_ret_pct": round(float(sub.mean()), 4),
        "win_rate_pct": round(float((sub > 0).mean() * 100), 1),
        "median_ret_pct": round(float(sub.median()), 4),
    }


def all_combinations(timeframes: list[str]) -> list[tuple[str, ...]]:
    """1軸 ~ 4軸の全組み合わせを返す."""
    out = []
    for r in range(1, len(timeframes) + 1):
        for c in combinations(timeframes, r):
            out.append(c)
    return out


def stage2_combination_search(feat: pd.DataFrame) -> dict:
    """全組み合わせ × 全 fwd 期間でフィルタ評価."""
    print(f"\n=== Stage 2: 組み合わせ全探索 ===")
    benchmark = {}
    for fm in FWD_MIN:
        col = f"fwd_{fm}m_ret"
        all_ret = feat[col].dropna()
        if len(all_ret) == 0:
            continue
        benchmark[f"{fm}m"] = {
            "n": int(len(all_ret)),
            "mean_ret_pct": round(float(all_ret.mean()), 4),
            "win_rate_pct": round(float((all_ret > 0).mean() * 100), 1),
        }

    combos = all_combinations(TIMEFRAMES)
    results = {"benchmark_no_filter": benchmark, "combinations": {}}

    for combo in combos:
        # combo (1軸〜4軸) について up / down 整合パターンを評価
        for direction_label, target_dir, side in [("up", "up", "long"),
                                                    ("down", "down", "short")]:
            conds = {f"dir_{tf}": target_dir for tf in combo}
            combo_key = "+".join(combo) + f"_{direction_label}"
            row = {"combo": list(combo), "target_dir": target_dir,
                   "side": side, "fwd_stats": {}}
            for fm in FWD_MIN:
                row["fwd_stats"][f"{fm}m"] = evaluate_filter(
                    feat, conds, side, f"fwd_{fm}m_ret"
                )
            results["combinations"][combo_key] = row
    return results


def display_top_combinations(results: dict, fm_key: str = "30m",
                              min_n: int = 100, top: int = 15) -> None:
    """forward return 改善度順に組み合わせを表示."""
    bench = results["benchmark_no_filter"].get(fm_key, {})
    base_ret = bench.get("mean_ret_pct", 0)
    base_wr = bench.get("win_rate_pct", 0)
    print(f"\n  ── {fm_key} forward (ベンチマーク無フィルタ: ret={base_ret:+.3f}%, "
          f"WR={base_wr}%, n={bench.get('n')}) ──")
    rows = []
    for key, rec in results["combinations"].items():
        st = rec["fwd_stats"].get(fm_key, {})
        if st.get("n", 0) < min_n:
            continue
        # short 側は基準を反転して比較
        side = rec["side"]
        side_base_ret = -base_ret if side == "short" else base_ret
        side_base_wr = 100 - base_wr if side == "short" else base_wr
        d_ret = st["mean_ret_pct"] - side_base_ret
        d_wr = st["win_rate_pct"] - side_base_wr
        # 「効果スコア」 = Δret + ΔWR/100
        score = d_ret + d_wr / 100
        rows.append({
            "key": key, "side": side, "n": st["n"],
            "ret": st["mean_ret_pct"], "wr": st["win_rate_pct"],
            "d_ret": d_ret, "d_wr": d_wr, "score": score,
            "n_axes": len(rec["combo"]),
        })
    rows.sort(key=lambda r: -r["score"])
    print(f"  {'#':<3} {'組み合わせ':<32} {'side':<6} {'軸数':>4} {'n':>6} | "
          f"{'ret':>7} {'WR':>5} | {'Δret':>7} {'ΔWR':>6} {'score':>6}")
    print("  " + "-" * 110)
    for i, r in enumerate(rows[:top], 1):
        print(f"  {i:<3} {r['key']:<32} {r['side']:<6} "
              f"{r['n_axes']:>4} {r['n']:>6} | "
              f"{r['ret']:>+6.3f}% {r['wr']:>4.1f}% | "
              f"{r['d_ret']:>+6.3f}% {r['d_wr']:>+5.1f}% {r['score']:>+5.3f}")


def stage3_per_symbol_analysis(feat: pd.DataFrame, top_combos: list[str]) -> dict:
    """銘柄別ヒートマップ: top 5 組み合わせの効果が銘柄でどう違うか."""
    out = {}
    print(f"\n=== Stage 3: 銘柄別ヒートマップ (上位 {len(top_combos)} 組み合わせ × 全銘柄) ===\n")
    print(f"  {'symbol':<10} | " + " | ".join(f"{c[:18]:>18}" for c in top_combos))
    print("  " + "-" * (12 + 21 * len(top_combos)))
    for sym, df_sym in feat.groupby("symbol"):
        cells = [f"{sym:<10}"]
        sym_record = {"symbol": sym}
        for combo_key in top_combos:
            # combo_key 例: "5m+15m_up"
            parts = combo_key.rsplit("_", 1)
            tfs = parts[0].split("+")
            target_dir = parts[1]
            side = "long" if target_dir == "up" else "short"
            conds = {f"dir_{tf}": target_dir for tf in tfs}
            st = evaluate_filter(df_sym, conds, side, "fwd_30m_ret")
            n = st.get("n", 0)
            if n < 5:
                cells.append(f"{'-':>18}")
                sym_record[combo_key] = None
            else:
                cells.append(f"r={st['mean_ret_pct']:>+5.2f} wr={st['win_rate_pct']:>4.1f} ({n})")
                sym_record[combo_key] = {
                    "ret": st["mean_ret_pct"], "wr": st["win_rate_pct"], "n": n,
                }
        out[sym] = sym_record
        print("  " + " | ".join(cells))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*",
                    help="対象銘柄 (省略時は ohlcv_1m 全銘柄)")
    ap.add_argument("--rebuild", action="store_true",
                    help="既存 features.parquet を破棄して再生成")
    ap.add_argument("--min-n", type=int, default=100,
                    help="表示する組み合わせの最小 n (default 100)")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    if args.symbols:
        symbols = args.symbols
    else:
        symbols = sorted([p.name for p in OHLCV_DIR.iterdir() if p.is_dir()])

    # Stage 1
    if FEATURES_PATH.exists() and not args.rebuild:
        feat = pd.read_parquet(FEATURES_PATH)
        print(f"=== Stage 1 SKIP: 既存 features.parquet (n={len(feat)}) を再利用 ===")
    else:
        feat = stage1_build_features(symbols)
        if feat.empty:
            print("!! 特徴量空")
            return

    # Stage 2: 全組み合わせ評価
    results = stage2_combination_search(feat)

    # 上位表示 (30m に絞る、各軸数別 Top 5)
    print(f"\n=== 30m forward Top 組み合わせ (軸数別) ===")
    for n_axes in range(1, len(TIMEFRAMES) + 1):
        # 軸数 n_axes に絞って表示
        bench = results["benchmark_no_filter"].get("30m", {})
        base_ret = bench.get("mean_ret_pct", 0)
        base_wr = bench.get("win_rate_pct", 0)
        rows = []
        for key, rec in results["combinations"].items():
            if len(rec["combo"]) != n_axes:
                continue
            st = rec["fwd_stats"].get("30m", {})
            # 軸数が多いほど機会数が減るので閾値を緩和
            min_n_dyn = max(20, args.min_n // (2 ** (n_axes - 1)))
            if st.get("n", 0) < min_n_dyn:
                continue
            side = rec["side"]
            sb_ret = -base_ret if side == "short" else base_ret
            sb_wr = 100 - base_wr if side == "short" else base_wr
            d_ret = st["mean_ret_pct"] - sb_ret
            d_wr = st["win_rate_pct"] - sb_wr
            score = d_ret + d_wr / 100
            rows.append({"key": key, "side": side, "n": st["n"],
                          "ret": st["mean_ret_pct"], "wr": st["win_rate_pct"],
                          "d_ret": d_ret, "d_wr": d_wr, "score": score})
        rows.sort(key=lambda r: -r["score"])
        if not rows:
            continue
        print(f"\n  -- 軸数 {n_axes} (top 5) --")
        print(f"  {'#':<3} {'組み合わせ':<40} {'side':<6} {'n':>5} | "
              f"{'WR':>5} {'Δret':>7} {'ΔWR':>6} {'score':>6}")
        for i, r in enumerate(rows[:5], 1):
            print(f"  {i:<3} {r['key']:<40} {r['side']:<6} {r['n']:>5} | "
                  f"{r['wr']:>4.1f}% {r['d_ret']:>+6.3f}% {r['d_wr']:>+5.1f}% {r['score']:>+5.3f}")

    # 全体 Top (軸数問わず)
    print(f"\n=== 30m forward 全体 Top {args.top} (軸数問わず) ===")
    display_top_combinations(results, fm_key="30m", min_n=args.min_n, top=args.top)

    # Stage 3: 銘柄別 (30m, 上位 5)
    bench = results["benchmark_no_filter"].get("30m", {})
    rows_for_top = []
    for key, rec in results["combinations"].items():
        st = rec["fwd_stats"].get("30m", {})
        if st.get("n", 0) < args.min_n:
            continue
        side = rec["side"]
        side_base_ret = -bench.get("mean_ret_pct", 0) if side == "short" else bench.get("mean_ret_pct", 0)
        side_base_wr = 100 - bench.get("win_rate_pct", 0) if side == "short" else bench.get("win_rate_pct", 0)
        d_ret = st["mean_ret_pct"] - side_base_ret
        d_wr = st["win_rate_pct"] - side_base_wr
        rows_for_top.append((key, d_ret + d_wr / 100))
    rows_for_top.sort(key=lambda r: -r[1])
    top_keys = [r[0] for r in rows_for_top[:5]]
    per_sym = stage3_per_symbol_analysis(feat, top_keys)

    # 保存
    results["per_symbol_top5"] = per_sym
    results["generated_at"] = datetime.now().isoformat()
    RESULTS_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\n=> 結果保存: {RESULTS_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
