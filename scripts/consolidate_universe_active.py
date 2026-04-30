#!/usr/bin/env python3
"""universe_active.json の戦略並走を整理 + D7 カテゴリチャンピオン優先.

現状 (2026-04-30 D7 ベース):
  9984.T: 6 戦略並走、3103.T: 3 戦略並走 など過密な状態。
  これはリスク集中 + 取引競合 + ポジション重複の温床。

整理方針:
  1. 各銘柄について上位 2 戦略のみ採用 (oos_daily 降順)
  2. ただし oos_daily >= 2,000 円/日 でなければ 1 戦略のみ
  3. カテゴリチャンピオン戦略がある場合は最優先で残す
  4. 結果として「16 銘柄 × 平均 1.5 戦略 = 約 24 ペア」 を目指す

カテゴリチャンピオン (D7):
  A → MacdRci, B → EnhancedMacdRci, C → Pullback,
  D → Scalp, E/F → MacdRci

期待効果: ノイズ戦略削除で WR 向上 + シグナル品質改善 + リスク分散
"""
from __future__ import annotations
import argparse
import json
import shutil
from datetime import datetime
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_PATH = ROOT / "data/universe_active.json"
CATEGORIES_PATH = ROOT / "data/symbol_categories.json"

CATEGORY_CHAMPION = {
    "A_high_vol_short_pref":   "MacdRci",
    "B_high_vol_trend_follow": "EnhancedMacdRci",
    "C_mid_vol_trend":         "Pullback",
    "D_mid_vol_neutral":       "Scalp",
    "E_low_vol_trend":         "MacdRci",
    "F_low_vol_or_ng":         "MacdRci",
}

# 採用基準
MIN_OOS_KEEP_2ND = 2000.0   # 2 番手戦略を残す最低 oos_daily
MAX_STRATEGIES_PER_SYMBOL = 2


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max-per-symbol", type=int, default=MAX_STRATEGIES_PER_SYMBOL)
    ap.add_argument("--min-oos-2nd", type=float, default=MIN_OOS_KEEP_2ND)
    args = ap.parse_args()

    cats = json.loads(CATEGORIES_PATH.read_text())
    sym_to_cat = cats.get("symbol_to_category", {})
    universe = json.loads(UNIVERSE_PATH.read_text())
    entries = universe.get("symbols", [])

    # 銘柄ごとにグループ化、oos_daily 降順
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_sym[e["symbol"]].append(e)
    for sym in by_sym:
        by_sym[sym].sort(key=lambda x: -float(x.get("oos_daily", 0) or 0))

    print(f"=== universe_active 整理 (前: {len(entries)} ペア, "
          f"{len(by_sym)} 銘柄) ===\n")

    new_entries: list[dict] = []
    removed: list[dict] = []
    summary_per_sym = []

    for sym in sorted(by_sym.keys()):
        cat = sym_to_cat.get(sym, "?")
        champion = CATEGORY_CHAMPION.get(cat, "")
        sym_entries = by_sym[sym]

        # チャンピオン優先で並べ替え (champion を先頭に)
        champion_entries = [e for e in sym_entries if e.get("strategy") == champion]
        non_champion = [e for e in sym_entries if e.get("strategy") != champion]
        ordered = champion_entries + non_champion

        # 採用ロジック:
        #  - 1 つだけ → そのまま
        #  - 2 つ以上 → 上位 1 つ + 2 番手 (oos>=MIN_OOS_KEEP_2ND の場合)
        if len(ordered) == 0:
            continue
        keep = [ordered[0]]
        if len(ordered) >= 2 and args.max_per_symbol >= 2:
            second = ordered[1]
            if float(second.get("oos_daily", 0) or 0) >= args.min_oos_2nd:
                keep.append(second)
        # 削除されるエントリ
        for e in ordered:
            if e not in keep:
                removed.append(e)

        # 表示
        kept_strs = [f"{k['strategy']}({k.get('oos_daily', 0):+.0f})" for k in keep]
        rmv_strs = [f"{r['strategy']}({r.get('oos_daily', 0):+.0f})"
                    for r in ordered if r not in keep]
        notation = "✓" if cat and champion and any(k['strategy'] == champion for k in keep) else "✗"
        print(f"  {notation} {sym:<7} {cat:<26} → 保持: {', '.join(kept_strs)}")
        if rmv_strs:
            print(f"      削除: {', '.join(rmv_strs)}")
        for k in keep:
            new_entries.append(k)
        summary_per_sym.append({
            "symbol": sym,
            "category": cat,
            "kept": [k["strategy"] for k in keep],
            "removed": [r["strategy"] for r in ordered if r not in keep],
            "kept_oos_sum": sum(float(k.get("oos_daily", 0) or 0) for k in keep),
            "removed_oos_sum": sum(float(r.get("oos_daily", 0) or 0)
                                    for r in ordered if r not in keep),
        })

    # ── サマリ ────────────────────────────────────────
    total_old_oos = sum(float(e.get("oos_daily", 0) or 0) for e in entries)
    total_new_oos = sum(float(e.get("oos_daily", 0) or 0) for e in new_entries)
    print(f"\n=== サマリ ===")
    print(f"  ペア: {len(entries)} → {len(new_entries)} ({len(removed)} 削除)")
    print(f"  oos_daily 合計: {total_old_oos:+.0f} → {total_new_oos:+.0f} "
          f"(削除分: {total_old_oos - total_new_oos:+.0f})")
    print(f"\n  期待効果: シンプル化 + ノイズ戦略削除 + リスク分散")
    print(f"  注意: 期待 PnL は減るが (重複削除のため)、WR/PF は改善見込み")

    if not args.apply:
        print("\n  --apply フラグなしのため未変更")
        return

    backup = UNIVERSE_PATH.with_suffix(
        f".bak.consolidate.{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    shutil.copy(UNIVERSE_PATH, backup)
    print(f"\n  バックアップ: {backup.relative_to(ROOT)}")

    universe["symbols"] = new_entries
    universe["active_count"] = len(new_entries)
    universe["last_consolidation"] = datetime.now().isoformat()
    universe["consolidation_summary"] = {
        "before_pairs": len(entries),
        "after_pairs": len(new_entries),
        "removed_pairs": len(removed),
        "max_per_symbol": args.max_per_symbol,
        "min_oos_2nd": args.min_oos_2nd,
        "per_symbol": summary_per_sym,
    }
    UNIVERSE_PATH.write_text(
        json.dumps(universe, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"  更新完了: {UNIVERSE_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
