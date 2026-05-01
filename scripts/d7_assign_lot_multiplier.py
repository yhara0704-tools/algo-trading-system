#!/usr/bin/env python3
"""D7a: 期待値駆動 lot_multiplier 自動割当.

universe_active.json の各 entry に「期待値シェア」 ベースの lot_multiplier を
書き込む。jp_live_runner はこれを position_value 計算時に乗じることで、
期待値の高い銘柄に余力を集中させる (機械的 1/N 均等分散から脱却)。

設計:
  - expected_value = MacdRci は 60d 実測 PnL/日 (D6e)、その他は oos_daily
  - share = expected_value_i / sum(expected_value_active)
  - active 27 entries の平均 share = 1/27 ≈ 3.7%
  - lot_multiplier = share / mean_share、ただし [0.5, 3.0] で clip
    (1 銘柄独占を防ぐ + 弱小でも完全 0 にしない)

例:
  9984.T EnhancedMacdRci: expected=12,168 → share=25.2% → mult ≈ 3.0 (clipped)
  6752.T MacdRci:         expected=+8,348 → share=17.3% → mult ≈ 3.0 (clipped)
  3103.T Breakout:        expected=+4,856 → share=10.1% → mult ≈ 2.7
  2413.T MacdRci:         expected=  +500 → share= 1.0% → mult ≈ 0.5 (clipped)
  Momentum5Min/ORB (oos=0): mult = 0.5 (最低、観察用)

出力:
  data/universe_active.json (lot_multiplier フィールド追加)
  data/d7_lot_multiplier_assignment.json (割当ログ)
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

LOT_MULT_MIN = 0.5
LOT_MULT_MAX = 3.0


def main() -> None:
    universe = json.load(open("data/universe_active.json"))
    health = json.load(open("data/d6_macd_rci_health_check.json"))
    macd_actuals = {r["symbol"]: r["pnl_per_day"] for r in health["rows"]}

    syms = universe["symbols"]
    active = [s for s in syms if not s.get("observation_only", False) or s.get("force_paper", False)]

    # Step 1: 各 active entry の期待値 (実測優先) を算出
    entries_with_ev = []
    for s in active:
        sym, strat = s["symbol"], s["strategy"]
        oos_daily = float(s.get("oos_daily", 0) or 0)
        if strat == "MacdRci" and sym in macd_actuals:
            ev = macd_actuals[sym]
            source = "60d_actual"
        else:
            ev = oos_daily
            source = "oos_daily"
        # 負の期待値は 0 にして share 計算から除外 (ただし clip で min mult は当たる)
        ev_pos = max(ev, 0)
        entries_with_ev.append({
            "ref": s, "symbol": sym, "strategy": strat,
            "expected_value": ev, "ev_pos": ev_pos, "source": source,
        })

    total_ev = sum(e["ev_pos"] for e in entries_with_ev)
    if total_ev <= 0:
        print("ERROR: total expected value <= 0, abort")
        return
    n = len(entries_with_ev)
    mean_share = 1.0 / n  # 均等分散 baseline

    print(f"=== D7a: lot_multiplier 自動割当 ===\n")
    print(f"active entries: {n}")
    print(f"total expected_value: {total_ev:.0f} 円/日")
    print(f"mean_share (1/{n}): {mean_share*100:.2f}%")
    print(f"lot_multiplier 範囲: [{LOT_MULT_MIN}, {LOT_MULT_MAX}]\n")

    # Step 2: lot_multiplier 算出
    log_rows = []
    for e in entries_with_ev:
        share = e["ev_pos"] / total_ev if total_ev > 0 else 0
        # 期待値 0 以下は最低倍率
        if e["ev_pos"] <= 0:
            mult = LOT_MULT_MIN
        else:
            mult = share / mean_share
            mult = max(LOT_MULT_MIN, min(LOT_MULT_MAX, mult))
        e["share"] = share
        e["lot_multiplier"] = round(mult, 2)
        log_rows.append({
            "symbol": e["symbol"],
            "strategy": e["strategy"],
            "expected_value": round(e["expected_value"], 0),
            "share": round(share * 100, 2),
            "lot_multiplier": e["lot_multiplier"],
            "source": e["source"],
        })

    # Step 3: universe entry に書き込み
    for e in entries_with_ev:
        e["ref"]["lot_multiplier"] = e["lot_multiplier"]
        e["ref"]["expected_value_per_day"] = round(e["expected_value"], 0)

    # Step 4: 表示
    print(f"  {'symbol':10} {'strategy':16} {'ev':>8} {'share':>6} {'mult':>5} {'src':<12}")
    print(f"  " + "-" * 70)
    for r in sorted(log_rows, key=lambda x: -x["lot_multiplier"]):
        print(f"  {r['symbol']:10} {r['strategy']:16} "
              f"{r['expected_value']:>+8.0f} {r['share']:>5.1f}% "
              f"{r['lot_multiplier']:>5.2f} {r['source']:<12}")

    # Step 5: 効果分析
    print(f"\n=== ロット倍率分布 ===\n")
    by_mult = defaultdict(int)
    for r in log_rows:
        bucket = "≥2.5" if r["lot_multiplier"] >= 2.5 else \
                 "1.5-2.5" if r["lot_multiplier"] >= 1.5 else \
                 "1.0-1.5" if r["lot_multiplier"] >= 1.0 else \
                 "0.5-1.0" if r["lot_multiplier"] >= 0.5 else "<0.5"
        by_mult[bucket] += 1
    for b in ["≥2.5", "1.5-2.5", "1.0-1.5", "0.5-1.0", "<0.5"]:
        print(f"  {b:8}: {by_mult[b]:>3} entries")

    # Step 6: 期待 PnL 効果計算 (機械分散 vs 期待値駆動)
    n_active = len(entries_with_ev)
    base_pos_value = 990_000 / 3  # MAX_POSITIONS=3 想定で 1 銘柄 33%
    
    # 機械分散版 PnL (現状)
    mech_pnl = sum(e["expected_value"] for e in entries_with_ev) * 0.4  # 圧縮 40%
    # 期待値駆動版 PnL (高 mult 銘柄が稼働しやすい想定)
    # 単純化: mult 1.0 から離れる差分 × expected_value で uplift を見積もる
    weighted_pnl = sum(e["expected_value"] * e["lot_multiplier"] for e in entries_with_ev) * 0.4 / 1.0
    print(f"\n=== 期待 PnL 効果 (圧縮 40% 後) ===\n")
    print(f"  機械分散 (mult=1 全員):  {mech_pnl:+9.0f} 円/日")
    print(f"  期待値駆動 (mult 適用):  {weighted_pnl:+9.0f} 円/日")
    print(f"  差分 (uplift):           {weighted_pnl - mech_pnl:+9.0f} 円/日")

    # Step 7: 保存
    universe["updated_at"] = datetime.now(JST).isoformat()
    universe["d7_lot_multiplier_applied"] = True
    universe["d7_lot_multiplier_log"] = log_rows
    Path("data/universe_active.json").write_text(
        json.dumps(universe, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    Path("data/d7_lot_multiplier_assignment.json").write_text(
        json.dumps({
            "generated_at": datetime.now(JST).isoformat(),
            "lot_mult_min": LOT_MULT_MIN, "lot_mult_max": LOT_MULT_MAX,
            "n_active": n_active, "total_ev": total_ev, "mean_share": mean_share,
            "rows": log_rows,
            "mech_pnl_per_day": mech_pnl,
            "weighted_pnl_per_day": weighted_pnl,
            "uplift_per_day": weighted_pnl - mech_pnl,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: data/universe_active.json")
    print(f"saved: data/d7_lot_multiplier_assignment.json")


if __name__ == "__main__":
    main()
