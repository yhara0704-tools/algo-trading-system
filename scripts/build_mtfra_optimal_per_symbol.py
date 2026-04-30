#!/usr/bin/env python3
"""銘柄別 MTFRA 最適組み合わせを抽出 (D9 Phase 2).

D9 Phase 1.6 の特徴量データ (data/mtfra_combination_features.parquet) を読込み、
各銘柄について最適な時間足組み合わせを評価して JSON 出力する。

判定ロジック:
  1. 各候補 combo (1m+60m, 3m+30m+60m, 1m+3m+15m+60m, 5m+15m+60m, 60m単独 等)
     について WR / mean_ret / 機会数 を計算
  2. 「総合スコア = mean_ret + ΔWR/100」 で順位付け
  3. ベスト combo の WR が無フィルタより悪化していたら "disable" 設定
  4. ベスト combo の WR がベンチマーク+5%pt 以上なら "use" 設定

出力: data/mtfra_optimal_per_symbol.json
"""
from __future__ import annotations
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FEATURES_PATH = ROOT / "data/mtfra_combination_features.parquet"
OUT_PATH = ROOT / "data/mtfra_optimal_per_symbol.json"

# 評価する候補組み合わせ (D9 Phase 1.6 で有意性確認済)
CANDIDATE_COMBOS: list[tuple[str, ...]] = [
    ("3m", "30m", "60m"),       # default 実用最強
    ("1m", "3m", "15m", "60m"),  # aggressive 高 WR
    ("1m", "60m"),               # シンプル 2 軸
    ("3m", "60m"),               # 機会数最大の 2 軸
    ("5m", "15m", "60m"),        # Phase 1 標準
    ("60m",),                    # 最長足単独
    ("3m", "30m"),               # 30m 中心ペア
    ("1m", "3m", "60m"),         # スキャラー寄り 3 軸
]

MIN_N = 10  # 評価対象とする最低サンプル数


def evaluate_combo(df: pd.DataFrame, combo: tuple[str, ...],
                   direction: str, fwd_col: str = "fwd_30m_ret") -> dict:
    """combo に対する up / down シグナルの評価."""
    target = "up" if direction == "long" else "down"
    mask = pd.Series(True, index=df.index)
    for tf in combo:
        col = f"dir_{tf}"
        if col not in df.columns:
            return {"n": 0, "skipped": "missing_col"}
        mask &= (df[col] == target)
    sub = df[mask][fwd_col].dropna()
    if len(sub) < MIN_N:
        return {"n": int(len(sub))}
    if direction == "short":
        sub = -sub
    return {
        "n": int(len(sub)),
        "mean_ret_pct": round(float(sub.mean()), 4),
        "win_rate_pct": round(float((sub > 0).mean() * 100), 1),
        "median_ret_pct": round(float(sub.median()), 4),
    }


def main() -> None:
    if not FEATURES_PATH.exists():
        print(f"!! {FEATURES_PATH} not found. "
              f"先に scripts/analyze_mtfra_combination_search.py を実行してください")
        sys.exit(1)

    feat = pd.read_parquet(FEATURES_PATH)
    print(f"=== 銘柄別 MTFRA 最適化 (n={len(feat)}, "
          f"{feat['symbol'].nunique()} 銘柄) ===\n")

    out: dict[str, dict] = {}
    summary_disable = []
    summary_default = []
    summary_aggressive = []
    summary_other = []

    # 銘柄ごとに 30m forward でベンチマーク評価 → 候補比較
    for sym, df_sym in feat.groupby("symbol"):
        bench_ret = df_sym["fwd_30m_ret"].dropna()
        if len(bench_ret) == 0:
            continue
        bench_mean = float(bench_ret.mean())
        bench_wr = float((bench_ret > 0).mean() * 100)

        candidates = []
        for combo in CANDIDATE_COMBOS:
            for direction in ["long", "short"]:
                stats = evaluate_combo(df_sym, combo, direction)
                if stats.get("n", 0) < MIN_N:
                    continue
                # ベンチ調整 (short 側はベンチ反転)
                if direction == "long":
                    base = bench_mean
                    base_wr = bench_wr
                else:
                    base = -bench_mean
                    base_wr = 100 - bench_wr
                d_ret = stats["mean_ret_pct"] - base
                d_wr = stats["win_rate_pct"] - base_wr
                score = d_ret + d_wr / 100
                candidates.append({
                    "combo": list(combo),
                    "direction": direction,
                    "n": stats["n"],
                    "wr": stats["win_rate_pct"],
                    "mean_ret": stats["mean_ret_pct"],
                    "d_ret": round(d_ret, 4),
                    "d_wr": round(d_wr, 1),
                    "score": round(score, 4),
                })

        if not candidates:
            # サンプル全部不足 → デフォルト戦略にフォールバック
            out[sym] = {
                "action": "use",
                "combo": list(("3m", "30m", "60m")),
                "reason": "insufficient_samples_fallback_default",
                "bench_wr": round(bench_wr, 1),
                "bench_mean_ret": round(bench_mean, 4),
            }
            summary_default.append((sym, "fallback"))
            continue

        candidates.sort(key=lambda r: -r["score"])
        best = candidates[0]
        # ベストでも WR が ベンチ +2%pt 未満 = MTFRA 効かない銘柄
        if best["d_wr"] < 2.0 and best["d_ret"] < 0.05:
            out[sym] = {
                "action": "disable",
                "combo": [],
                "reason": "all_combos_underperform",
                "best_attempt": best,
                "bench_wr": round(bench_wr, 1),
                "bench_mean_ret": round(bench_mean, 4),
            }
            summary_disable.append((sym, best["wr"], best["d_wr"]))
            continue

        out[sym] = {
            "action": "use",
            "combo": best["combo"],
            "direction_priority": best["direction"],
            "expected_wr": best["wr"],
            "expected_mean_ret_pct": best["mean_ret"],
            "improvement_wr_pct": best["d_wr"],
            "improvement_ret_pct": best["d_ret"],
            "n_samples": best["n"],
            "bench_wr": round(bench_wr, 1),
            "bench_mean_ret": round(bench_mean, 4),
            "alternatives": candidates[1:4],
        }
        # 分類
        if "+".join(best["combo"]) == "1m+3m+15m+60m":
            summary_aggressive.append((sym, best["wr"], best["d_wr"]))
        elif "+".join(best["combo"]) == "3m+30m+60m":
            summary_default.append((sym, best["wr"], best["d_wr"]))
        else:
            summary_other.append((sym, "+".join(best["combo"]), best["wr"], best["d_wr"]))

    # ── サマリ表示 ────────────────────────────────────────
    print(f"=== 銘柄別 MTFRA 設定サマリ (合計 {len(out)} 銘柄) ===\n")
    print(f"  [A] 3m+30m+60m (default) 採用: {len(summary_default)} 銘柄")
    for sym, wr, d_wr in summary_default[:10]:
        if isinstance(wr, str):
            print(f"      {sym}: {wr}")
        else:
            print(f"      {sym}: WR {wr}% (Δ{d_wr:+.1f}%pt)")

    print(f"\n  [B] 1m+3m+15m+60m (aggressive) 採用: {len(summary_aggressive)} 銘柄")
    for sym, wr, d_wr in summary_aggressive[:10]:
        print(f"      {sym}: WR {wr}% (Δ{d_wr:+.1f}%pt)")

    print(f"\n  [C] その他組み合わせ採用: {len(summary_other)} 銘柄")
    for sym, combo, wr, d_wr in summary_other[:10]:
        print(f"      {sym}: {combo}, WR {wr}% (Δ{d_wr:+.1f}%pt)")

    print(f"\n  [D] MTFRA 無効化 (全 combo 効果なし): {len(summary_disable)} 銘柄")
    for sym, wr, d_wr in summary_disable[:10]:
        print(f"      {sym}: best WR {wr}% (Δ{d_wr:+.1f}%pt) → disable")

    # ── 出力 JSON ────────────────────────────────────────
    OUT_PATH.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "source": "data/mtfra_combination_features.parquet (D9 Phase 1.6)",
        "n_symbols": len(out),
        "min_n_per_combo": MIN_N,
        "candidate_combos": [list(c) for c in CANDIDATE_COMBOS],
        "summary_counts": {
            "use_default_3m_30m_60m": len(summary_default),
            "use_aggressive_1m_3m_15m_60m": len(summary_aggressive),
            "use_other": len(summary_other),
            "disable": len(summary_disable),
        },
        "symbols": out,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=> 保存: {OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
