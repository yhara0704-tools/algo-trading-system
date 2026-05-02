#!/usr/bin/env python3
"""N2: daemon WF OOS × 60日実測 整合表 + 四象限分類で lot_multiplier 見直し.

universe の各 entry について:
  - WF OOS = daemon の oos_daily (macd_rci_params.json + universe oos_daily)
  - 60d 実測 = D6/D8 の health_check 結果 (MacdRci/Breakout/BbShort/Pullback/EnhancedMacdRci)
  - MicroScalp は D3 で 30d 1m 検証済 → 信頼値 = oos_daily

四象限分類:
  Q1 両方+ : 高 mult (1.5-3.0)
  Q2 WF のみ+: 中立 (1.0、観察モード相当だが force_paper 維持)
  Q3 60d のみ+: 中位 (0.7、直近で当たっているが過去の WF が悪い)
  Q4 両方- : 強 demote 候補 (observation_only)

特殊ケース:
  - daemon WF データがない (= 手動 oos_daily のみ): WF=N/A → 60d で判定
  - 60d データがない (= 戦略未実測): 60d=N/A → WF で判定 (中立寄り)

出力:
  data/n2_alignment_table.json
  data/universe_active.json (lot_multiplier 微調整、Q4 demote)
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

# 60d 実測値マップ (sym, strategy) -> pnl_per_day
def build_actual_map() -> dict:
    out = {}
    # MacdRci (D6)
    d6 = json.load(open("data/d6_macd_rci_health_check.json"))
    for r in d6["rows"]:
        out[(r["symbol"], "MacdRci")] = r["pnl_per_day"]
    # 他戦略 (D8)
    d8 = json.load(open("data/d8_all_strategies_health_check.json"))
    for r in d8["results"]:
        out[(r["symbol"], r["strategy"])] = r.get("pnl_per_day", 0)
    # 6752 EnhancedMacdRci (D8d)
    d8_6752 = json.load(open("data/d8_6752_multi_strategy_test.json"))
    enh = d8_6752["results"].get("EnhancedMacdRci")
    if enh:
        out[("6752.T", "EnhancedMacdRci")] = enh["pnl_per_day"]
    return out


def build_wf_map() -> dict:
    """daemon の WF OOS 値 (macd_rci_params.json) と universe の oos_daily を合成."""
    out = {}
    macd_params = json.load(open("data/macd_rci_params.json"))
    for sym, v in macd_params.items():
        if isinstance(v, dict) and not sym.startswith("_"):
            oos = v.get("oos_daily")
            if oos is not None:
                out[(sym, "MacdRci")] = float(oos)
    # 他戦略は universe の oos_daily を WF プロキシとして採用
    universe = json.load(open("data/universe_active.json"))
    for s in universe["symbols"]:
        key = (s["symbol"], s["strategy"])
        if key not in out:
            oos = s.get("oos_daily")
            if oos is not None:
                out[key] = float(oos)
    return out


def classify(wf_oos, actual_60d) -> str:
    """四象限分類."""
    has_wf = wf_oos is not None
    has_60d = actual_60d is not None
    if has_wf and has_60d:
        if wf_oos > 0 and actual_60d > 0:
            return "Q1_both_positive"
        if wf_oos > 0 and actual_60d <= 0:
            return "Q2_wf_only"
        if wf_oos <= 0 and actual_60d > 0:
            return "Q3_60d_only"
        return "Q4_both_negative"
    if has_60d:
        return "Q1_both_positive" if actual_60d > 0 else "Q4_both_negative"
    if has_wf:
        return "Q2_wf_only" if wf_oos > 0 else "Q4_both_negative"
    return "UNKNOWN"


def recommend_mult(quadrant: str, wf_oos, actual_60d, current_mult: float) -> tuple[float, str]:
    """象限ごとの mult 推奨値."""
    if quadrant == "Q1_both_positive":
        # 実測値で割合配分。current_mult は維持 (D8c で計算済)
        return current_mult, "Q1: keep (実測値で算出済)"
    if quadrant == "Q2_wf_only":
        # WF のみ + = 直近で当たってない、観察に近い水準
        return min(current_mult, 1.0), "Q2: cap=1.0 (WF のみ+、直近 60d で確認できず)"
    if quadrant == "Q3_60d_only":
        # 60d のみ + = 直近の好調、WF は悪い → 中位 (期間限定の可能性)
        return min(current_mult, 0.7), "Q3: cap=0.7 (60d のみ+、WF 悪、期間限定の可能性)"
    if quadrant == "Q4_both_negative":
        # 両方 - = demote 候補。force_paper=true でも mult=0.5
        return 0.5, "Q4: mult=0.5 (両方-、demote 強推奨)"
    return current_mult, "UNKNOWN: 維持"


def main() -> None:
    universe = json.load(open("data/universe_active.json"))
    syms = universe["symbols"]
    actual_map = build_actual_map()
    wf_map = build_wf_map()

    print(f"=== N2: WF OOS × 60日実測 整合表 ===\n")
    print(f"{'symbol':10} {'strategy':16} {'wf_oos':>8} {'60d':>8} {'quad':<20} {'cur':>5} {'rec':>5} {'note'}")
    print(f"{'-'*100}")

    rows = []
    by_quad = defaultdict(list)

    active = [s for s in syms
              if not s.get("observation_only", False) or s.get("force_paper", False)]

    changes = []
    for s in active:
        sym, strat = s["symbol"], s["strategy"]
        key = (sym, strat)
        wf_oos = wf_map.get(key)
        actual_60d = actual_map.get(key)
        quad = classify(wf_oos, actual_60d)
        cur_mult = float(s.get("lot_multiplier", 1.0) or 1.0)
        rec_mult, note = recommend_mult(quad, wf_oos, actual_60d, cur_mult)

        wf_str = f"{wf_oos:+.0f}" if wf_oos is not None else "N/A"
        a_str = f"{actual_60d:+.0f}" if actual_60d is not None else "N/A"
        change_marker = " *" if abs(rec_mult - cur_mult) > 0.01 else ""
        print(f"  {sym:10} {strat:16} {wf_str:>8} {a_str:>8} {quad:<20} "
              f"{cur_mult:>5.2f} {rec_mult:>5.2f}{change_marker} {note}")

        rows.append({
            "symbol": sym, "strategy": strat,
            "wf_oos_daily": wf_oos, "actual_60d_per_day": actual_60d,
            "quadrant": quad, "current_mult": cur_mult, "recommended_mult": rec_mult,
            "note": note,
        })
        by_quad[quad].append((sym, strat, cur_mult, rec_mult))

        if abs(rec_mult - cur_mult) > 0.01:
            changes.append({"symbol": sym, "strategy": strat,
                          "before": cur_mult, "after": rec_mult,
                          "quadrant": quad, "note": note})
            s["lot_multiplier"] = round(rec_mult, 2)

    # ── 集計 ──
    print(f"\n=== 象限別 集計 ===\n")
    for q in ["Q1_both_positive", "Q2_wf_only", "Q3_60d_only", "Q4_both_negative", "UNKNOWN"]:
        ents = by_quad.get(q, [])
        print(f"  {q:24} : {len(ents):>2} entries")
        for sym, strat, cur, rec in ents:
            change = f" → {rec:.2f}" if abs(rec - cur) > 0.01 else ""
            print(f"    {sym:10} {strat:16} mult={cur:.2f}{change}")

    print(f"\n=== mult 変更 サマリ ===\n")
    if changes:
        for c in changes:
            print(f"  {c['symbol']:10} {c['strategy']:16} {c['before']:.2f} → {c['after']:.2f} "
                  f"({c['quadrant']})")
    else:
        print(f"  変更なし (全銘柄が現状 mult を維持)")

    # ── 期待 PnL 再計算 ──
    grand_real = 0
    weighted = 0
    for r in rows:
        ev = r["actual_60d_per_day"] if r["actual_60d_per_day"] is not None else (r["wf_oos_daily"] or 0)
        ev_pos = max(ev, 0)
        grand_real += ev_pos
        weighted += ev_pos * r["recommended_mult"]
    target = 29_700
    real_compressed = grand_real * 0.4 + 3_000
    weighted_compressed = weighted * 0.4 + 3_000
    print(f"\n=== 期待 PnL (圧縮 40%, D2 +3,000) ===\n")
    print(f"  機械分散 (mult=1):  {real_compressed:>+9.0f} 円/日 ({real_compressed/target*100:.1f}%)")
    print(f"  N2 推奨 mult 適用:   {weighted_compressed:>+9.0f} 円/日 ({weighted_compressed/target*100:.1f}%)")

    universe["updated_at"] = datetime.now(JST).isoformat()
    Path("data/universe_active.json").write_text(
        json.dumps(universe, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    Path("data/n2_alignment_table.json").write_text(
        json.dumps({
            "generated_at": datetime.now(JST).isoformat(),
            "rows": rows, "changes": changes,
            "by_quadrant_count": {q: len(by_quad[q]) for q in by_quad},
            "grand_real_per_day": grand_real,
            "weighted_pnl_per_day": weighted,
            "compressed_real": real_compressed,
            "compressed_weighted": weighted_compressed,
            "target": target,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: data/n2_alignment_table.json")
    print(f"saved: data/universe_active.json")


if __name__ == "__main__":
    main()
