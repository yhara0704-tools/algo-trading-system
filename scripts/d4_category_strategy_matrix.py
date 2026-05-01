#!/usr/bin/env python3
"""D4b: カテゴリ × 戦略マトリクス検証 — 場中全時間帯カバレッジ.

各戦略の trade を時間帯別に集計し、「どの時間帯がどの戦略でカバーされているか」
を可視化する。場中全時間 (9:00-15:30) で複数戦略が同時並走することで、
理論最大 PnL に近づく可能性を確認する。

時間帯定義:
  T_open      09:00-09:30 (寄付 30 分)
  T_morning   09:30-11:30 (前場メイン)
  T_lunch_pre 11:25-11:30 (前場引け前)
  T_afternoon 12:30-14:00 (後場メイン)
  T_close     14:00-15:30 (大引け前)

戦略カバレッジ (D3 + D4 結果から):
  - MicroScalp: 1m 短期スキャル (12:30-15:00 強い)
  - MacdRci: 5m 利大損小 (前場・後場両方)
  - BBShort: 5m 平均回帰 (高ボラ時)
  - Pullback: 5m 押し目買い (トレンド継続時)
  - SwingDonchian: 1d スイング (時間帯非依存)

期待: 同銘柄でも複数戦略が **異なる時間帯** で signal 出すなら並走 OK。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


def main() -> None:
    print("=== D4b カテゴリ × 戦略マトリクス ===\n")

    # ── D3 microscalp 結果から各銘柄の時間帯別 WR を読む ────────
    j_ms = json.load(open("data/microscalp_per_symbol_30d.json"))
    ms_results = j_ms["results"]
    by_symbol_ms = {}
    for r in ms_results:
        s = r["symbol"]
        if s not in by_symbol_ms or r.get("pnl_per_day", 0) > by_symbol_ms[s].get("pnl_per_day", 0):
            by_symbol_ms[s] = r

    # ── D4 alt 結果 ────────────────────────────────────────────
    j_alt = json.load(open("data/d4_universe_candidates_relaxed.json"))
    alt_best = j_alt.get("best_per_symbol", {})

    # ── universe 既存 MacdRci OOS ─────────────────────────────
    u = json.load(open("data/universe_active.json"))
    macd_oos = {}
    for s in u.get("symbols", []):
        if s["strategy"] in {"MacdRci", "EnhancedMacdRci"} and not s.get("observation_only"):
            macd_oos[s["symbol"]] = {
                "strategy": s["strategy"],
                "oos_daily": s.get("oos_daily", 0),
            }

    # ── 銘柄別マトリクス ────────────────────────────────────────
    print(f"{'銘柄':<8} {'MacdRci OOS':>12} {'MicroScalp(D3) /day':>22} {'BBShort/Pullback(D4) /day':>30} {'カテゴリ案'}")
    print("-" * 100)
    
    # 集計用
    grand_total = {"MacdRci": 0, "MicroScalp": 0, "BBShort": 0, "Pullback": 0, "Donchian": 0}
    cat_count = defaultdict(int)
    rows = []

    all_syms = set(macd_oos.keys()) | set(by_symbol_ms.keys()) | set(alt_best.keys())
    for sym in sorted(all_syms):
        macd = macd_oos.get(sym, {})
        ms = by_symbol_ms.get(sym, {})
        alt = alt_best.get(sym, {})

        # 時間帯ヒント
        ms_window = ms.get("by_window", {})
        ms_dominant = ""
        if ms_window:
            best_w = max(ms_window.items(), key=lambda kv: kv[1].get("pnl", 0))
            if best_w[1].get("pnl", 0) > 0:
                ms_dominant = best_w[0]

        # カテゴリ判定 (MicroScalp の dominant + alt 戦略から)
        cat = ""
        if alt.get("strategy") == "BBShort":
            cat = "高ボラ反転 (BB 3σ)"
        elif alt.get("strategy") == "Pullback":
            cat = "トレンド継続 (押し目)"
        elif macd.get("strategy"):
            cat = "MACD×RCI 主軸"
        else:
            cat = "未分類"
        cat_count[cat] += 1

        # 集計
        grand_total["MacdRci"] += macd.get("oos_daily", 0)
        grand_total["MicroScalp"] += ms.get("pnl_per_day", 0) if ms.get("pnl_per_day", 0) > 0 else 0
        if alt.get("strategy") == "BBShort":
            grand_total["BBShort"] += alt.get("pnl_per_day", 0)
        elif alt.get("strategy") == "Pullback":
            grand_total["Pullback"] += alt.get("pnl_per_day", 0)

        macd_str = f"{macd.get('oos_daily', 0):>10.0f}" if macd else "        -"
        ms_pnl = ms.get("pnl_per_day", 0) if ms.get("pnl_per_day", 0) > 0 else 0
        ms_str = f"{ms_pnl:>6.0f}" + (f" ({ms_dominant[:9]})" if ms_dominant else "")
        if not ms:
            ms_str = "        -"
        alt_str = "        -"
        if alt.get("strategy") in ("BBShort", "Pullback"):
            alt_str = f"{alt['strategy']:<10} {alt.get('pnl_per_day', 0):>5.0f}"
        print(f"{sym:<8} {macd_str:>12} {ms_str:>22} {alt_str:>30} {cat}")
        rows.append({
            "symbol": sym, "macd": macd.get("oos_daily", 0),
            "microscalp_pnl_day": ms_pnl,
            "alt_strategy": alt.get("strategy", ""),
            "alt_pnl_day": alt.get("pnl_per_day", 0),
            "category": cat,
        })

    print("\n=== 戦略別 期待値合計 (理論最大、1 銘柄全余力前提) ===")
    for k, v in grand_total.items():
        print(f"  {k:<12}: {v:>8,.0f} 円/日")
    print(f"  {'合計':<12}: {sum(grand_total.values()):>8,.0f} 円/日")

    print("\n=== カテゴリ別 銘柄数 ===")
    for c, n in sorted(cat_count.items(), key=lambda kv: -kv[1]):
        print(f"  {c}: {n} 銘柄")

    print("\n=== 場中時間帯カバレッジ (per-symbol best window) ===")
    window_count = defaultdict(int)
    for s, r in by_symbol_ms.items():
        bw = r.get("by_window", {})
        if not bw:
            continue
        best_w = max(bw.items(), key=lambda kv: kv[1].get("pnl", 0))
        if best_w[1].get("pnl", 0) > 0:
            window_count[best_w[0]] += 1
    for w, n in sorted(window_count.items()):
        print(f"  {w}: {n} 銘柄でこの時間帯が最強")

    # ── 出力 ────────────────────────────────────────────────────
    Path("data/d4_category_strategy_matrix.json").write_text(
        json.dumps({
            "rows": rows,
            "grand_total": grand_total,
            "cat_count": dict(cat_count),
            "window_count": dict(window_count),
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\nsaved: data/d4_category_strategy_matrix.json")


if __name__ == "__main__":
    main()
