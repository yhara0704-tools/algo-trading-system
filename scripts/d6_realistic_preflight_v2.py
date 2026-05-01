#!/usr/bin/env python3
"""D6e: 実測値ベースの preflight v2.

universe oos_daily ではなく、D6a で取得した 60日実測 PnL を使って
真の期待値を算定する。MacdRci 以外は oos_daily にフォールバック。

出力:
  data/d6_realistic_preflight_v2.json
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))


def main() -> None:
    universe = json.load(open("data/universe_active.json"))
    health = json.load(open("data/d6_macd_rci_health_check.json"))
    macd_actuals = {r["symbol"]: r["pnl_per_day"] for r in health["rows"]}

    syms = universe["symbols"]
    active = [s for s in syms if not s.get("observation_only", False) or s.get("force_paper", False)]
    print(f"=== D6e: 実測値ベース preflight v2 ===\n")
    print(f"active entries: {len(active)}\n")

    rows = []
    by_strategy = defaultdict(list)
    for s in active:
        sym, strat = s["symbol"], s["strategy"]
        oos_orig = float(s.get("oos_daily", 0) or 0)
        # MacdRci 実測値があれば使用 (実測値優先)
        if strat == "MacdRci" and sym in macd_actuals:
            real_pnl = macd_actuals[sym]
            source = "60d_actual"
        else:
            real_pnl = oos_orig
            source = "oos_daily"
        rows.append({"symbol": sym, "strategy": strat, "oos_daily": oos_orig,
                    "real_pnl_per_day": real_pnl, "source": source})
        by_strategy[strat].append({"symbol": sym, "real_pnl": real_pnl, "oos_daily": oos_orig})

    print(f"{'strategy':18} {'n':>3} {'real_pnl':>9} {'oos_daily':>9} {'delta':>8}")
    print(f"{'-'*52}")
    grand_real = 0
    grand_oos = 0
    for strat in sorted(by_strategy.keys()):
        ents = by_strategy[strat]
        real_sum = sum(e["real_pnl"] for e in ents)
        oos_sum = sum(e["oos_daily"] for e in ents)
        grand_real += real_sum
        grand_oos += oos_sum
        delta = real_sum - oos_sum
        print(f"  {strat:16} {len(ents):>3} {real_sum:>+9.0f} {oos_sum:>+9.0f} {delta:>+8.0f}")
    print(f"  {'-'*48}")
    print(f"  {'TOTAL':16} {len(active):>3} {grand_real:>+9.0f} {grand_oos:>+9.0f} {grand_real-grand_oos:>+8.0f}")

    # 圧縮係数 (Day 5 と同じ 40%)
    real_compressed = grand_real * 0.4
    oos_compressed = grand_oos * 0.4
    target = 29_700  # 3% / 日

    print()
    print(f"=== 真の期待値推定 (圧縮率 40%) ===\n")
    print(f"  理論最大 (実測ベース):  {grand_real:>+9.0f} 円/日")
    print(f"  現実見積 (実測ベース):  {real_compressed:>+9.0f} 円/日")
    print(f"  目標 (3%/日):           {target:>+9.0f} 円/日")
    print(f"  目標達成率:                {real_compressed/target*100:>5.1f}%")
    print(f"  目標まで不足:           {target-real_compressed:>+9.0f} 円/日")
    print()
    print(f"  (参考) oos_daily ベース 現実見積: {oos_compressed:>+9.0f} 円/日")
    print(f"  実測値補正 (oos との差分):        {real_compressed-oos_compressed:>+9.0f} 円/日")

    # 銘柄別ランキング
    print(f"\n=== 実測 PnL/日 ベース 銘柄 × 戦略 トップ 10 ===\n")
    sorted_rows = sorted(rows, key=lambda r: -r["real_pnl_per_day"])
    print(f"  {'rank':>4} {'symbol':8} {'strategy':14} {'pnl/d':>7} {'oos_d':>7} {'src':<12}")
    for i, r in enumerate(sorted_rows[:10], 1):
        print(f"  {i:>4} {r['symbol']:8} {r['strategy']:14} "
              f"{r['real_pnl_per_day']:>+7.0f} {r['oos_daily']:>+7.0f} {r['source']:<12}")

    # ── D2 capital guard 効果 (5/1 timeline simulation で +3,000 円/日 推定) ──
    d2_uplift_per_day = 3_000
    final_estimate = real_compressed + d2_uplift_per_day
    print()
    print(f"=== D2 capital guard 効果加算 ===\n")
    print(f"  D2 capital guard uplift:  {d2_uplift_per_day:>+9.0f} 円/日 (5/1 timeline 検証)")
    print(f"  最終期待値:                {final_estimate:>+9.0f} 円/日")
    print(f"  目標達成率:                {final_estimate/target*100:>5.1f}%")
    if final_estimate >= target:
        print(f"  ✅ 目標達成 (29,700 円/日)")
    else:
        print(f"  ❗ 目標まで不足: {target-final_estimate:+.0f} 円/日")

    out = {
        "generated_at": datetime.now(JST).isoformat(),
        "active_entries": len(active),
        "rows": rows,
        "by_strategy_real": {k: sum(e["real_pnl"] for e in v) for k, v in by_strategy.items()},
        "by_strategy_oos": {k: sum(e["oos_daily"] for e in v) for k, v in by_strategy.items()},
        "grand_real_per_day": grand_real,
        "grand_oos_per_day": grand_oos,
        "compressed_real_per_day": real_compressed,
        "compressed_oos_per_day": oos_compressed,
        "d2_uplift_per_day": d2_uplift_per_day,
        "final_estimate_per_day": final_estimate,
        "target_per_day": target,
        "achievement_rate": final_estimate / target * 100,
    }
    Path("data/d6_realistic_preflight_v2.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: data/d6_realistic_preflight_v2.json")


if __name__ == "__main__":
    main()
