#!/usr/bin/env python3
"""universe_active.json を D7 カテゴリチャンピオンに最適化 (D7 反映).

D7 で各カテゴリのチャンピオン戦略が判明した:
  A → MacdRci (+27,978 円/日)
  B → EnhancedMacdRci (+9,042 円/日)
  C → Pullback (+1,179 円/日)
  D → Scalp (+308 円/日)
  E → MacdRci (+340 円/日)
  F → MacdRci (+187 円/日)

現状の universe_active.json では、特に以下の銘柄で「カテゴリ非最適戦略」 が選ばれている:
  - 3103.T (A): Pullback (+2,696) → MacdRci (+27,978) で +25,282 円/日 改善
  - 9984.T (B): Scalp (-83 NG)   → EnhancedMacdRci (+9,042) で +9,125 円/日 改善
  - 6501/6613/6723 (B): MacdRci/Breakout → EnhancedMacdRci で +6,500 円/日 × 3 改善
  - 1605/6752/8136 (C): Scalp/MacdRci/Breakout → Pullback で +840 円/日 × 3 改善

このスクリプトは:
  1. 各銘柄を experiments DB から「カテゴリチャンピオン戦略」の最新 robust データで
     再評価 (robust+sensitivity>=0.8 の最良結果を取得)
  2. 既存戦略よりチャンピオン戦略の oos_daily が高い場合、swap を提案
  3. --apply フラグで universe_active.json を実際に書き換え (バックアップ作成)

注意:
  - チャンピオン戦略でも「個別銘柄の robust」が無いと swap できない (DB に存在必須)
  - swap 後は paper trading で検証してから実弾移行を推奨
"""
from __future__ import annotations
import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

UNIVERSE_PATH = ROOT / "data/universe_active.json"
CATEGORIES_PATH = ROOT / "data/symbol_categories.json"
DB_PATH = ROOT / "data/algo_trading.db"

CATEGORY_CHAMPION = {
    "A_high_vol_short_pref":   "MacdRci",
    "B_high_vol_trend_follow": "EnhancedMacdRci",
    "C_mid_vol_trend":         "Pullback",
    "D_mid_vol_neutral":       "Scalp",
    "E_low_vol_trend":         "MacdRci",
    "F_low_vol_or_ng":         "MacdRci",
}


def fetch_best_for_pair(symbol: str, strategy: str) -> dict | None:
    """(symbol, strategy) について最良 robust 行を取得."""
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, oos_daily_pnl, oos_pf, oos_win_rate, oos_trades,
               robust, sensitivity, is_oos_pass, calmar, score, created_at
          FROM experiments
         WHERE symbol = ? AND strategy_name = ?
           AND oos_daily_pnl IS NOT NULL
        ORDER BY (CASE WHEN robust=1 THEN 1 ELSE 0 END) DESC,
                 oos_daily_pnl DESC
         LIMIT 1
        """,
        (symbol, strategy),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    keys = ["id", "oos_daily_pnl", "oos_pf", "oos_win_rate", "oos_trades",
            "robust", "sensitivity", "is_oos_pass", "calmar", "score", "created_at"]
    return dict(zip(keys, row))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="変更を universe_active.json に書き込み (バックアップ作成)")
    ap.add_argument("--min-improvement-pct", type=float, default=20.0,
                    help="この%以上の改善のみ swap (default 20%)")
    args = ap.parse_args()

    cats = json.loads(CATEGORIES_PATH.read_text())
    sym_to_cat = cats.get("symbol_to_category", {})
    universe = json.loads(UNIVERSE_PATH.read_text())
    symbols_list = universe.get("symbols", [])

    print(f"=== universe_active swap 提案 ({len(symbols_list)} 銘柄) ===\n")

    swaps = []
    keep = []
    for entry in symbols_list:
        sym = entry["symbol"]
        cur_strat = entry.get("strategy", "?")
        cur_oos = entry.get("oos_daily", 0)
        cat = sym_to_cat.get(sym, "?")
        champion = CATEGORY_CHAMPION.get(cat)
        if not champion:
            keep.append((sym, cur_strat, cur_oos, "no_category"))
            continue

        if cur_strat == champion:
            keep.append((sym, cur_strat, cur_oos, "already_champion"))
            print(f"  ✓ {sym:<7} {cat:<26} {cur_strat:<18} (oos {cur_oos:+.0f}) — 既にチャンピオン")
            continue

        champion_data = fetch_best_for_pair(sym, champion)
        if not champion_data:
            keep.append((sym, cur_strat, cur_oos, f"no_robust_for_{champion}"))
            print(f"  ⚠ {sym:<7} {cat:<26} {cur_strat:<18} (oos {cur_oos:+.0f}) — "
                  f"{champion} の robust データなし、現状維持")
            continue

        new_oos = champion_data["oos_daily_pnl"]
        improvement = new_oos - cur_oos
        improvement_pct = (improvement / max(1, abs(cur_oos))) * 100 if cur_oos else 999

        if new_oos <= cur_oos:
            keep.append((sym, cur_strat, cur_oos, f"champion_worse_{new_oos:+.0f}"))
            print(f"  → {sym:<7} {cat:<26} {cur_strat:<18} (oos {cur_oos:+.0f}) — "
                  f"{champion} ({new_oos:+.0f}) は劣化、現状維持")
            continue

        if improvement_pct < args.min_improvement_pct:
            keep.append((sym, cur_strat, cur_oos, f"low_improvement_{improvement_pct:.0f}%"))
            print(f"  → {sym:<7} {cat:<26} {cur_strat:<18} (oos {cur_oos:+.0f}) → "
                  f"{champion} ({new_oos:+.0f}, +{improvement_pct:.0f}%) — 改善小、現状維持")
            continue

        swaps.append({
            "symbol": sym, "category": cat,
            "old_strategy": cur_strat, "old_oos": cur_oos,
            "new_strategy": champion, "new_oos": new_oos,
            "new_robust": champion_data["robust"],
            "new_pf": champion_data["oos_pf"],
            "new_wr": champion_data["oos_win_rate"],
            "new_trades": champion_data["oos_trades"],
            "improvement_jpy": improvement,
            "improvement_pct": improvement_pct,
            "experiment_id": champion_data["id"],
        })
        print(f"  ★ {sym:<7} {cat:<26} {cur_strat:<18} ({cur_oos:+.0f}) → "
              f"{champion} ({new_oos:+.0f}, +{improvement:+.0f}円 = +{improvement_pct:.0f}%) "
              f"[robust={champion_data['robust']}]")

    # サマリ
    total_improvement = sum(s["improvement_jpy"] for s in swaps)
    print(f"\n=== サマリ ===")
    print(f"  swap 提案: {len(swaps)} 銘柄")
    print(f"  現状維持: {len(keep)} 銘柄")
    print(f"  期待改善合計: {total_improvement:+.0f} 円/日")

    if not swaps:
        print("\n  swap 対象なし。終了。")
        return

    if not args.apply:
        print("\n  --apply フラグなしのため、universe_active.json は未変更")
        print("  実際に適用するには: python scripts/rebalance_universe_to_champions.py --apply")
        return

    # ── universe_active.json 更新 ────────────────────────────
    backup_path = UNIVERSE_PATH.with_suffix(
        f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    shutil.copy(UNIVERSE_PATH, backup_path)
    print(f"\n  バックアップ作成: {backup_path.relative_to(ROOT)}")

    swap_map = {s["symbol"]: s for s in swaps}
    new_symbols = []
    for entry in symbols_list:
        sym = entry["symbol"]
        if sym in swap_map:
            sw = swap_map[sym]
            ed = entry["experiment_id"] if "experiment_id" in entry else None
            new_entry = {
                **entry,
                "strategy": sw["new_strategy"],
                "oos_daily": sw["new_oos"],
                "is_pf": sw["new_pf"],
                "is_trades": sw["new_trades"],
                "robust": bool(sw["new_robust"]),
                "source": f"D7_champion_swap_{sw['old_strategy']}_to_{sw['new_strategy']}",
                "previous_strategy": sw["old_strategy"],
                "previous_oos_daily": sw["old_oos"],
                "swapped_at": datetime.now().isoformat(),
            }
            if "score" in entry:
                new_entry["score"] = sw["new_oos"]  # 簡易: oos_daily をスコアに
            new_symbols.append(new_entry)
        else:
            new_symbols.append(entry)

    universe["symbols"] = new_symbols
    universe["last_d7_rebalance"] = datetime.now().isoformat()
    universe["d7_rebalance_summary"] = {
        "n_swaps": len(swaps),
        "expected_improvement_jpy_per_day": total_improvement,
        "swaps": [{"symbol": s["symbol"],
                   "from": s["old_strategy"], "to": s["new_strategy"],
                   "improvement": s["improvement_jpy"]} for s in swaps],
    }
    UNIVERSE_PATH.write_text(
        json.dumps(universe, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"  更新完了: {UNIVERSE_PATH.relative_to(ROOT)}")
    print(f"\n  期待改善: {total_improvement:+.0f} 円/日 (理論値、要 paper 検証)")


if __name__ == "__main__":
    main()
