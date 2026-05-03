#!/usr/bin/env python3
"""N4: 6954.T (FANUC) MacdRci を universe に Q1 投入 + lot_multiplier 全面再計算.

N4 検証で 6954.T MacdRci が Q1 (60d 実測 +4,375 円/日 / WF +4,267 円/日 / ratio=1.03) と
判明。8411.T (Q2、慎重 mult=0.5) と異なり、フル投入 (mult 自動算出) に値する。

処理:
  1. universe_active.json に 6954.T MacdRci を新規追加
  2. expected_value 配列を再構築:
     - MacdRci で 60d 実測値あり → 60d 実測 (D6 health_check + N4)
     - その他 → universe oos_daily
  3. lot_multiplier を期待値シェアで再計算 (D7 ロジック)
  4. N2 の Q2 cap (MicroScalp/8411 cap=1.0) を再適用
  5. universe_active.json を上書き

入力:
  - data/macd_rci_params.json (6954.T パラメータ)
  - data/d6_macd_rci_health_check.json (既存 MacdRci 60d 実測)
  - data/n4_validate_6954_macdrci.json (6954.T 60d 実測 = Q1 確定)

出力:
  - data/universe_active.json (上書き)
  - data/n4_add_6954_summary.json (再計算ログ)
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

LOT_MULT_MIN = 0.5
LOT_MULT_MAX = 3.0


def load_actual_60d_map() -> dict:
    """sym -> 60d 実測 PnL/日 (MacdRci 系のみ。D6 + N4 6954)."""
    out = {}
    d6 = json.load(open("data/d6_macd_rci_health_check.json"))
    for r in d6["rows"]:
        out[(r["symbol"], "MacdRci")] = r["pnl_per_day"]
    n4 = json.load(open("data/n4_validate_6954_macdrci.json"))
    out[(n4["symbol"], n4["strategy"])] = n4["pnl_per_day_60d"]
    # 他戦略 (D8) も拾う
    try:
        d8 = json.load(open("data/d8_all_strategies_health_check.json"))
        for r in d8.get("results", []):
            out[(r["symbol"], r["strategy"])] = r.get("pnl_per_day", 0)
    except FileNotFoundError:
        pass
    try:
        d8_6752 = json.load(open("data/d8_6752_multi_strategy_test.json"))
        enh = d8_6752["results"].get("EnhancedMacdRci")
        if enh:
            out[("6752.T", "EnhancedMacdRci")] = enh["pnl_per_day"]
    except FileNotFoundError:
        pass
    return out


def add_6954_entry(universe: dict) -> bool:
    """universe に 6954.T MacdRci entry を追加 (既存があれば更新)."""
    src = json.load(open("data/macd_rci_params.json")).get("6954.T", {})
    if not src:
        print("ERROR: 6954.T not in macd_rci_params.json")
        return False

    PARAM_KEYS = {
        "tp_pct", "sl_pct", "rci_min_agree", "macd_signal", "macd_fast", "macd_slow",
        "entry_profile", "exit_profile", "hist_exit_delay_bars", "rci_exit_min_agree",
        "rci_entry_mode", "rci_gc_slope_lookback", "rci_gc_slope_enabled",
        "rci_gc_slope_min", "rci_gc_slope_max",
        "rci_danger_low", "rci_danger_high", "require_macd_above_signal",
        "volume_surge_max_ratio", "disable_lunch_session_entry",
        "rci_danger_zone_enabled",
    }
    params = {k: src[k] for k in PARAM_KEYS if k in src}
    params["interval"] = "5m"

    syms = universe["symbols"]
    existing = next((s for s in syms
                    if s["symbol"] == "6954.T" and s["strategy"] == "MacdRci"), None)
    if existing:
        existing["params"] = params
        existing["oos_daily"] = src.get("oos_daily")
        existing["expected_value_per_day"] = round(src.get("oos_daily", 0), 0)
        existing["observation_only"] = False
        existing["force_paper"] = True
        existing["n4_updated_at"] = datetime.now(JST).isoformat()
        print(f"updated: 6954.T MacdRci (existing entry)")
        return True

    new_entry = {
        "symbol": "6954.T",
        "name": "FANUC MacdRci (Q1 確定投入)",
        "strategy": "MacdRci",
        "score": round(src.get("oos_daily", 0), 0),
        "is_daily": round(src.get("is_daily", 0), 1),
        "oos_daily": round(src.get("oos_daily", 0), 1),
        "is_pf": src.get("is_pf"),
        "is_trades": src.get("is_trades"),
        "oos_trades": src.get("oos_trades"),
        "is_win_rate": src.get("is_win_rate"),
        "oos_pf": src.get("oos_pf"),
        "oos_win_rate": src.get("oos_win_rate"),
        "robust": True,
        "is_oos_pass": True,
        "wf_window_total": src.get("wf_window_total"),
        "wf_window_pass_ratio": src.get("wf_window_pass_ratio"),
        "source": "n4_macd_rci_params_2026-05-03",
        "observation_only": False,
        "force_paper": True,
        "lot_multiplier": 1.0,
        "expected_value_per_day": round(src.get("oos_daily", 0), 0),
        "params": params,
        "added_at": datetime.now(JST).isoformat(),
        "n4_validation": {
            "wf_oos_daily": src.get("oos_daily"),
            "actual_60d_per_day": 4375,
            "ratio": 1.03,
            "verdict": "Q1",
            "note": "60d 実測=+4,375 / WF=+4,267 / ratio=1.03 = Q1 確定。"
                    "8411.T (Q2 慎重) と異なり、期待値シェア配分で本格投入。",
        },
    }
    syms.append(new_entry)
    print(f"added: 6954.T MacdRci")
    print(f"  oos_daily={new_entry['oos_daily']:.0f} oos_pf={new_entry['oos_pf']:.2f} "
          f"oos_wr={new_entry['oos_win_rate']:.1f}% trades={new_entry['oos_trades']}")
    print(f"  60d 実測=+4,375 / ratio=1.03 → Q1 確定")
    return True


def recompute_lot_multipliers(universe: dict, actual_map: dict) -> dict:
    """期待値シェアで lot_multiplier を再計算 + Q2 cap 適用."""
    syms = universe["symbols"]
    active = [s for s in syms
              if not s.get("observation_only", False) or s.get("force_paper", False)]

    # Step 1: 期待値算出
    entries = []
    for s in active:
        sym, strat = s["symbol"], s["strategy"]
        key = (sym, strat)
        if key in actual_map:
            ev = actual_map[key]
            source = "60d_actual"
        else:
            ev = float(s.get("oos_daily", 0) or 0)
            source = "oos_daily"
        ev_pos = max(ev, 0)
        entries.append({
            "ref": s, "symbol": sym, "strategy": strat,
            "expected_value": ev, "ev_pos": ev_pos, "source": source,
            "actual_60d": actual_map.get(key),
            "wf_oos": float(s.get("oos_daily", 0) or 0),
        })

    total_ev = sum(e["ev_pos"] for e in entries)
    n = len(entries)
    mean_share = 1.0 / n if n > 0 else 0

    # Step 2: 基本 mult 算出 (期待値シェア)
    for e in entries:
        share = e["ev_pos"] / total_ev if total_ev > 0 else 0
        if e["ev_pos"] <= 0:
            mult = LOT_MULT_MIN
        else:
            mult = share / mean_share if mean_share > 0 else 1.0
            mult = max(LOT_MULT_MIN, min(LOT_MULT_MAX, mult))
        e["share"] = share
        e["base_mult"] = round(mult, 2)
        e["lot_multiplier"] = round(mult, 2)

    # Step 3: Q2 cap 適用 (WF のみ正、60d 実測なし or 負)
    for e in entries:
        wf = e["wf_oos"]
        a60 = e["actual_60d"]
        # Q2 判定: WF+ かつ 60d 実測なし (MicroScalp 等) or 60d <= 0
        if wf > 0 and (a60 is None or a60 <= 0):
            # MicroScalp 系は 60d 5m で動かない → 慎重に cap=1.0
            # 8411.T も Q2 (caveat) → cap=1.0
            old = e["lot_multiplier"]
            e["lot_multiplier"] = round(min(old, 1.0), 2)
            if old > 1.0:
                e["q2_cap_applied"] = True
                e["q2_note"] = f"Q2 cap: {old} → 1.0 (WF+ but 60d unavailable/negative)"

    # Step 4: universe entry に書き込み
    for e in entries:
        e["ref"]["lot_multiplier"] = e["lot_multiplier"]
        e["ref"]["expected_value_per_day"] = round(e["expected_value"], 0)

    # Step 5: 集計
    grand_real = sum(e["ev_pos"] for e in entries)
    weighted = sum(e["ev_pos"] * e["lot_multiplier"] for e in entries)
    target = 29_700
    real_compressed = grand_real * 0.4 + 3_000
    weighted_compressed = weighted * 0.4 + 3_000

    return {
        "n_active": n, "total_ev": total_ev, "entries": entries,
        "grand_real_per_day": grand_real, "weighted_per_day": weighted,
        "compressed_real": real_compressed,
        "compressed_weighted": weighted_compressed,
        "target": target,
    }


def main() -> None:
    print(f"=== N4: 6954.T MacdRci universe 投入 + lot_multiplier 全面再計算 ===\n")
    universe = json.load(open("data/universe_active.json"))

    if not add_6954_entry(universe):
        return

    print()
    actual_map = load_actual_60d_map()
    summary = recompute_lot_multipliers(universe, actual_map)

    print(f"=== lot_multiplier 再計算結果 ===\n")
    print(f"  active entries: {summary['n_active']}")
    print(f"  total expected_value: {summary['total_ev']:.0f} 円/日\n")
    print(f"  {'symbol':10} {'strategy':16} {'ev':>8} {'share':>6} {'mult':>5} {'src':<12} {'note'}")
    print(f"  {'-' * 95}")
    log_rows = []
    for e in sorted(summary["entries"], key=lambda x: -x["lot_multiplier"]):
        share_pct = e["share"] * 100
        note = "Q2 cap" if e.get("q2_cap_applied") else ""
        if e["symbol"] == "6954.T":
            note = "★ N4 新規"
        print(f"  {e['symbol']:10} {e['strategy']:16} "
              f"{e['expected_value']:>+8.0f} {share_pct:>5.1f}% "
              f"{e['lot_multiplier']:>5.2f} {e['source']:<12} {note}")
        log_rows.append({
            "symbol": e["symbol"], "strategy": e["strategy"],
            "expected_value": round(e["expected_value"], 0),
            "share_pct": round(share_pct, 2),
            "lot_multiplier": e["lot_multiplier"],
            "base_mult": e.get("base_mult"),
            "source": e["source"],
            "q2_cap_applied": e.get("q2_cap_applied", False),
            "actual_60d": e.get("actual_60d"),
            "wf_oos": e.get("wf_oos"),
        })

    print(f"\n=== 期待 PnL (圧縮 40%, D2 +3,000) ===\n")
    print(f"  機械分散 (mult=1):    {summary['compressed_real']:>+9.0f} 円/日 "
          f"({summary['compressed_real']/summary['target']*100:.1f}%)")
    print(f"  N4 期待値 mult 適用:   {summary['compressed_weighted']:>+9.0f} 円/日 "
          f"({summary['compressed_weighted']/summary['target']*100:.1f}%)")

    # active_count 更新
    active = sum(
        1 for s in universe["symbols"]
        if not s.get("observation_only", False) or s.get("force_paper", False)
    )
    universe["active_count"] = active
    universe["updated_at"] = datetime.now(JST).isoformat()
    universe["n4_recomputed_at"] = datetime.now(JST).isoformat()

    Path("data/universe_active.json").write_text(
        json.dumps(universe, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    Path("data/n4_add_6954_summary.json").write_text(
        json.dumps({
            "generated_at": datetime.now(JST).isoformat(),
            "n_active": summary["n_active"],
            "total_ev_per_day": summary["total_ev"],
            "compressed_real": summary["compressed_real"],
            "compressed_weighted": summary["compressed_weighted"],
            "target": summary["target"],
            "rows": log_rows,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"\nactive_count: {active}")
    print(f"saved: data/universe_active.json")
    print(f"saved: data/n4_add_6954_summary.json")


if __name__ == "__main__":
    main()
