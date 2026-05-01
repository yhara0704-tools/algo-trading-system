#!/usr/bin/env python3
"""D7c: lot_multiplier 反映の preflight v3.

機械分散 vs 期待値駆動 (lot_multiplier 反映) の期待 PnL 比較。
各 entry の expected_value に lot_multiplier を乗じ、capital cap で
頭打ち調整した上で日次 PnL を試算する。

仮定:
  - 1 銘柄あたり最大ロット = buying_power(990k) × position_pct(0.30) × tier_margin(1)
    × lot_multiplier × LIVE_POSITION_SCALE(1.0)
  - 複数 active 同時保有時は concurrent_value_cap (= 1.5 × buying_power)
    でロット圧縮
  - 日次 PnL ∝ position_value (仮定として線形)

出力:
  data/d7_preflight_v3.json
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

CAPITAL = 990_000  # buying power = 300k × 3.3 (T1 信用)
POSITION_PCT = 0.30
CONCURRENT_VALUE_CAP_RATIO = 1.5
COMPRESSION = 0.4  # signal タイミングばらつき + 余力競合の現実圧縮率
D2_UPLIFT = 3_000  # capital guard の +3,000 円/日


def main() -> None:
    universe = json.load(open("data/universe_active.json"))
    health = json.load(open("data/d6_macd_rci_health_check.json"))
    macd_actuals = {r["symbol"]: r["pnl_per_day"] for r in health["rows"]}

    syms = universe["symbols"]
    active = [s for s in syms if not s.get("observation_only", False) or s.get("force_paper", False)]
    print(f"=== D7c: 期待値駆動 lot_multiplier preflight v3 ===\n")
    print(f"active entries: {len(active)}")
    print(f"buying power: {CAPITAL:,.0f} JPY (T1 信用 3.3x)")
    print(f"per-position base ratio: {POSITION_PCT*100:.0f}% × lot_multiplier\n")

    rows = []
    grand_ev_mech = 0.0
    grand_ev_d7 = 0.0
    base_position_value = CAPITAL * POSITION_PCT  # 297,000

    for s in active:
        sym, strat = s["symbol"], s["strategy"]
        oos_daily = float(s.get("oos_daily", 0) or 0)
        if strat == "MacdRci" and sym in macd_actuals:
            ev = macd_actuals[sym]
        else:
            ev = oos_daily
        mult = float(s.get("lot_multiplier", 1.0) or 1.0)

        # 機械分散版: ev そのまま (mult=1)
        ev_mech = ev
        # 期待値駆動版: ev × mult (lot pump → PnL も同比率と仮定)
        ev_d7 = ev * mult

        grand_ev_mech += ev_mech
        grand_ev_d7 += ev_d7

        rows.append({
            "symbol": sym, "strategy": strat,
            "expected_value": round(ev, 0), "lot_multiplier": mult,
            "ev_mech": round(ev_mech, 0), "ev_d7": round(ev_d7, 0),
            "delta": round(ev_d7 - ev_mech, 0),
        })

    # 圧縮 + D2 加算
    real_mech = grand_ev_mech * COMPRESSION + D2_UPLIFT
    real_d7 = grand_ev_d7 * COMPRESSION + D2_UPLIFT
    target = 29_700

    print(f"=== 期待 PnL 比較 ===\n")
    print(f"{'':30} {'機械分散':>10} {'D7 期待値駆動':>14}")
    print(f"  理論最大:                  {grand_ev_mech:>+10.0f} {grand_ev_d7:>+14.0f}")
    print(f"  圧縮 ({COMPRESSION*100:.0f}%) + D2 +{D2_UPLIFT}: {real_mech:>+10.0f} {real_d7:>+14.0f}")
    print(f"  目標 (3%/日):              {target:>+10.0f} {target:>+14.0f}")
    print(f"  目標達成率:                 {real_mech/target*100:>9.1f}% {real_d7/target*100:>13.1f}%")
    print(f"  uplift (D7 - 機械分散):                  {real_d7-real_mech:>+14.0f} 円/日")

    # トップ寄与銘柄
    print(f"\n=== D7 PnL トップ 10 寄与銘柄 ===\n")
    print(f"  {'sym':10} {'strat':16} {'ev':>7} {'mult':>5} {'ev_d7':>7} {'delta':>7}")
    for r in sorted(rows, key=lambda x: -x["ev_d7"])[:10]:
        print(f"  {r['symbol']:10} {r['strategy']:16} "
              f"{r['expected_value']:>+7.0f} {r['lot_multiplier']:>5.2f} "
              f"{r['ev_d7']:>+7.0f} {r['delta']:>+7.0f}")

    # ── 信用枠 cap 警告 ──
    print(f"\n=== 信用枠 cap 警告 ===\n")
    cap_warning = []
    for r in rows:
        max_pos_val = base_position_value * r["lot_multiplier"]
        # 1 銘柄が信用枠 buying_power × CONCURRENT_VALUE_CAP_RATIO の 50% 以上
        if max_pos_val > CAPITAL * CONCURRENT_VALUE_CAP_RATIO * 0.5:
            cap_warning.append((r, max_pos_val))
    if cap_warning:
        print("  以下の銘柄は単独で信用枠の 75% を占める可能性あり:")
        for r, mv in cap_warning:
            print(f"    {r['symbol']:8} {r['strategy']:14} mult={r['lot_multiplier']:.2f} "
                  f"max_pos_val={mv:,.0f} JPY")
    else:
        print("  なし (全銘柄 < 信用枠 75%)")

    out = {
        "generated_at": datetime.now(JST).isoformat(),
        "n_active": len(active),
        "rows": rows,
        "grand_ev_mech": grand_ev_mech,
        "grand_ev_d7": grand_ev_d7,
        "real_mech_per_day": real_mech,
        "real_d7_per_day": real_d7,
        "target_per_day": target,
        "achievement_rate_d7": real_d7 / target * 100,
        "uplift_d7_vs_mech": real_d7 - real_mech,
        "compression": COMPRESSION,
        "d2_uplift": D2_UPLIFT,
    }
    Path("data/d7_preflight_v3.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: data/d7_preflight_v3.json")

    if real_d7 >= target:
        print(f"\n✅ D7 で目標達成見込み (+{real_d7-target:.0f} 円/日 上回る)")
    else:
        print(f"\n❗ 目標まで不足: {target-real_d7:+.0f} 円/日")


if __name__ == "__main__":
    main()
