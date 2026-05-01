#!/usr/bin/env python3
"""D2 concurrent_value_cap + high_cost_cap の効果を 5/1 timeline で simulate.

5/1 paper の trade と skip events を時系列で再生し、
新 guard を適用した場合の各 entry/skip 判定を再評価する。

各 entry 時点での cumulative_locked と guard 判定を表示し、
新 PnL を試算する。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

# 5/1 paper の actual trades (実 PnL は記録)
TRADES = [
    {"entry": "09:39:51", "exit": "09:43:25", "sym": "4911.T", "side": "short", "qty": 200, "px": 3140.0, "exit_px": 3154.0, "pnl": -2800, "oos_expected": 199},
    {"entry": "09:40:59", "exit": "09:48:48", "sym": "9984.T", "side": "short", "qty": 100, "px": 5356.0, "exit_px": 5362.0, "pnl": -600, "oos_expected": 8937},
    {"entry": "09:43:19", "exit": "09:46:30", "sym": "9433.T", "side": "short", "qty": 200, "px": 2547.5, "exit_px": 2551.5, "pnl": -800, "oos_expected": 1016},
    {"entry": "09:46:29", "exit": "11:04:00", "sym": "8316.T", "side": "long", "qty": 100, "px": 5474.0, "exit_px": 5529.0, "pnl": 5500, "oos_expected": 845},
    {"entry": "10:16:47", "exit": "14:19:30", "sym": "4911.T", "side": "long", "qty": 100, "px": 3138.0, "exit_px": 3141.0, "pnl": 300, "oos_expected": 199},
    {"entry": "11:04:09", "exit": "15:05:05", "sym": "9433.T", "side": "long", "qty": 200, "px": 2534.5, "exit_px": 2538.0, "pnl": 700, "oos_expected": 1016},
    {"entry": "14:20:31", "exit": "14:38:02", "sym": "6723.T", "side": "long", "qty": 100, "px": 3217.0, "exit_px": 3200.0, "pnl": -1700, "oos_expected": 1471},
    {"entry": "14:38:03", "exit": "15:05:05", "sym": "4911.T", "side": "long", "qty": 100, "px": 3144.0, "exit_px": 3146.0, "pnl": 200, "oos_expected": 199},
]

# 余力枯渇で skip した signal (推定 OOS 期待値で機会損失を評価)
MISSED_SIGNALS = [
    # 9984.T MacdRci 10:22-10:30 の 6 件 (long signal)
    {"ts": "10:22", "sym": "9984.T", "side": "long", "px": 5350.0, "qty": 100,
     "oos_expected": 8937, "scenario": "MacdRci long signal 6回連続"},
    # 9468.T MacdRci 10:21-10:22 の 2 件 (long signal)
    {"ts": "10:21", "sym": "9468.T", "side": "long", "px": 1620.0, "qty": 200,
     "oos_expected": 1597, "scenario": "MacdRci long signal 2回連続"},
]

INITIAL_CASH = 990_000  # 99 万円
HIGH_COST_THRESHOLD = 500_000
HIGH_COST_MAX_CONCURRENT = 1


def parse_time(s: str) -> datetime:
    return datetime.strptime(f"2026-05-01 {s if len(s) == 8 else s + ':00'}", "%Y-%m-%d %H:%M:%S")


def evaluate_with_guard(ratio: float) -> dict:
    """新 guard (max_concurrent_value_ratio=ratio) で 5/1 を simulate."""
    buying_power = INITIAL_CASH
    cap_limit = buying_power * ratio
    positions: dict[str, dict] = {}
    cash = INITIAL_CASH

    events = []
    for t in TRADES:
        events.append({"ts": parse_time(t["entry"]), "type": "entry", "trade": t})
        events.append({"ts": parse_time(t["exit"]), "type": "exit", "trade": t})
    for s in MISSED_SIGNALS:
        events.append({"ts": parse_time(s["ts"]), "type": "missed_signal", "signal": s})
    events.sort(key=lambda e: e["ts"])

    log = []
    blocked_trades = []
    blocked_actual_pnl = 0
    accepted_missed_pnl = 0  # missed signal が新 guard で entry できたか
    accepted_trade_pnl = 0
    new_total = 0

    for e in events:
        ts_str = e["ts"].strftime("%H:%M:%S")
        cumulative_locked = sum(p["qty"] * p["px"] for p in positions.values())
        high_cost_n = sum(1 for p in positions.values() if p["qty"] * p["px"] >= HIGH_COST_THRESHOLD)

        if e["type"] == "entry":
            t = e["trade"]
            new_pos_cost = t["qty"] * t["px"]
            # guard 1: cumulative cap
            if cumulative_locked + new_pos_cost > cap_limit:
                blocked_trades.append({
                    "ts": ts_str, "sym": t["sym"], "side": t["side"],
                    "cumulative": cumulative_locked, "new_cost": new_pos_cost,
                    "cap": cap_limit, "actual_pnl": t["pnl"],
                    "reason": "concurrent_value_cap",
                })
                blocked_actual_pnl += t["pnl"]
                log.append({
                    "ts": ts_str, "result": "BLOCK (cap)",
                    "sym": t["sym"], "side": t["side"], "qty": t["qty"], "px": t["px"],
                    "cumulative": cumulative_locked, "new_cost": new_pos_cost,
                    "cap": cap_limit,
                })
                continue
            # guard 2: high cost concurrent
            if new_pos_cost >= HIGH_COST_THRESHOLD and high_cost_n >= HIGH_COST_MAX_CONCURRENT:
                blocked_trades.append({
                    "ts": ts_str, "sym": t["sym"], "side": t["side"],
                    "high_cost_n": high_cost_n, "actual_pnl": t["pnl"],
                    "reason": "high_cost_concurrent_cap",
                })
                blocked_actual_pnl += t["pnl"]
                log.append({
                    "ts": ts_str, "result": "BLOCK (hc)",
                    "sym": t["sym"], "side": t["side"], "qty": t["qty"], "px": t["px"],
                })
                continue
            # entry 成立
            positions[f"{t['sym']}_{t['side']}_{ts_str}"] = {
                "sym": t["sym"], "side": t["side"], "qty": t["qty"], "px": t["px"],
                "trade": t,
            }
            cash -= new_pos_cost
            accepted_trade_pnl += t["pnl"]
            new_total += t["pnl"]
            log.append({
                "ts": ts_str, "result": "ENTRY OK",
                "sym": t["sym"], "side": t["side"], "qty": t["qty"], "px": t["px"],
                "pnl_realized": t["pnl"],
            })
        elif e["type"] == "exit":
            t = e["trade"]
            # find matching position
            key_match = None
            for k, p in list(positions.items()):
                if p["sym"] == t["sym"] and p["side"] == t["side"] and p["px"] == t["px"]:
                    key_match = k
                    break
            if key_match:
                positions.pop(key_match)
                if t["side"] == "long":
                    cash += t["qty"] * t["exit_px"]
                else:
                    cash += t["qty"] * t["px"] + t["pnl"]
                log.append({
                    "ts": ts_str, "result": "EXIT",
                    "sym": t["sym"], "side": t["side"], "pnl": t["pnl"],
                })
        elif e["type"] == "missed_signal":
            s = e["signal"]
            new_pos_cost = s["qty"] * s["px"]
            # 新 guard で entry できるか?
            if cumulative_locked + new_pos_cost > cap_limit:
                log.append({
                    "ts": ts_str, "result": "MISSED-still-blocked",
                    "sym": s["sym"], "scenario": s["scenario"],
                    "cumulative": cumulative_locked, "new_cost": new_pos_cost, "cap": cap_limit,
                })
                continue
            if new_pos_cost >= HIGH_COST_THRESHOLD and high_cost_n >= HIGH_COST_MAX_CONCURRENT:
                log.append({
                    "ts": ts_str, "result": "MISSED-still-blocked-hc",
                    "sym": s["sym"], "scenario": s["scenario"],
                })
                continue
            # 余力ある → 新 guard 後では entry できる!
            # 仮に OOS expected の比率で取れると仮定 (1日分なので /1 day)
            # 5/1 は 1 日。OOS 期待値の 100% を取れる仮定 (上限)
            estimated_pnl = s["oos_expected"]
            log.append({
                "ts": ts_str, "result": "MISSED-now-CAPTURED",
                "sym": s["sym"], "scenario": s["scenario"],
                "cumulative": cumulative_locked, "estimated_pnl": estimated_pnl,
            })
            accepted_missed_pnl += estimated_pnl
            new_total += estimated_pnl
            # entry simulate
            positions[f"{s['sym']}_long_{ts_str}"] = {
                "sym": s["sym"], "side": "long", "qty": s["qty"], "px": s["px"],
            }
            cash -= new_pos_cost

    return {
        "ratio": ratio, "cap_limit": cap_limit,
        "blocked_count": len(blocked_trades),
        "blocked_actual_pnl": blocked_actual_pnl,
        "accepted_trade_pnl": accepted_trade_pnl,
        "missed_captured_pnl": accepted_missed_pnl,
        "new_total": new_total,
        "delta_vs_baseline": new_total - 800,  # baseline 5/1 actual = +800
        "log": log,
        "blocked": blocked_trades,
    }


def main() -> None:
    print(f"baseline 5/1 paper actual = +800 円\n")
    for ratio in [0.85, 1.0, 1.5, 2.0, 3.0]:
        r = evaluate_with_guard(ratio)
        print(f"ratio={ratio} (cap={r['cap_limit']:,.0f}円)")
        print(f"  blocked: {r['blocked_count']} 件 (回避 PnL = {r['blocked_actual_pnl']:+.0f})")
        print(f"  accepted trades PnL: {r['accepted_trade_pnl']:+.0f}")
        print(f"  captured missed signals (OOS-based): {r['missed_captured_pnl']:+.0f}")
        print(f"  new TOTAL: {r['new_total']:+.0f} (delta vs baseline +800: {r['delta_vs_baseline']:+.0f})")
        print()

    # ratio=1.5 の log を保存
    detail = evaluate_with_guard(1.5)
    Path("data/d2_concurrent_cap_simulation.json").write_text(
        json.dumps(detail, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nsaved: data/d2_concurrent_cap_simulation.json (ratio=1.5)")
    print(f"\n=== ratio=1.5 ログサンプル ===")
    for e in detail["log"]:
        result = e.get("result", "")
        sym = e.get("sym", "")
        ts = e.get("ts", "")
        if "BLOCK" in result or "MISSED" in result:
            print(f"  {ts} {result:<30} {sym} {e}")
        else:
            extra = e.get("pnl_realized") or e.get("pnl") or ""
            print(f"  {ts} {result:<30} {sym} {extra}")


if __name__ == "__main__":
    main()
