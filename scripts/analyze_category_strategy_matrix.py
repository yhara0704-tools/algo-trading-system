#!/usr/bin/env python3
"""カテゴリ × 戦略 マトリクス分析 — 「無駄なバックテストを減らす」.

ユーザー指針 (2026-04-30 17:46):
> ある程度絞れるだけでもバックテストで無駄なテストが減るからね。
> 有効な手法に発展しやすくなると思う。

D6 で銘柄を 6 カテゴリに分類した。今度は既存 experiments テーブル (348,658 行) から
(銘柄, 戦略) の最良 oos_daily を抽出し、カテゴリ × 戦略マトリクスで集計する。

得られるもの:
  1. 「カテゴリ A 銘柄なら Breakout が平均 +N円、MacdRci が +M円」というベンチマーク
  2. 各カテゴリの「チャンピオン戦略」(= そのカテゴリで最も期待値高い)
  3. 「デッドゾーン」(= どの戦略も負けるカテゴリ → 戦略不在問題の可視化)

これで新規銘柄を発見した時も、カテゴリさえ判定すれば「この戦略は試す価値あり、これはなし」と
バックテストの試行回数を 1/3 程度に削減できる。
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data/algo_trading.db"
CATEGORIES_PATH = ROOT / "data/symbol_categories.json"

# 集計対象戦略 (Momentum5Min, MaVol, VwapReversion はサンプル少のため除外)
TARGET_STRATEGIES = [
    "MacdRci", "EnhancedMacdRci",
    "Scalp", "EnhancedScalp",
    "Breakout", "Pullback", "BbShort",
]


def fetch_best_per_pair() -> dict[tuple[str, str], dict]:
    """(symbol, strategy) ごとに最良 oos_daily を返す.

    優先順位:
      1. robust=1 で oos_daily 最大
      2. なければ全体で oos_daily 最大
    """
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT symbol, strategy_name, oos_daily_pnl, oos_pf, oos_win_rate,
               oos_trades, robust, calmar, cost_drag_pct, created_at
          FROM experiments
         WHERE oos_daily_pnl IS NOT NULL
           AND strategy_name IN ({','.join('?' * len(TARGET_STRATEGIES))})
        """,
        TARGET_STRATEGIES,
    )
    out: dict[tuple[str, str], dict] = {}
    for row in cur.fetchall():
        sym, strat, oos_d, oos_pf, oos_wr, oos_n, robust, calmar, cost_drag, created = row
        key = (sym, strat)
        cur_best = out.get(key)
        # 比較ルール: robust 優先、次に oos_daily
        new_score = (1 if robust else 0, oos_d or -1e9)
        if cur_best:
            cur_score = (1 if cur_best["robust"] else 0, cur_best["oos_daily_pnl"] or -1e9)
            if new_score <= cur_score:
                continue
        out[key] = {
            "symbol": sym,
            "strategy_name": strat,
            "oos_daily_pnl": oos_d,
            "oos_pf": oos_pf,
            "oos_win_rate": oos_wr,
            "oos_trades": oos_n,
            "robust": bool(robust),
            "calmar": calmar,
            "cost_drag_pct": cost_drag,
            "created_at": created,
        }
    conn.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/category_strategy_matrix.json")
    ap.add_argument("--min-trades", type=int, default=10,
                    help="oos_trades がこの値未満は除外 (低サンプル排除)")
    args = ap.parse_args()

    if not CATEGORIES_PATH.exists():
        print(f"!! {CATEGORIES_PATH} not found")
        sys.exit(1)
    cats_data = json.loads(CATEGORIES_PATH.read_text())
    sym_to_cat: dict[str, str] = cats_data.get("symbol_to_category", {})
    if not sym_to_cat:
        print("!! symbol_to_category が空。run categorize_symbols.py first")
        sys.exit(1)
    print(f"=== カテゴリ × 戦略 マトリクス === categories: {len(set(sym_to_cat.values()))}, "
          f"symbols: {len(sym_to_cat)}")

    pairs = fetch_best_per_pair()
    pairs = {k: v for k, v in pairs.items() if (v["oos_trades"] or 0) >= args.min_trades}
    print(f"=== experiments: {len(pairs)} pairs (oos_trades >= {args.min_trades})\n")

    # ── (cat, strat) で集計 ──────────────────────────────────────────
    matrix: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {
        "n_symbols": 0, "n_robust": 0,
        "oos_daily_sum": 0.0, "oos_daily_max": -1e18, "oos_daily_min": 1e18,
        "oos_pf_sum": 0.0, "oos_pf_count": 0,
        "oos_wr_sum": 0.0, "oos_wr_count": 0,
        "symbols_pos": [], "symbols_neg": [],
    }))
    for (sym, strat), info in pairs.items():
        cat = sym_to_cat.get(sym)
        if cat is None:
            continue
        cell = matrix[cat][strat]
        cell["n_symbols"] += 1
        if info["robust"]:
            cell["n_robust"] += 1
        v = info["oos_daily_pnl"] or 0.0
        cell["oos_daily_sum"] += v
        cell["oos_daily_max"] = max(cell["oos_daily_max"], v)
        cell["oos_daily_min"] = min(cell["oos_daily_min"], v)
        if info["oos_pf"] is not None:
            cell["oos_pf_sum"] += info["oos_pf"]
            cell["oos_pf_count"] += 1
        if info["oos_win_rate"] is not None:
            cell["oos_wr_sum"] += info["oos_win_rate"]
            cell["oos_wr_count"] += 1
        if v > 0:
            cell["symbols_pos"].append(sym)
        elif v < 0:
            cell["symbols_neg"].append(sym)

    # ── 表示 + チャンピオン抽出 ──────────────────────────────────────
    print("=== カテゴリ × 戦略 マトリクス (平均 oos_daily / robust 数 / カバレッジ) ===\n")
    cat_order = ["A_high_vol_short_pref", "B_high_vol_trend_follow",
                 "C_mid_vol_trend", "D_mid_vol_neutral",
                 "E_low_vol_trend", "F_low_vol_or_ng"]
    cat_total = {c: sum(1 for s, ct in sym_to_cat.items() if ct == c) for c in cat_order}

    champions: dict[str, dict] = {}
    deadzones: dict[str, list] = {}

    header_strats = TARGET_STRATEGIES
    print(f"{'カテゴリ':<26} (n) | " +
          " | ".join(f"{s[:14]:>14}" for s in header_strats))
    print("-" * 130)
    for cat in cat_order:
        row_data = matrix.get(cat, {})
        if not any(row_data.get(s, {}).get("n_symbols", 0) > 0 for s in header_strats):
            continue
        cells = []
        best_avg = -1e18
        best_strat = None
        for s in header_strats:
            cell = row_data.get(s)
            if cell and cell["n_symbols"] > 0:
                avg = cell["oos_daily_sum"] / cell["n_symbols"]
                cov_pct = cell["n_symbols"] / cat_total[cat] * 100 if cat_total[cat] else 0
                cells.append(f"{avg:>+8.0f}/{cell['n_symbols']:>2}({cov_pct:>3.0f}%)")
                if avg > best_avg and cell["n_symbols"] >= 2:
                    best_avg = avg
                    best_strat = s
            else:
                cells.append(f"{'-':>14}")
        print(f"{cat:<26} ({cat_total[cat]:>2}) | " + " | ".join(cells))
        if best_strat:
            champions[cat] = {
                "strategy": best_strat,
                "avg_oos_daily": round(best_avg, 1),
                "n_symbols": row_data[best_strat]["n_symbols"],
                "n_robust": row_data[best_strat]["n_robust"],
            }
        # デッドゾーン: 全戦略の avg が負 (= このカテゴリは戦略不在)
        all_neg = all(
            (row_data.get(s, {}).get("oos_daily_sum", 0) /
             max(1, row_data.get(s, {}).get("n_symbols", 0))) <= 0
            for s in header_strats if row_data.get(s, {}).get("n_symbols", 0) > 0
        )
        if all_neg:
            deadzones[cat] = [s for s in header_strats
                               if row_data.get(s, {}).get("n_symbols", 0) > 0]

    print("\n=== 各カテゴリのチャンピオン戦略 ===")
    for cat in cat_order:
        if cat in champions:
            c = champions[cat]
            print(f"  {cat:<26} → {c['strategy']:<18} "
                  f"avg={c['avg_oos_daily']:>+7.1f}円/日 "
                  f"({c['n_symbols']} 銘柄, robust {c['n_robust']})")
        elif cat_total.get(cat, 0) > 0:
            print(f"  {cat:<26} → (該当データなし、戦略不在の可能性)")

    if deadzones:
        print("\n=== デッドゾーン (全戦略マイナス = 新戦略開発候補) ===")
        for cat, strats in deadzones.items():
            print(f"  {cat:<26} 戦略: {strats}")

    # ── 出力 JSON ────────────────────────────────────────────────────
    out_path = ROOT / args.out
    serialized = {}
    for cat, strat_dict in matrix.items():
        serialized[cat] = {}
        for s, cell in strat_dict.items():
            n = cell["n_symbols"]
            serialized[cat][s] = {
                "n_symbols": n,
                "n_robust": cell["n_robust"],
                "avg_oos_daily": round(cell["oos_daily_sum"] / n, 1) if n else 0,
                "max_oos_daily": round(cell["oos_daily_max"], 1) if n else 0,
                "min_oos_daily": round(cell["oos_daily_min"], 1) if n else 0,
                "avg_oos_pf": round(cell["oos_pf_sum"] / cell["oos_pf_count"], 2)
                              if cell["oos_pf_count"] else None,
                "avg_oos_wr": round(cell["oos_wr_sum"] / cell["oos_wr_count"] * 100, 1)
                              if cell["oos_wr_count"] else None,
                "symbols_pos": cell["symbols_pos"],
                "symbols_neg": cell["symbols_neg"],
            }
    out_path.write_text(json.dumps({
        "generated_at": "2026-04-30",
        "source": "experiments table (best per (symbol, strategy))",
        "min_trades": args.min_trades,
        "categories_total": cat_total,
        "matrix": serialized,
        "champions": champions,
        "deadzones": deadzones,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=> 結果保存: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
