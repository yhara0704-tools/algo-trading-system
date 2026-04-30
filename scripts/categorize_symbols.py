#!/usr/bin/env python3
"""銘柄カテゴライザー — profile を 6 カテゴリに分類 + テンプレ戦略を付与.

ユーザー指針 (2026-04-30 17:34):
> 銘柄の癖をカテゴライズすることでこういう銘柄はこうカテゴライズされやすい
> みたいなのが見えてこれば、新規で良さそうな銘柄を見つけた時に
> 最短で最適手法に辿り着く可能性もあります。

入力: data/symbol_open_profile_full.json
出力: data/symbol_categories.json

カテゴリ定義 (ルールベース、シンプルかつ拡張容易):
  A: 高ボラ + ショート優位 (寄り天 >= 50%)
  B: 高ボラ + 順張り (寄り天 < 25%)
  C: 中ボラ + 順張り
  D: 中ボラ + 中立 (寄り天 25-50%)
  E: 低ボラ + 順張り (MacdRci 専用)
  F: 低ボラ + 中立 / NG (除外推奨)

各カテゴリにはテンプレ戦略パラメータ + 推奨 universe 用例 を付与。
新規銘柄を分類する際は、profile 計算 → カテゴリ判定 → テンプレ適用 だけで OK。
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── カテゴリ定義 ────────────────────────────────────────────────────────
# vol_decay@30min, yoriten_pct, abs_gap, micro_score の 4 軸でルール判定
CATEGORIES: dict[str, dict] = {
    "A_high_vol_short_pref": {
        "label": "高ボラ + ショート優位 (寄り天高い)",
        "rule": lambda p: (
            p["vol30"] >= 2.5 and p["yoriten"] >= 50.0
        ),
        "template_strategies": ["MicroScalp_short", "BbShort", "Scalp"],
        "template_params": {
            "MicroScalp": {
                "tp_jpy": 10, "sl_jpy": 5, "cooldown_bars": 5,
                "max_trades_per_day": 25, "allow_short": True, "allow_long": False,
                "allowed_time_windows": ["09:00-09:30", "12:30-15:00"],
                "open_bias_mode": True,
            },
        },
        "color": "red",
        "examples": [],
    },
    "B_high_vol_trend_follow": {
        "label": "高ボラ + 順張り/中立 (寄り天 50% 未満)",
        "rule": lambda p: (
            p["vol30"] >= 2.5 and p["yoriten"] < 50.0
        ),
        "template_strategies": ["MicroScalp", "Breakout", "MacdRci"],
        "template_params": {
            "MicroScalp": {
                "tp_jpy": 10, "sl_jpy": 5, "cooldown_bars": 5,
                "max_trades_per_day": 20, "allow_short": True, "allow_long": True,
                "allowed_time_windows": ["09:00-09:30", "12:30-15:00"],
                "open_bias_mode": False,
            },
        },
        "color": "orange",
        "examples": [],
    },
    "C_mid_vol_trend": {
        "label": "中ボラ + 順張り (universe 主力)",
        "rule": lambda p: (
            1.5 <= p["vol30"] < 2.5 and p["yoriten"] < 30.0
        ),
        "template_strategies": ["MicroScalp_back", "Scalp", "MacdRci", "Breakout"],
        "template_params": {
            "MicroScalp": {
                "tp_jpy": 8, "sl_jpy": 4, "cooldown_bars": 5,
                "max_trades_per_day": 15, "allow_short": True, "allow_long": True,
                "allowed_time_windows": ["09:00-09:30"],
                "open_bias_mode": False,
            },
        },
        "color": "yellow",
        "examples": [],
    },
    "D_mid_vol_neutral": {
        "label": "中ボラ + 中立 (寄り天やや高い)",
        "rule": lambda p: (
            1.0 <= p["vol30"] < 2.5 and 25.0 <= p["yoriten"] < 50.0
        ),
        "template_strategies": ["Scalp", "MacdRci"],
        "template_params": {},
        "color": "green",
        "examples": [],
    },
    "E_low_vol_trend": {
        "label": "低ボラ + 順張り (MacdRci 専用)",
        "rule": lambda p: (
            0.6 <= p["vol30"] < 1.5 and p["yoriten"] < 30.0
        ),
        "template_strategies": ["MacdRci", "Scalp_low_freq"],
        "template_params": {},
        "color": "blue",
        "examples": [],
    },
    "F_low_vol_or_ng": {
        "label": "低ボラ / 不適合 (除外推奨)",
        "rule": lambda p: p["vol30"] < 0.6,
        "template_strategies": [],  # 個別判断
        "template_params": {},
        "color": "gray",
        "examples": [],
    },
}

# カテゴリ判定の優先順位 (上から評価、最初に該当したカテゴリに割当)
CATEGORY_ORDER = [
    "A_high_vol_short_pref",
    "B_high_vol_trend_follow",
    "C_mid_vol_trend",
    "D_mid_vol_neutral",
    "E_low_vol_trend",
    "F_low_vol_or_ng",
]


def _vol30(prof: dict) -> float:
    """vol_decay_range_pct から 30min の値を取り出す (JSON 経由は str キー)."""
    vd = prof.get("vol_decay_range_pct", {}) or {}
    return float(vd.get(30) or vd.get("30") or 0)


def categorize(prof: dict) -> str:
    p = {
        "vol30": _vol30(prof),
        "yoriten": prof.get("yoriten_pct", 0),
        "abs_gap": prof.get("gap_stats", {}).get("abs_mean", 0),
        "micro_score": 0,  # classified 側で算出済みなのでここでは未使用
    }
    for cat_id in CATEGORY_ORDER:
        cat = CATEGORIES[cat_id]
        try:
            if cat["rule"](p):
                return cat_id
        except Exception:
            continue
    return "F_low_vol_or_ng"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/symbol_open_profile_full.json")
    ap.add_argument("--out", default="data/symbol_categories.json")
    args = ap.parse_args()

    in_path = ROOT / args.input
    if not in_path.exists():
        print(f"!! {in_path} not found, run build_full_universe_profile.py first")
        sys.exit(1)
    full = json.loads(in_path.read_text())
    profiles = full.get("profiles", {})
    classified = full.get("classified", {})

    # 各銘柄をカテゴリに割当
    sym_to_cat: dict[str, str] = {}
    cat_members: dict[str, list[dict]] = {c: [] for c in CATEGORY_ORDER}
    for sym, prof in profiles.items():
        cat = categorize(prof)
        sym_to_cat[sym] = cat
        info = classified.get(sym, {})
        cat_members[cat].append({
            "symbol": sym,
            "name": info.get("name", ""),
            "vol30": round(_vol30(prof), 2),
            "yoriten": prof.get("yoriten_pct", 0),
            "abs_gap": prof.get("gap_stats", {}).get("abs_mean", 0),
            "best_observe_min": (prof.get("best_observe_min") or {}).get("observe_min"),
            "best_same_dir_pct": (prof.get("best_observe_min") or {}).get("same_dir_pct"),
            "micro_scalp_score": info.get("micro_scalp_score"),
            "existing_strategies": info.get("existing_strategies", []),
            "open_bias": info.get("open_bias", "neutral"),
        })

    # サンプル抽出 (各カテゴリの上位 5 銘柄)
    for cat_id in CATEGORY_ORDER:
        members = cat_members[cat_id]
        members.sort(key=lambda r: -(r.get("micro_scalp_score") or 0))
        CATEGORIES[cat_id]["examples"] = members[:5]

    # ── 表示 ────────────────────────────────────────────────────────────
    print("=== 銘柄カテゴライザー結果 ===\n")
    for cat_id in CATEGORY_ORDER:
        cat = CATEGORIES[cat_id]
        members = cat_members[cat_id]
        print(f"### {cat_id}: {cat['label']}  ({len(members)} 銘柄)")
        print(f"    推奨戦略: {', '.join(cat['template_strategies']) if cat['template_strategies'] else '(個別判断)'}")
        if members:
            print(f"    銘柄一覧:")
            for m in members:
                strat_str = ",".join(m["existing_strategies"][:3])
                print(f"      {m['symbol']:<7} {m.get('name', ''):<22} "
                      f"vol={m['vol30']:>5.2f}% 寄り天={m['yoriten']:>5}% "
                      f"obs={m['best_observe_min']}min sd={m['best_same_dir_pct']}% "
                      f"score={m['micro_scalp_score'] or 0:>+5} "
                      f"既存=[{strat_str}]")
        print()

    # ── 出力 JSON ────────────────────────────────────────────────────────
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_payload = {
        "generated_at": datetime.now().isoformat(),
        "source": str(in_path.relative_to(ROOT)),
        "n_symbols": len(profiles),
        "categories": {
            cat_id: {
                "label": cat["label"],
                "template_strategies": cat["template_strategies"],
                "template_params": cat["template_params"],
                "color": cat["color"],
                "n_members": len(cat_members[cat_id]),
                "members": cat_members[cat_id],
            }
            for cat_id, cat in CATEGORIES.items()
        },
        "symbol_to_category": sym_to_cat,
    }
    out_path.write_text(json.dumps(out_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"=> 結果保存: {out_path.relative_to(ROOT)}")
    print(f"\n累計: {sum(len(cat_members[c]) for c in CATEGORY_ORDER)} 銘柄を 6 カテゴリに分類")

    # ── 履歴スナップショット保存 (動的カテゴリ追跡用) ────────────────────
    # ユーザー指針 (2026-04-30 17:52): カテゴライズされた中身は動的に変化する。
    # ずっと強いテーマもあれば、すぐ廃れるテーマもある。
    # → 週次スナップショットを蓄積して、後で遷移検出 + テーマ強弱分析に使う。
    today_str = datetime.now().strftime("%Y-%m-%d")
    history_dir = ROOT / "data/category_history"
    history_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = history_dir / f"{today_str}.json"
    snapshot_payload = {
        "snapshot_date": today_str,
        "n_symbols": len(profiles),
        "symbol_to_category": sym_to_cat,
        "category_counts": {c: len(cat_members[c]) for c in CATEGORY_ORDER},
        "symbol_features": {
            sym: {
                "category": sym_to_cat[sym],
                "vol30": round(_vol30(profiles[sym]), 2),
                "yoriten": profiles[sym].get("yoriten_pct", 0),
                "best_observe_min": (profiles[sym].get("best_observe_min") or {}).get("observe_min"),
            }
            for sym in profiles
        },
    }
    snapshot_path.write_text(
        json.dumps(snapshot_payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"=> 履歴スナップショット保存: {snapshot_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
