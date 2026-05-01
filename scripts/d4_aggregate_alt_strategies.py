#!/usr/bin/env python3
"""D4 後処理: alt 戦略の universe 入り判定と組み合わせ効果分析.

判定基準を緩めて (WR>=45, pnl/day>=200) 候補銘柄を抽出し、
銘柄ごとに「どの戦略を採用すべきか」を期待値ベースで決定する。

既存 universe (MacdRci) と alt 戦略の **同銘柄重複** を整理し、
universe_active.json への投入計画を確定する。
"""
from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    j = json.load(open("data/d4_alt_strategies_validation.json"))
    results = j["results"]

    # 緩めた基準
    THRESHOLDS = {
        "BBShort": {"wr": 45, "pf": 1.5, "pnl_day": 200, "trades_min": 8},
        "Pullback": {"wr": 45, "pf": 1.0, "pnl_day": 200, "trades_min": 8},
        "SwingDonchian": {"pnl_day": 100, "sharpe": 1.0, "trades_min": 2},
    }

    candidates = {"BBShort": [], "Pullback": [], "SwingDonchian": []}

    for sname, syms in results.items():
        for sym, r in syms.items():
            if sname == "SwingDonchian":
                if (r.get("pnl_per_day", 0) >= THRESHOLDS[sname]["pnl_day"]
                    and r.get("sharpe", 0) >= THRESHOLDS[sname]["sharpe"]
                    and r.get("trades", 0) >= THRESHOLDS[sname]["trades_min"]):
                    candidates[sname].append(r)
            else:
                if (r.get("wr", 0) >= THRESHOLDS[sname]["wr"]
                    and r.get("pf", 0) >= THRESHOLDS[sname]["pf"]
                    and r.get("pnl_per_day", 0) >= THRESHOLDS[sname]["pnl_day"]
                    and r.get("trades", 0) >= THRESHOLDS[sname]["trades_min"]):
                    candidates[sname].append(r)

    print("=== D4 alt 戦略 universe 入り候補 (緩和基準) ===\n")
    for sname, cands in candidates.items():
        cands.sort(key=lambda r: r.get("pnl_per_day", 0), reverse=True)
        print(f"\n{sname}: {len(cands)} 候補 (基準 {THRESHOLDS[sname]})")
        for r in cands:
            print(f"  {r['symbol']:8} trades={r['trades']:3d} wr={r['wr']:5.1f}% "
                  f"pf={r['pf']:5.2f} pnl/day={r['pnl_per_day']:7.0f} "
                  + (f"sharpe={r['sharpe']:.2f}" if r.get('sharpe') else ''))

    # ── 銘柄別 best 戦略選択 (単一ペアのみ) ───────────────────────
    print("\n\n=== 銘柄別 alt 戦略 best (期待値順) ===\n")
    by_symbol = {}
    for sname, cands in candidates.items():
        for r in cands:
            sym = r["symbol"]
            r["strategy"] = sname
            if sym not in by_symbol or r["pnl_per_day"] > by_symbol[sym]["pnl_per_day"]:
                by_symbol[sym] = r

    sorted_syms = sorted(by_symbol.items(), key=lambda x: -x[1]["pnl_per_day"])
    total = 0
    for sym, r in sorted_syms:
        print(f"  {sym:8} {r['strategy']:15} trades={r['trades']:3d} "
              f"wr={r['wr']:5.1f}% pf={r['pf']:5.2f} pnl/day={r['pnl_per_day']:7.0f}")
        total += r["pnl_per_day"]
    print(f"\n  全銘柄合計: {total:,} 円/日 (理論期待値、1 銘柄全余力前提)")

    # ── 既存 MacdRci universe との重複 ─────────────────────────
    print("\n\n=== 既存 universe (MacdRci) との競合分析 ===\n")
    try:
        u = json.load(open("data/universe_active.json"))
        existing_pairs = {(s["symbol"], s["strategy"]) for s in u.get("symbols", [])
                         if not s.get("observation_meta", {}).get("force_paper") in (False,)}
        print(f"既存 universe entries: {len(existing_pairs)}")
        existing_macd = {s for s, st in existing_pairs if st == "MacdRci"}
        alt_syms = set(by_symbol.keys())
        overlap = existing_macd & alt_syms
        new_syms = alt_syms - existing_macd
        print(f"既存 MacdRci 銘柄: {len(existing_macd)}")
        print(f"alt 戦略候補: {len(alt_syms)}")
        print(f"重複 (MacdRci + alt 並走候補): {len(overlap)}")
        print(f"  重複銘柄: {sorted(overlap)}")
        print(f"alt のみ (新規 universe 候補): {len(new_syms)}")
        print(f"  新規銘柄: {sorted(new_syms)}")
    except Exception as e:
        print(f"既存 universe 読み込みエラー: {e}")

    # ── Output ──────────────────────────────────────────────────
    out_path = Path("data/d4_universe_candidates_relaxed.json")
    out_path.write_text(json.dumps({
        "thresholds": THRESHOLDS,
        "candidates_by_strategy": candidates,
        "best_per_symbol": by_symbol,
        "total_pnl_per_day": total,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    main()
