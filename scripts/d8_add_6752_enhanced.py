#!/usr/bin/env python3
"""D8e: 6752.T EnhancedMacdRci を universe に追加.

D8d 検証で +2,034 円/日 (60d 5m, WR 40%, PF 1.38) を確認済み。
6752.T MacdRci (+8,348 円/日) と並走させ、6752.T 単独で +10,382 円/日 期待。

追加後に lot_multiplier 全体再計算 (D8c と同手法)。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

LOT_MULT_MIN = 0.5
LOT_MULT_MAX = 3.0


def main() -> None:
    universe = json.load(open("data/universe_active.json"))
    d6_health = json.load(open("data/d6_macd_rci_health_check.json"))
    d8_health = json.load(open("data/d8_all_strategies_health_check.json"))
    d8_6752 = json.load(open("data/d8_6752_multi_strategy_test.json"))

    syms = universe["symbols"]

    # ── 重複チェック + 追加 ──
    has_6752_enh = any(
        s["symbol"] == "6752.T" and s["strategy"] == "EnhancedMacdRci" for s in syms
    )
    if has_6752_enh:
        print("6752.T EnhancedMacdRci は既に universe に存在します")
    else:
        new_entry = {
            "symbol": "6752.T",
            "name": "Panasonic EnhancedMacdRci",
            "strategy": "EnhancedMacdRci",
            "score": 2034,
            "is_daily": 0.0,
            "oos_daily": 2034,
            "is_pf": 1.38,
            "is_trades": 222,
            "robust": True,
            "is_oos_pass": True,
            "calmar": 0.0,
            "source": "d8_6752_multi_strategy_test",
            "observation_only": False,
            "force_paper": True,
            "params": {"interval": "5m"},
            "added_at": datetime.now(JST).isoformat(),
        }
        syms.append(new_entry)
        print(f"added: 6752.T EnhancedMacdRci (oos_daily=2,034)")

    # ── 実測値辞書再構築 ──
    actual_pnl = {}
    for r in d6_health["rows"]:
        actual_pnl[(r["symbol"], "MacdRci")] = r["pnl_per_day"]
    for r in d8_health["results"]:
        actual_pnl[(r["symbol"], r["strategy"])] = r.get("pnl_per_day", 0)
    # 6752 EnhancedMacdRci の実測値
    enh_6752 = d8_6752["results"]["EnhancedMacdRci"]
    actual_pnl[("6752.T", "EnhancedMacdRci")] = enh_6752["pnl_per_day"]

    active = [s for s in syms
              if not s.get("observation_only", False) or s.get("force_paper", False)]
    print(f"\nactive entries: {len(active)}\n")

    # ── lot_multiplier 再計算 ──
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

    rows_log = []
    for e in entries_with_ev:
        share = e["ev_pos"] / total_ev if total_ev > 0 else 0
        if e["ev_pos"] <= 0:
            mult = LOT_MULT_MIN
        else:
            mult = share / mean_share
            mult = max(LOT_MULT_MIN, min(LOT_MULT_MAX, mult))
        e["share"] = share
        e["lot_multiplier"] = round(mult, 2)
        e["ref"]["lot_multiplier"] = e["lot_multiplier"]
        e["ref"]["expected_value_per_day"] = round(e["expected_value"], 0)
        rows_log.append({
            "symbol": e["symbol"], "strategy": e["strategy"],
            "expected_value": round(e["expected_value"], 0),
            "share": round(e["share"]*100, 2),
            "lot_multiplier": e["lot_multiplier"],
            "source": e["source"],
        })

    # 表示
    print(f"  {'symbol':10} {'strategy':16} {'ev':>8} {'share':>6} {'mult':>5}")
    for r in sorted(rows_log, key=lambda x: -x["lot_multiplier"]):
        print(f"  {r['symbol']:10} {r['strategy']:16} {r['expected_value']:>+8.0f} "
              f"{r['share']:>5.1f}% {r['lot_multiplier']:>5.2f}")

    # ── 期待 PnL ──
    grand_real = sum(e["expected_value"] for e in entries_with_ev)
    weighted_pnl = sum(e["expected_value"] * e["lot_multiplier"]
                       for e in entries_with_ev)
    target = 29_700
    d2_uplift = 3_000
    real_compressed = grand_real * 0.4 + d2_uplift
    weighted_compressed = weighted_pnl * 0.4 + d2_uplift

    print(f"\n=== 期待 PnL (実測ベース、圧縮 40%、D2 +{d2_uplift}) ===\n")
    print(f"  機械分散:                   {real_compressed:>+9.0f} 円/日 = {real_compressed/target*100:5.1f}%")
    print(f"  D8 期待値駆動 (6752 拡張後):  {weighted_compressed:>+9.0f} 円/日 = {weighted_compressed/target*100:5.1f}%")
    if weighted_compressed >= target:
        print(f"\n  ✅ 目標達成 (+{weighted_compressed-target:.0f} 円/日 上回る)")
    else:
        print(f"\n  ❗ 目標まで不足 {target-weighted_compressed:.0f} 円/日")

    # ── 6752.T 集中効果 ──
    sym_ev = {}
    for e in entries_with_ev:
        sym_ev[e["symbol"]] = sym_ev.get(e["symbol"], 0) + e["expected_value"]
    top_syms = sorted(sym_ev.items(), key=lambda x: -x[1])[:5]
    print(f"\n=== 銘柄別 期待値合計 (top 5) ===\n")
    for sym, ev in top_syms:
        print(f"  {sym:10} {ev:>+8.0f} 円/日")

    universe["active_count"] = len(active)
    universe["updated_at"] = datetime.now(JST).isoformat()
    Path("data/universe_active.json").write_text(
        json.dumps(universe, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: data/universe_active.json (active_count={len(active)})")


if __name__ == "__main__":
    main()
