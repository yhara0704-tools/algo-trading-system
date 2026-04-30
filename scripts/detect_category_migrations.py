#!/usr/bin/env python3
"""カテゴリ遷移検出 + テーマ強弱分析 — 「動的カテゴリ追跡」 (D8).

ユーザー指針 (2026-04-30 17:52):
> A〜Fのカテゴライズされた中身の銘柄は動的に変化するよ。
> やっぱりずっと強いテーマもあれば、すぐに廃れるテーマもあるからね。

カテゴリは静的なラベルではなく、テーマ性・地合い・ボラ変動で時系列に遷移する。
これを追跡しないと、戦略アロケーションが古いまま固定されてしまう。

入力: data/category_history/<YYYY-MM-DD>.json (週次スナップショット)
     data/theme_map.json (テーマ × 銘柄)

出力: data/category_migrations_latest.json (遷移検出 + 戦略切替推奨)

検出するもの:
  1. **カテゴリ遷移**: 個別銘柄が前回 vs 今回でカテゴリが変わった
     例: 「6920.T が B → A に昇格 (高ボラ強化)」
     → 戦略を EnhancedMacdRci → MacdRci 寄せに切替推奨

  2. **テーマ強弱**: 同一テーマ銘柄の同時カテゴリ移動を検出
     例: 「半導体テーマ 5 銘柄が一斉に B → A → AI テーマ急騰の兆候」
     → universe 拡張 + ポジション増の推奨

  3. **デッドテーマ**: 全銘柄が E/F に降格しているテーマ
     例: 「化粧品テーマが全銘柄 F に → universe から除外候補」
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY_DIR = ROOT / "data/category_history"
THEME_MAP_PATH = ROOT / "data/theme_map.json"
SECTOR_MAP_PATH = ROOT / "data/sector_map.json"

# カテゴリの上下関係 (高ボラ ↔ 低ボラの軸)
CATEGORY_RANK = {
    "A_high_vol_short_pref": 5,  # 最高ボラ
    "B_high_vol_trend_follow": 5,
    "C_mid_vol_trend": 3,
    "D_mid_vol_neutral": 3,
    "E_low_vol_trend": 1,
    "F_low_vol_or_ng": 0,
}

# 戦略切替推奨マップ (from_category → to_category → 推奨アクション)
STRATEGY_TRANSITION = {
    "A→B": "EnhancedMacdRci 寄せに切替 (順張り優位化)",
    "A→C": "Pullback 寄せに切替 (ボラ低下、中ボラ順張り化)",
    "A→E": "MacdRci 1 本化、ポジションサイズ縮小",
    "A→F": "universe から除外候補",
    "B→A": "MacdRci + MicroScalp_short 追加 (寄り天増強)",
    "B→C": "Pullback 寄せに切替 (ボラ低下)",
    "B→D": "Scalp + MicroScalp 寄せに切替",
    "B→E": "MacdRci 1 本化、ポジションサイズ縮小",
    "B→F": "universe から除外候補",
    "C→A": "MacdRci + MicroScalp_short 追加 (高ボラ昇格、ショート優位)",
    "C→B": "EnhancedMacdRci 追加 (高ボラ + 順張り強化)",
    "C→D": "Scalp + MicroScalp 追加 (ボラ維持、中立化)",
    "C→E": "MacdRci 1 本化、ポジションサイズ縮小",
    "C→F": "universe から除外候補",
    "D→A": "MacdRci + MicroScalp_short 追加",
    "D→B": "EnhancedMacdRci 追加",
    "D→C": "Pullback 追加 (順張り化)",
    "D→E": "MacdRci 1 本化",
    "D→F": "universe から除外候補",
    "E→A": "高ボラ昇格 → MacdRci + MicroScalp_short 投入",
    "E→B": "高ボラ昇格 → EnhancedMacdRci 投入",
    "E→C": "ボラ上昇 → Pullback 追加",
    "E→D": "Scalp + MicroScalp 追加",
    "E→F": "universe から除外候補",
    "F→A": "復活 → 高ボラ + ショート優位、MacdRci + MicroScalp_short 投入",
    "F→B": "復活 → EnhancedMacdRci 投入",
    "F→C": "復活 → Pullback 投入",
    "F→D": "復活 → Scalp 投入",
    "F→E": "復活 → MacdRci 投入",
}


def list_snapshots() -> list[Path]:
    if not HISTORY_DIR.exists():
        return []
    return sorted(HISTORY_DIR.glob("*.json"))


def load_snapshot(path: Path) -> dict:
    return json.loads(path.read_text())


def detect_migrations(prev: dict, cur: dict) -> list[dict]:
    """銘柄ごとのカテゴリ遷移を検出.

    Returns:
        [{symbol, from_cat, to_cat, rank_delta, strategy_action}, ...]
    """
    prev_map: dict[str, str] = prev.get("symbol_to_category", {})
    cur_map: dict[str, str] = cur.get("symbol_to_category", {})
    migrations = []
    for sym, cur_cat in cur_map.items():
        prev_cat = prev_map.get(sym)
        if prev_cat is None:
            migrations.append({
                "symbol": sym,
                "from_cat": "(NEW)",
                "to_cat": cur_cat,
                "rank_delta": None,
                "strategy_action": "新規追加: カテゴリのテンプレ戦略を試行",
                "type": "new",
            })
            continue
        if prev_cat == cur_cat:
            continue
        rank_delta = CATEGORY_RANK.get(cur_cat, 0) - CATEGORY_RANK.get(prev_cat, 0)
        key = f"{prev_cat[0]}→{cur_cat[0]}"  # "B→A" 等
        action = STRATEGY_TRANSITION.get(key, "(要マニュアル判断)")
        migrations.append({
            "symbol": sym,
            "from_cat": prev_cat,
            "to_cat": cur_cat,
            "rank_delta": rank_delta,
            "strategy_action": action,
            "type": "promote" if rank_delta > 0 else "demote",
        })
    # 削除 (current にない symbol)
    for sym in prev_map:
        if sym not in cur_map:
            migrations.append({
                "symbol": sym,
                "from_cat": prev_map[sym],
                "to_cat": "(REMOVED)",
                "rank_delta": None,
                "strategy_action": "universe から削除済",
                "type": "removed",
            })
    return migrations


def analyze_themes(cur: dict) -> dict:
    """テーマごとのカテゴリ分布を集計 (テーマ強弱).

    Returns:
        {theme_name: {n_total, members: [(symbol, category)], avg_rank, status}}
    """
    if not THEME_MAP_PATH.exists():
        return {}
    theme_data = json.loads(THEME_MAP_PATH.read_text())
    themes = theme_data.get("themes", {})
    cur_map: dict[str, str] = cur.get("symbol_to_category", {})

    out = {}
    for theme_name, info in themes.items():
        members = []
        for entry in info.get("tracked", []):
            sym = entry.get("symbol")
            cat = cur_map.get(sym)
            if cat:
                members.append((sym, cat))
        if not members:
            continue
        ranks = [CATEGORY_RANK.get(c, 0) for _, c in members]
        avg_rank = sum(ranks) / len(ranks)
        # ステータス判定
        if avg_rank >= 4:
            status = "🔥 強い (高ボラ集中)"
        elif avg_rank >= 2:
            status = "○ 中程度"
        elif avg_rank >= 1:
            status = "▽ 弱め (低ボラ)"
        else:
            status = "✗ 廃れ (除外候補)"
        out[theme_name] = {
            "n_tracked": len(members),
            "n_total": info.get("constituents_total"),
            "members": [{"symbol": s, "category": c} for s, c in members],
            "avg_rank": round(avg_rank, 2),
            "status": status,
        }
    return out


def detect_theme_movements(prev: dict, cur: dict) -> list[dict]:
    """テーマ単位での同時カテゴリ移動を検出."""
    if not THEME_MAP_PATH.exists():
        return []
    theme_data = json.loads(THEME_MAP_PATH.read_text())
    themes = theme_data.get("themes", {})
    prev_map = prev.get("symbol_to_category", {})
    cur_map = cur.get("symbol_to_category", {})

    out = []
    for theme_name, info in themes.items():
        syms = [e.get("symbol") for e in info.get("tracked", [])]
        moves = []
        for s in syms:
            p = prev_map.get(s)
            c = cur_map.get(s)
            if not p or not c or p == c:
                continue
            delta = CATEGORY_RANK.get(c, 0) - CATEGORY_RANK.get(p, 0)
            moves.append({"symbol": s, "from": p, "to": c, "delta": delta})
        if not moves:
            continue
        # 同方向移動が多数派なら強いシグナル
        promoted = sum(1 for m in moves if m["delta"] > 0)
        demoted = sum(1 for m in moves if m["delta"] < 0)
        if promoted > demoted * 2 and promoted >= 2:
            sig = f"🔥 テーマ急騰 (昇格 {promoted} / 降格 {demoted})"
        elif demoted > promoted * 2 and demoted >= 2:
            sig = f"❄ テーマ冷却 (昇格 {promoted} / 降格 {demoted})"
        else:
            sig = f"○ 混在 (昇格 {promoted} / 降格 {demoted})"
        out.append({
            "theme": theme_name,
            "moves": moves,
            "signal": sig,
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prev", help="比較元スナップショット (省略時は最新の 1 つ前)")
    ap.add_argument("--cur", help="比較先スナップショット (省略時は最新)")
    ap.add_argument("--out", default="data/category_migrations_latest.json")
    args = ap.parse_args()

    snapshots = list_snapshots()
    if not snapshots:
        print("!! data/category_history/ にスナップショットなし。"
              "まず scripts/categorize_symbols.py を実行してください。")
        sys.exit(1)

    cur_path = Path(args.cur) if args.cur else snapshots[-1]
    if args.prev:
        prev_path = Path(args.prev)
    elif len(snapshots) >= 2:
        prev_path = snapshots[-2]
    else:
        prev_path = None

    cur = load_snapshot(cur_path)
    print(f"=== カテゴリ遷移検出 ===")
    print(f"  比較先 (cur): {cur_path.name}  ({cur.get('n_symbols')} 銘柄)")

    if prev_path is None:
        print("\n  ⚠ 履歴に 1 件しかないため遷移検出スキップ。")
        print("    → 翌週以降に再実行することで遷移が見える化。")
        migrations = []
        movements = []
    else:
        prev = load_snapshot(prev_path)
        print(f"  比較元 (prev): {prev_path.name}  ({prev.get('n_symbols')} 銘柄)")
        days_diff = (datetime.strptime(cur_path.stem, "%Y-%m-%d")
                     - datetime.strptime(prev_path.stem, "%Y-%m-%d")).days
        print(f"  期間: {days_diff} 日間\n")
        migrations = detect_migrations(prev, cur)
        movements = detect_theme_movements(prev, cur)

        # ── 遷移結果の表示 ──────────────────────────────────────
        if not migrations:
            print("  ✓ 遷移なし: 全銘柄のカテゴリが安定しています。")
        else:
            promotes = [m for m in migrations if m["type"] == "promote"]
            demotes = [m for m in migrations if m["type"] == "demote"]
            news = [m for m in migrations if m["type"] == "new"]
            removed = [m for m in migrations if m["type"] == "removed"]
            print(f"  検出: 昇格 {len(promotes)} / 降格 {len(demotes)} / "
                  f"新規 {len(news)} / 削除 {len(removed)}\n")
            for m in promotes:
                print(f"    🔼 {m['symbol']:<8} {m['from_cat']:<24} → {m['to_cat']:<24} "
                      f"({m['strategy_action']})")
            for m in demotes:
                print(f"    🔽 {m['symbol']:<8} {m['from_cat']:<24} → {m['to_cat']:<24} "
                      f"({m['strategy_action']})")
            for m in news:
                print(f"    ➕ {m['symbol']:<8} (NEW) → {m['to_cat']}")
            for m in removed:
                print(f"    ➖ {m['symbol']:<8} {m['from_cat']} → (REMOVED)")

        # ── テーマ移動シグナル ──────────────────────────────────
        if movements:
            print(f"\n=== テーマ別 同時移動シグナル ===")
            for tm in movements:
                print(f"  {tm['signal']:<35} テーマ: {tm['theme']}")
                for m in tm["moves"]:
                    arrow = "🔼" if m["delta"] > 0 else "🔽"
                    print(f"      {arrow} {m['symbol']:<8} {m['from']} → {m['to']}")

    # ── 現状のテーマ強弱 ──────────────────────────────────────
    # tracked >= 2 のテーマのみ表示 (単独銘柄テーマはシグナル性低い)
    themes = analyze_themes(cur)
    if themes:
        multi = {tn: t for tn, t in themes.items() if t["n_tracked"] >= 2}
        single_strong = {tn: t for tn, t in themes.items()
                         if t["n_tracked"] == 1 and t["avg_rank"] >= 4}
        print(f"\n=== 現状のテーマ強弱 (tracked>=2: {len(multi)} / "
              f"単独銘柄高ボラ: {len(single_strong)}) ===")
        if multi:
            print(f"  -- 複数銘柄テーマ (シグナル性高) --")
            sorted_multi = sorted(multi.items(), key=lambda x: -x[1]["avg_rank"])
            for tn, t in sorted_multi:
                syms_str = ", ".join(f"{m['symbol']}({m['category'][0]})"
                                       for m in t["members"])
                print(f"    {t['status']:<28} {tn:<22} "
                      f"(n={t['n_tracked']}, rank={t['avg_rank']:.1f})  [{syms_str}]")
        if single_strong:
            print(f"\n  -- 単独銘柄高ボラテーマ (上位 10) --")
            sorted_single = sorted(single_strong.items(), key=lambda x: -x[1]["avg_rank"])
            for tn, t in sorted_single[:10]:
                m = t["members"][0]
                print(f"    {t['status']:<28} {tn:<22} {m['symbol']}({m['category'][0]})")

    # ── 出力 JSON ────────────────────────────────────────────
    out_path = ROOT / args.out
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "cur_snapshot": cur_path.name,
        "prev_snapshot": prev_path.name if prev_path else None,
        "n_migrations": len(migrations),
        "migrations": migrations,
        "theme_movements": movements,
        "themes_current": themes,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=> 結果保存: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
