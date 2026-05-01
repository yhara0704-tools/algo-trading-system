#!/usr/bin/env python3
"""D5: 5/7 用 paper 投入 universe の期待値計算 + preflight チェック.

universe_active.json の全 entries を読み、戦略別・銘柄別の期待 PnL を集計。
余力圧縮係数 (1/N 並走時の現実的補正) を考慮した「現実見積もり」も計算。

出力:
  data/d5_preflight_expected_pnl.json
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))


def main() -> None:
    j = json.load(open("data/universe_active.json"))
    syms = j.get("symbols", [])
    # observation_only でないもののみ集計 (実 paper trade 対象)
    active = [s for s in syms if not s.get("observation_only", False)]
    print(f"universe entries: {len(syms)} (observation_only 除外後 active: {len(active)})\n")

    # 戦略別集計
    by_strategy = defaultdict(list)
    for s in active:
        by_strategy[s["strategy"]].append(s)

    print("=== 戦略別期待 PnL (1 銘柄全余力前提、理論最大) ===\n")
    grand_total = 0.0
    for strat in sorted(by_strategy.keys()):
        entries = by_strategy[strat]
        total = sum(float(s.get("oos_daily", 0) or 0) for s in entries)
        grand_total += total
        print(f"  {strat:20} {len(entries):3d} 銘柄, 合計 oos_daily = {total:>8,.0f} 円/日")
        for s in sorted(entries, key=lambda x: -float(x.get("oos_daily", 0) or 0)):
            print(f"    {s['symbol']:8} oos_daily = {s.get('oos_daily', 0):>7,.0f}")
    print(f"\n  GRAND TOTAL: {grand_total:>8,.0f} 円/日 (理論最大、1 銘柄全余力前提)")

    # 銘柄ごとの並走数 → 余力圧縮係数推定
    print("\n=== 銘柄ごとの戦略並走数 ===\n")
    by_symbol = defaultdict(list)
    for s in active:
        by_symbol[s["symbol"]].append(s["strategy"])

    n_multi = 0
    for sym in sorted(by_symbol.keys()):
        strats = by_symbol[sym]
        if len(strats) > 1:
            n_multi += 1
            print(f"  {sym:8} {len(strats)} 戦略並走: {strats}")
    print(f"\n  複数戦略並走銘柄: {n_multi} / {len(by_symbol)}")

    # 余力圧縮係数 (signal の発生時刻が一致しない前提で 1/3 程度を採用)
    REALISTIC_COMPRESSION = 0.40  # signal 発生時刻のばらつきにより、理論最大の 40% 取れる想定
    realistic = grand_total * REALISTIC_COMPRESSION
    print(f"\n=== 現実見積もり (余力圧縮 {REALISTIC_COMPRESSION:.1%} 適用) ===\n")
    print(f"  理論最大: {grand_total:>8,.0f} 円/日")
    print(f"  現実見積: {realistic:>8,.0f} 円/日 (圧縮率 {REALISTIC_COMPRESSION:.0%})")
    print(f"  目標 (3%/日 = 29,700 円/日) との差: {realistic - 29700:>+8,.0f}")
    print(f"  目標達成率: {realistic / 29700 * 100:.1f}%")

    # MicroScalp は短期決着で回転が速いため、追加で +50% (PnL 期待) 補正可能
    ms_total = sum(float(s.get("oos_daily", 0) or 0)
                   for s in active if s["strategy"] == "MicroScalp")
    if ms_total > 0:
        ms_realistic = ms_total * 0.30  # MicroScalp は 1/4 余力で確実に 30% 程度取れる
        print(f"\n  MicroScalp 単独補正:")
        print(f"    MicroScalp 理論: {ms_total:>8,.0f} 円/日")
        print(f"    MicroScalp 現実 (1/4 余力で 30%): {ms_realistic:>8,.0f} 円/日")

    # 既存 universe (D5 拡張前) との差分も表示
    d5_added = [s for s in active if s.get("source", "") in
                ("microscalp_30d_optimization", "d4_bb_short_validation",
                 "d4_pullback_validation")]
    d5_added_total = sum(float(s.get("oos_daily", 0) or 0) for s in d5_added)
    print(f"\n=== D5 拡張分の期待値 ===")
    print(f"  D5 追加銘柄数: {len(d5_added)}")
    print(f"  D5 追加合計 oos_daily: {d5_added_total:>8,.0f} 円/日 (理論最大)")
    print(f"  D5 現実見積: {d5_added_total * REALISTIC_COMPRESSION:>8,.0f} 円/日")

    out = {
        "generated_at": datetime.now(JST).isoformat(),
        "active_count": len(active),
        "total_count": len(syms),
        "grand_total_oos": grand_total,
        "realistic_compression": REALISTIC_COMPRESSION,
        "realistic_pnl_per_day": realistic,
        "target_pnl_per_day": 29700,
        "achievement_pct": realistic / 29700 * 100,
        "by_strategy": {
            k: {"n": len(v), "total_oos": sum(float(s.get("oos_daily", 0) or 0) for s in v)}
            for k, v in by_strategy.items()
        },
        "multi_strategy_symbols": {
            sym: strats for sym, strats in by_symbol.items() if len(strats) > 1
        },
        "d5_added_count": len(d5_added),
        "d5_added_total_oos": d5_added_total,
    }
    Path("data/d5_preflight_expected_pnl.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: data/d5_preflight_expected_pnl.json")


if __name__ == "__main__":
    main()
