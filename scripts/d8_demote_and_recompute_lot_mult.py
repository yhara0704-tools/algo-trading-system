#!/usr/bin/env python3
"""D8c: D8a の結果を反映して universe を再構成.

Step 1: UNHEALTHY 銘柄を observation_only=true / force_paper=false に demote
Step 2: 全 active entry の expected_value_per_day を実測値で再校正
        (D6 で MacdRci 実測、D8 で他戦略実測を採用)
Step 3: lot_multiplier を実測ベースで再計算 ([0.5, 3.0] clip)

実測値の優先順:
  - MacdRci → D6 health_check (60d 5m)
  - Breakout/BbShort/Pullback/EnhancedMacdRci → D8 health_check (60d 5m)
  - MicroScalp → D3 (30d 1m) で per-symbol 最適化済み (信頼) → oos_daily 維持
  - その他 (登録済みかつ未検証戦略があれば) → oos_daily 維持
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
    d6_health = json.load(open("data/d6_macd_rci_health_check.json"))
    d8_health = json.load(open("data/d8_all_strategies_health_check.json"))

    # 実測値辞書 (sym, strat) -> pnl_per_day
    actual_pnl = {}
    for r in d6_health["rows"]:
        actual_pnl[(r["symbol"], "MacdRci")] = r["pnl_per_day"]
    for r in d8_health["results"]:
        actual_pnl[(r["symbol"], r["strategy"])] = r.get("pnl_per_day", 0)

    # ── Step 1: UNHEALTHY を demote ──
    unhealthy_keys = set(
        (u["symbol"], u["strategy"]) for u in d8_health["results"]
        if u["status"] == "UNHEALTHY"
    )
    print(f"=== D8c Step 1: UNHEALTHY demote ===\n")
    demote_log = []
    for entry in universe["symbols"]:
        key = (entry["symbol"], entry["strategy"])
        if key not in unhealthy_keys:
            continue
        if entry.get("observation_only", False) and not entry.get("force_paper", False):
            continue  # 既に demote 済み
        actual = actual_pnl.get(key, 0)
        before = {"observation_only": entry.get("observation_only"),
                  "force_paper": entry.get("force_paper")}
        entry["observation_only"] = True
        entry["force_paper"] = False
        entry["d8_demote_reason"] = f"60d 5m: {actual:+.0f} 円/日 (UNHEALTHY)"
        entry["d8_demoted_at"] = datetime.now(JST).isoformat()
        demote_log.append({"symbol": key[0], "strategy": key[1],
                          "before": before, "actual_pnl_per_day": actual})
        print(f"  demote {key[0]} {key[1]}: actual {actual:+.0f} 円/日")

    # ── Step 2: expected_value_per_day を実測値で更新 + lot_multiplier 再計算 ──
    active = [s for s in universe["symbols"]
              if not s.get("observation_only", False) or s.get("force_paper", False)]
    print(f"\n=== D8c Step 2: expected_value 校正 + lot_multiplier 再計算 ===\n")
    print(f"active entries (after demote): {len(active)}\n")

    entries_with_ev = []
    for s in active:
        sym, strat = s["symbol"], s["strategy"]
        oos_daily = float(s.get("oos_daily", 0) or 0)
        key = (sym, strat)
        if key in actual_pnl:
            ev = actual_pnl[key]
            source = "60d_actual"
        else:
            ev = oos_daily
            source = "oos_daily"
        ev_pos = max(ev, 0)
        entries_with_ev.append({
            "ref": s, "symbol": sym, "strategy": strat,
            "expected_value": ev, "ev_pos": ev_pos, "source": source,
        })

    total_ev = sum(e["ev_pos"] for e in entries_with_ev)
    n = len(entries_with_ev)
    mean_share = 1.0 / n if n > 0 else 1.0
    print(f"total expected_value (実測ベース): {total_ev:.0f} 円/日")
    print(f"mean_share (1/{n}): {mean_share*100:.2f}%\n")

    for e in entries_with_ev:
        share = e["ev_pos"] / total_ev if total_ev > 0 else 0
        if e["ev_pos"] <= 0:
            mult = LOT_MULT_MIN
        else:
            mult = share / mean_share
            mult = max(LOT_MULT_MIN, min(LOT_MULT_MAX, mult))
        e["share"] = share
        e["lot_multiplier"] = round(mult, 2)
        # universe 直接更新
        e["ref"]["lot_multiplier"] = e["lot_multiplier"]
        e["ref"]["expected_value_per_day"] = round(e["expected_value"], 0)

    # 表示
    print(f"  {'symbol':10} {'strategy':16} {'ev':>8} {'share':>6} {'mult':>5} {'src':<12}")
    print(f"  " + "-" * 70)
    rows_log = []
    for e in sorted(entries_with_ev, key=lambda x: -x["lot_multiplier"]):
        line = f"  {e['symbol']:10} {e['strategy']:16} {e['expected_value']:>+8.0f} " \
               f"{e['share']*100:>5.1f}% {e['lot_multiplier']:>5.2f} {e['source']:<12}"
        print(line)
        rows_log.append({
            "symbol": e["symbol"], "strategy": e["strategy"],
            "expected_value": round(e["expected_value"], 0),
            "share": round(e["share"]*100, 2),
            "lot_multiplier": e["lot_multiplier"],
            "source": e["source"],
        })

    # ── Step 3: 期待 PnL 効果計算 ──
    grand_real = sum(e["expected_value"] for e in entries_with_ev)
    weighted_pnl = sum(e["expected_value"] * e["lot_multiplier"]
                       for e in entries_with_ev)
    real_compressed = grand_real * 0.4
    weighted_compressed = weighted_pnl * 0.4
    target = 29_700
    d2_uplift = 3_000

    print(f"\n=== 期待 PnL 効果 (実測ベース、圧縮 40%、D2 +{d2_uplift}) ===\n")
    print(f"  機械分散 (mult=1):       {real_compressed+d2_uplift:>+9.0f} 円/日 = {(real_compressed+d2_uplift)/target*100:5.1f}%")
    print(f"  D8 期待値駆動 (mult 適用): {weighted_compressed+d2_uplift:>+9.0f} 円/日 = {(weighted_compressed+d2_uplift)/target*100:5.1f}%")
    print(f"  目標 (3%/日):              {target:>+9.0f} 円/日")
    if (weighted_compressed + d2_uplift) >= target:
        print(f"\n  ✅ D8 で目標達成見込み (+{weighted_compressed+d2_uplift-target:.0f} 円/日 上回る)")
    else:
        print(f"\n  ❗ 目標まで不足 {target-(weighted_compressed+d2_uplift):.0f} 円/日")

    # ── 保存 ──
    universe["active_count"] = len(active)
    universe["updated_at"] = datetime.now(JST).isoformat()
    universe["d8_demote_log"] = demote_log
    universe["d8_lot_multiplier_log"] = rows_log
    Path("data/universe_active.json").write_text(
        json.dumps(universe, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    Path("data/d8_lot_multiplier_v2.json").write_text(
        json.dumps({
            "generated_at": datetime.now(JST).isoformat(),
            "demote_log": demote_log, "rows": rows_log,
            "grand_real_per_day": grand_real,
            "weighted_pnl_per_day": weighted_pnl,
            "compressed_real": real_compressed,
            "compressed_weighted": weighted_compressed,
            "d2_uplift": d2_uplift,
            "final_estimate": weighted_compressed + d2_uplift,
            "target": target,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nactive_count: {len(active)}")
    print(f"saved: data/universe_active.json")
    print(f"saved: data/d8_lot_multiplier_v2.json")


if __name__ == "__main__":
    main()
