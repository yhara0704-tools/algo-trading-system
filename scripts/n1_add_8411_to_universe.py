#!/usr/bin/env python3
"""N1: 8411.T (MUFG) MacdRci を universe に慎重追加.

daemon (macd_rci_params.json) で 5/2 に新規 Robust 入り:
  oos_daily=+4,747, oos_pf=2.56, oos_win_rate=70.6%, is_oos_pass=true
  ただし IS が IS_pf=1.01 / IS_wr=30% / wf_window_total=1 で再現性疑念あり。
  paper 検証で実信頼性を測るため、force_paper=true + lot_multiplier=0.5 で投入。

universe entry に既存があれば params 更新のみ、なければ新規追加。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))


def main() -> None:
    universe = json.load(open("data/universe_active.json"))
    src_params = json.load(open("data/macd_rci_params.json")).get("8411.T", {})
    if not src_params:
        print("ERROR: 8411.T not in macd_rci_params.json")
        return

    # 戦略パラメータだけ抽出 (oos_daily 等のメタは除外)
    PARAM_KEYS = {
        "tp_pct", "sl_pct", "rci_min_agree", "macd_signal", "macd_fast", "macd_slow",
        "entry_profile", "exit_profile", "hist_exit_delay_bars", "rci_exit_min_agree",
        "rci_entry_mode", "rci_gc_slope_lookback", "rci_gc_slope_enabled",
        "rci_gc_slope_min", "rci_gc_slope_max",
        "rci_danger_low", "require_macd_above_signal", "rci_danger_high",
        "volume_surge_max_ratio", "disable_lunch_session_entry",
        "rci_danger_zone_enabled",
    }
    params = {k: src_params[k] for k in PARAM_KEYS if k in src_params}
    params["interval"] = "5m"

    syms = universe["symbols"]
    existing = next((s for s in syms
                    if s["symbol"] == "8411.T" and s["strategy"] == "MacdRci"), None)
    if existing:
        existing["params"] = params
        existing["oos_daily"] = src_params.get("oos_daily")
        existing["expected_value_per_day"] = round(src_params.get("oos_daily", 0), 0)
        existing["lot_multiplier"] = 0.5
        existing["observation_only"] = False
        existing["force_paper"] = True
        existing["n1_updated_at"] = datetime.now(JST).isoformat()
        print(f"updated: 8411.T MacdRci (existing entry)")
    else:
        new_entry = {
            "symbol": "8411.T",
            "name": "Mizuho FG MacdRci (慎重投入)",
            "strategy": "MacdRci",
            "score": round(src_params.get("oos_daily", 0), 0),
            "is_daily": round(src_params.get("is_daily", 0), 1),
            "oos_daily": round(src_params.get("oos_daily", 0), 1),
            "is_pf": src_params.get("is_pf"),
            "is_trades": src_params.get("is_trades"),
            "oos_trades": src_params.get("oos_trades"),
            "is_win_rate": src_params.get("is_win_rate"),
            "oos_pf": src_params.get("oos_pf"),
            "oos_win_rate": src_params.get("oos_win_rate"),
            "robust": True,
            "is_oos_pass": True,
            "wf_window_total": src_params.get("wf_window_total"),
            "wf_window_pass_ratio": src_params.get("wf_window_pass_ratio"),
            "source": "n1_macd_rci_params_2026-05-02",
            "observation_only": False,
            "force_paper": True,
            "lot_multiplier": 0.5,
            "expected_value_per_day": round(src_params.get("oos_daily", 0), 0),
            "params": params,
            "added_at": datetime.now(JST).isoformat(),
            "caveat": "IS_pf=1.01 / IS_wr=30% / wf_window=1 で再現性疑念。"
                      "paper 検証中は lot_multiplier=0.5 で抑制、再現性確認後に拡大判断。",
        }
        syms.append(new_entry)
        print(f"added: 8411.T MacdRci")
        print(f"  oos_daily={new_entry['oos_daily']:.0f} oos_pf={new_entry['oos_pf']:.2f} "
              f"oos_wr={new_entry['oos_win_rate']:.1f}%")
        print(f"  lot_multiplier=0.5 (caveat: IS 弱、wf_window=1)")

    # active_count
    active = sum(
        1 for s in syms
        if not s.get("observation_only", False) or s.get("force_paper", False)
    )
    universe["active_count"] = active
    universe["updated_at"] = datetime.now(JST).isoformat()
    Path("data/universe_active.json").write_text(
        json.dumps(universe, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nactive_count: {active}")
    print(f"saved: data/universe_active.json")


if __name__ == "__main__":
    main()
