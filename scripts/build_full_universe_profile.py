#!/usr/bin/env python3
"""universe 全銘柄プロファイル — universe_active + robust 候補を一気にプロファイル化.

ユーザー指針 (2026-04-30 17:23):
> 銘柄ごとの癖って絶対あるからね。人間が感覚ならこちらはデータで勝負しなきゃ。

この版では universe_active.json + macd_rci_params.json (robust) + strategy_fit_map.json
(robust) の和集合 (約 35 銘柄) を一括でプロファイル化し、銘柄ごとに

  - 既存の癖プロファイル (gap stats / observe_min / 寄り天率 / vol_decay)
  - 各銘柄に既に適用されている戦略リスト (universe_active から)
  - 戦略相性推奨 ("MicroScalp_pri" / "MicroScalp_back" / "trend" / "exclude")
  - 初動予測スコア (best same_dir%) と推奨 observe_min

を出力。これを weekly cron で更新すれば「データドリブンな銘柄選定」の真の土台になる。
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 既存スクリプトのロジックを流用
from scripts.build_symbol_open_profile import analyze_symbol, OBSERVE_MINS  # noqa: E402

JST = "Asia/Tokyo"


def load_all_symbols() -> dict[str, dict]:
    """3 つの canonical から銘柄リスト + 既存戦略を集める."""
    out: dict[str, dict] = defaultdict(lambda: {"strategies": set(), "sources": set()})

    # universe_active.json
    ua_path = ROOT / "data/universe_active.json"
    if ua_path.exists():
        ua = json.loads(ua_path.read_text())
        for entry in ua.get("symbols", []):
            sym = entry.get("symbol")
            if not sym:
                continue
            out[sym]["strategies"].add(entry.get("strategy", "unknown"))
            out[sym]["sources"].add("universe_active")
            out[sym]["name"] = entry.get("name", "")
            out[sym].setdefault("oos_daily", []).append(entry.get("oos_daily"))

    # macd_rci_params.json (robust)
    mc_path = ROOT / "data/macd_rci_params.json"
    if mc_path.exists():
        mc = json.loads(mc_path.read_text())
        for sym, v in mc.items():
            if not isinstance(v, dict) or not v.get("robust"):
                continue
            out[sym]["strategies"].add("MacdRci")
            out[sym]["sources"].add("macd_rci_robust")
            out[sym].setdefault("oos_daily", []).append(v.get("oos_daily"))

    # strategy_fit_map.json (robust)
    fm_path = ROOT / "data/strategy_fit_map.json"
    if fm_path.exists():
        fm = json.loads(fm_path.read_text())
        for sym, v in fm.items():
            if not isinstance(v, dict):
                continue
            for strat_name, strat in v.get("strategies", {}).items():
                if not isinstance(strat, dict):
                    continue
                if strat.get("robust"):
                    out[sym]["strategies"].add(strat_name)
                    out[sym]["sources"].add("strategy_fit_robust")

    # set → list に変換 + oos_daily の代表値抽出
    cleaned = {}
    for sym, info in out.items():
        oos_list = [v for v in info.get("oos_daily", []) if v is not None]
        cleaned[sym] = {
            "strategies": sorted(info["strategies"]),
            "sources": sorted(info["sources"]),
            "name": info.get("name", ""),
            "oos_daily_max": max(oos_list) if oos_list else None,
        }
    return cleaned


def classify_symbol(prof: dict, existing_strats: list[str]) -> dict:
    """プロファイルと既存戦略から「適合戦略推奨」を判定する.

    判定軸:
      - vol_decay@30min: MicroScalp は >=1.5% が必要 (TP=5円が届く)
      - best_observe_min same_dir%: 50%超で予測力あり
      - yoriten_pct: 60%+ ならショート優位、25%以下ならトレンド継続型
      - 既存戦略との重複: MacdRci/Scalp 等が既にあれば「back-up」評価
    """
    vol30 = prof.get("vol_decay_range_pct", {}).get(30, 0)
    yoriten = prof.get("yoriten_pct", 0)
    best = prof.get("best_observe_min") or {}
    best_same_dir = best.get("same_dir_pct") or 0
    best_obs = best.get("observe_min")
    abs_gap = prof.get("gap_stats", {}).get("abs_mean", 0)

    micro_score = 0.0
    micro_notes = []
    if vol30 >= 1.5:
        micro_score += 30
        micro_notes.append(f"vol@30={vol30:.2f}% >=1.5 (MicroScalp 適格)")
    elif vol30 < 0.5:
        micro_score -= 30
        micro_notes.append(f"vol@30={vol30:.2f}% <0.5 (ボラ不足)")
    if best_same_dir >= 60:
        micro_score += 20
        micro_notes.append(f"best obs={best_obs}min same_dir={best_same_dir}% (予測力○)")
    elif best_same_dir < 40:
        micro_score -= 10
    if abs_gap >= 1.0:
        micro_score += 10
        micro_notes.append(f"abs_gap={abs_gap:.2f}% (ギャップ豊富)")
    if yoriten >= 50:
        micro_score += 10
        micro_notes.append(f"yoriten={yoriten}% (ショート優位)")

    # 戦略推奨
    has_macdrci = "MacdRci" in existing_strats
    has_scalp = "Scalp" in existing_strats or "EnhancedScalp" in existing_strats
    has_breakout = "Breakout" in existing_strats

    rec: list[str] = []
    if micro_score >= 40:
        rec.append("MicroScalp_pri")
    elif micro_score >= 20:
        rec.append("MicroScalp_back")
    elif micro_score < 0:
        rec.append("MicroScalp_exclude")

    if has_macdrci and (yoriten <= 25 or vol30 < 1.0):
        rec.append("MacdRci_keep")
    if has_scalp:
        rec.append("Scalp_keep")
    if has_breakout and abs_gap >= 1.0:
        rec.append("Breakout_keep")

    bias = "neutral"
    if yoriten >= 60:
        bias = "short_pref_open"
    elif yoriten <= 25 and abs_gap >= 0.5:
        bias = "trend_follow"

    return {
        "micro_scalp_score": round(micro_score, 1),
        "micro_scalp_notes": micro_notes,
        "open_bias": bias,
        "best_observe_min": best_obs,
        "best_same_dir_pct": best_same_dir,
        "strategy_recommendation": rec,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="data/symbol_open_profile_full.json")
    ap.add_argument("--sleep", type=float, default=0.3, help="銘柄間 yfinance sleep")
    ap.add_argument("--max", type=int, default=0, help="0=無制限。デバッグ用")
    args = ap.parse_args()

    sym_info = load_all_symbols()
    syms = sorted(sym_info.keys())
    if args.max > 0:
        syms = syms[:args.max]
    print(f"=== universe 全銘柄プロファイル === {len(syms)} symbols, {args.days}d "
          f"(sleep={args.sleep}s)")

    profiles: dict = {}
    classified: dict = {}
    fail_count = 0
    for i, sym in enumerate(syms, 1):
        info = sym_info[sym]
        try:
            p = analyze_symbol(sym, days=args.days)
        except Exception as exc:
            print(f"  [{i:>2}/{len(syms)}] {sym} err: {exc}")
            fail_count += 1
            time.sleep(args.sleep)
            continue
        if p is None:
            print(f"  [{i:>2}/{len(syms)}] {sym} no data")
            fail_count += 1
            time.sleep(args.sleep)
            continue
        # day_records は嵩むので除外 (個別 build_symbol_open_profile.py で取れる)
        p_compact = {k: v for k, v in p.items() if k != "day_records"}
        profiles[sym] = p_compact

        cls = classify_symbol(p_compact, info["strategies"])
        classified[sym] = {
            "name": info.get("name", ""),
            "existing_strategies": info["strategies"],
            "sources": info["sources"],
            "oos_daily_max": info.get("oos_daily_max"),
            **cls,
        }

        bg = p["best_observe_min"]
        bg_str = (f"obs={bg['observe_min']:>2}min sd={bg['same_dir_pct']:>5}%"
                  if bg else "n/a              ")
        print(f"  [{i:>2}/{len(syms)}] {sym:<7} "
              f"vol@30={p['vol_decay_range_pct'].get(30, 0):>5.2f}% "
              f"yoriten={p['yoriten_pct']:>5}% gap={p['gap_stats']['abs_mean']:>4.2f}% "
              f"{bg_str} "
              f"micro={cls['micro_scalp_score']:>+5.1f} "
              f"rec={','.join(cls['strategy_recommendation'])[:35]}")
        time.sleep(args.sleep)

    print(f"\n=== サマリ ===")
    print(f"  total={len(syms)}, profiled={len(profiles)}, failed={fail_count}")

    # 戦略推奨別集計
    by_rec = defaultdict(list)
    for sym, c in classified.items():
        for r in c["strategy_recommendation"]:
            by_rec[r].append(sym)
    print("\n=== strategy_recommendation 別 ===")
    for r in ["MicroScalp_pri", "MicroScalp_back", "MicroScalp_exclude",
              "MacdRci_keep", "Scalp_keep", "Breakout_keep"]:
        syms_r = by_rec.get(r, [])
        if syms_r:
            print(f"  {r}: {len(syms_r)} 銘柄  {syms_r}")

    by_bias = defaultdict(list)
    for sym, c in classified.items():
        by_bias[c["open_bias"]].append(sym)
    print("\n=== open_bias 別 ===")
    for b, syms_in in by_bias.items():
        print(f"  {b}: {len(syms_in)}  {syms_in}")

    # MicroScalp 主力候補 Top 10 (micro_scalp_score 降順)
    micro_top = sorted(classified.items(),
                       key=lambda kv: -kv[1]["micro_scalp_score"])[:10]
    print("\n=== MicroScalp 主力候補 Top 10 (score 降順) ===")
    for sym, c in micro_top:
        print(f"  {sym} {c['name']:<25} score={c['micro_scalp_score']:>+5.1f} "
              f"bias={c['open_bias']:<18} obs={c['best_observe_min']}min "
              f"sd={c['best_same_dir_pct']}%")

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "n_symbols": len(syms),
        "n_profiled": len(profiles),
        "days": args.days,
        "profiles": profiles,
        "classified": classified,
        "summary": {
            "by_recommendation": {k: v for k, v in by_rec.items()},
            "by_bias": {k: v for k, v in by_bias.items()},
            "micro_scalp_top10": [
                {"symbol": s, "name": c["name"],
                 "score": c["micro_scalp_score"],
                 "bias": c["open_bias"],
                 "best_observe_min": c["best_observe_min"]}
                for s, c in micro_top
            ],
        }
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=> 結果保存: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
