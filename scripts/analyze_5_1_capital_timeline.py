#!/usr/bin:env python3
"""5/1 paper の余力時系列再構成 → 改革案の効果比較.

trades と skip_events から、各時刻の同時保有・cash・拘束資金を計算し、
「どの skip がどの保有によって発生したか」を特定する。

出力:
  - data/capital_timeline_5_1.json: 時系列 cash/position 履歴
  - data/capital_timeline_5_1.txt:  人間向けレポート

改革案検証:
  案 A: 9984.T を universe から削除 → 他銘柄並走で +α 試算
  案 B: 9984.T を 50 株 lot 縮小 (S 株前提) → 高 OOS 全活用試算
  案 C: max_concurrent=3 + 期待値順 queue → 余力分散試算
  案 D: 中額銘柄 (100-300 万) だけで構成 → 4-5 並走確実
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

# 5/1 paper の confirmed trades
TRADES = [
    {"entry": "09:39:51", "exit": "09:43:25", "sym": "4911.T", "side": "short", "qty": 200, "px": 3140.0, "exit_px": 3154.0, "pnl": -2800},
    {"entry": "09:40:59", "exit": "09:48:48", "sym": "9984.T", "side": "short", "qty": 100, "px": 5356.0, "exit_px": 5362.0, "pnl": -600},
    {"entry": "09:43:19", "exit": "09:46:30", "sym": "9433.T", "side": "short", "qty": 200, "px": 2547.5, "exit_px": 2551.5, "pnl": -800},
    {"entry": "09:46:29", "exit": "11:04:00", "sym": "8316.T", "side": "long", "qty": 100, "px": 5474.0, "exit_px": 5529.0, "pnl": 5500},
    {"entry": "10:16:47", "exit": "14:19:30", "sym": "4911.T", "side": "long", "qty": 100, "px": 3138.0, "exit_px": 3141.0, "pnl": 300},
    {"entry": "11:04:09", "exit": "15:05:05", "sym": "9433.T", "side": "long", "qty": 200, "px": 2534.5, "exit_px": 2538.0, "pnl": 700},
    {"entry": "14:20:31", "exit": "14:38:02", "sym": "6723.T", "side": "long", "qty": 100, "px": 3217.0, "exit_px": 3200.0, "pnl": -1700},
    {"entry": "14:38:03", "exit": "15:05:05", "sym": "4911.T", "side": "long", "qty": 100, "px": 3144.0, "exit_px": 3146.0, "pnl": 200},
]

# 5/1 paper の confirmed insufficient_lot skips (時刻 / 銘柄)
INSUFFICIENT_SKIPS = [
    {"ts": "10:21", "sym": "9468.T"}, {"ts": "10:22", "sym": "9468.T"},
    {"ts": "10:22", "sym": "9984.T"},
    {"ts": "10:23", "sym": "9984.T"},
    {"ts": "10:24", "sym": "9984.T"},
    {"ts": "10:25", "sym": "9984.T"},
    {"ts": "10:29", "sym": "9984.T"},
    {"ts": "10:30", "sym": "9984.T"},
    {"ts": "11:19", "sym": "8316.T"},
    {"ts": "11:01", "sym": "9433.T"},
]

INITIAL_CASH = 990_000  # 99 万円信用 (T1)
TIER_MAX_CONCURRENT = 5

# 各 universe 銘柄 OOS expected (取得した universe_active.json から)
OOS_EXPECTED = {
    "9984.T_MacdRci": 8937,
    "9984.T_EnhancedMacdRci": 12168,
    "6613.T_MacdRci": 12464,
    "6752.T_MacdRci": 3072,
    "8306.T_MacdRci": 2208,
    "9107.T_MacdRci": 1954,
    "9468.T_MacdRci": 1597,
    "6723.T_MacdRci": 1471,
    "9433.T_MacdRci": 1016,
    "8316.T_MacdRci": 845,
    "8136.T_Pullback": 2456,
    "3103.T_Breakout": 4856,
    "1605.T_Pullback": 1081,
    "6758.T_Breakout": 602,
    "8058.T_MacdRci": 447,
    "6501.T_MacdRci": 391,
    "4911.T_MacdRci": 199,
    "9432.T_MacdRci": 187,
}


def parse_time(s: str) -> datetime:
    return datetime.strptime(f"2026-05-01 {s if len(s) == 8 else s + ':00'}", "%Y-%m-%d %H:%M:%S")


def reconstruct_timeline() -> list[dict]:
    """trades と skips から時系列イベントリストを作る."""
    events = []
    for t in TRADES:
        events.append({"ts": parse_time(t["entry"]), "type": "entry",
                       "sym": t["sym"], "side": t["side"], "qty": t["qty"],
                       "px": t["px"], "trade": t})
        events.append({"ts": parse_time(t["exit"]), "type": "exit",
                       "sym": t["sym"], "side": t["side"], "qty": t["qty"],
                       "px": t["exit_px"], "pnl": t["pnl"], "trade": t})
    for s in INSUFFICIENT_SKIPS:
        events.append({"ts": parse_time(s["ts"]), "type": "skip",
                       "sym": s["sym"]})
    events.sort(key=lambda e: e["ts"])
    return events


def simulate_cash_timeline() -> list[dict]:
    """各イベントで cash と active position を追跡."""
    events = reconstruct_timeline()
    cash = INITIAL_CASH
    positions: dict[str, dict] = {}  # sym → {qty, px, entry_ts, side}
    log = []
    for e in events:
        if e["type"] == "entry":
            cost = e["qty"] * e["px"]
            cash -= cost
            positions[e["sym"]] = {"qty": e["qty"], "px": e["px"],
                                   "entry_ts": e["ts"], "side": e["side"]}
            log.append({
                "ts": e["ts"].strftime("%H:%M:%S"),
                "event": f"ENTRY {e['sym']} {e['side']} {e['qty']}@{e['px']}",
                "cash_after": cash,
                "active_pos": list(positions.keys()),
                "n_concurrent": len(positions),
                "locked_cash": sum(p["qty"] * p["px"] for p in positions.values()),
            })
        elif e["type"] == "exit":
            if e["sym"] in positions:
                pos = positions.pop(e["sym"])
                # 売却金額 = qty × exit_px (long), short の場合 entry 時 cost を return + pnl
                if pos["side"] == "long":
                    cash += pos["qty"] * e["px"]
                else:  # short: entry 時に既に cost 引かれている、exit 時に entry_value 戻す + pnl
                    cash += pos["qty"] * pos["px"] + (e.get("pnl") or 0)
                log.append({
                    "ts": e["ts"].strftime("%H:%M:%S"),
                    "event": f"EXIT  {e['sym']} {e['side']} pnl={(e.get('pnl') or 0):+.0f}",
                    "cash_after": cash,
                    "active_pos": list(positions.keys()),
                    "n_concurrent": len(positions),
                    "locked_cash": sum(p["qty"] * p["px"] for p in positions.values()),
                })
        elif e["type"] == "skip":
            log.append({
                "ts": e["ts"].strftime("%H:%M:%S"),
                "event": f"SKIP  {e['sym']} (insufficient_lot)",
                "cash_after": cash,
                "active_pos": list(positions.keys()),
                "n_concurrent": len(positions),
                "locked_cash": sum(p["qty"] * p["px"] for p in positions.values()),
            })
    return log


def simulate_alternative_a_remove_9984() -> dict:
    """案 A: 9984.T を universe から削除した場合の試算.
    
    9984.T entry/skip を全削除し、空いた余力で他銘柄が機会を取れたか試算。
    """
    # 9984.T trade を除外
    others = [t for t in TRADES if t["sym"] != "9984.T"]
    skips = [s for s in INSUFFICIENT_SKIPS if s["sym"] != "9984.T"]
    # 試算
    pnl = sum(t["pnl"] for t in others)
    return {
        "name": "案 A: 9984.T 削除",
        "trades": len(others),
        "actual_pnl": pnl,
        "loss_avoided": -(-600),  # 9984 short -600 を回避
        "missed_opportunity": -8937,  # MacdRci OOS 期待値を捨てる
        "net_effect": pnl + 600 - 8937,
    }


def simulate_alternative_b_lot_50() -> dict:
    """案 B: 9984.T を 50 株 lot にした場合.
    
    必要 cash 27.5 万円 → 5/1 では 10:22 時点 cash 13 万 でもまだ届かない。
    11:04 8316 exit 後 cash 56 万 → 27.5 万 の 9984 entry が成立。
    """
    return {
        "name": "案 B: 9984.T 50 株化",
        "trades": "8 件 (現状) + 9984 long 1 件 (約 11:04 入れる)",
        "expected_extra_pnl_50shares": 4500,  # OOS 8937 の 50%
        "feasibility": "50 株単位は信用取引対応外 (S 株は不可)",
    }


def simulate_alternative_c_priority_queue() -> dict:
    """案 C: max_concurrent=3 + 期待値順 queue.
    
    9984.T MacdRci (OOS +8,937) が 9433.T MacdRci (OOS +1,016) より優先される
    べきだが、5/1 の signal タイムラインでは 9433 short が 9984 short より早かった。
    """
    return {
        "name": "案 C: max_concurrent=3 + 期待値順",
        "issue": "signal の発生時刻に依存。5/1 では 9984 signal が後 → priority queue でも保護不可",
        "expected_extra_pnl": 0,
    }


def simulate_alternative_d_low_cost_universe() -> dict:
    """案 D: 中低額銘柄 (株価 1500-3500 円) のみで構成.
    
    9984.T (5,500), 8316.T (5,500), 6613.T (?) を除外し、
    1605.T, 6501.T, 9432.T, 9468.T, 4911.T, 9433.T, 6723.T, 8058.T を中心に。
    """
    LOW_COST = ["1605.T", "6501.T", "9432.T", "9468.T", "4911.T", "9433.T", "6723.T", "8058.T"]
    expected_oos = {
        "1605.T_Pullback": 1081,
        "6501.T_MacdRci": 391,
        "9432.T_MacdRci": 187,  # halt されている
        "9468.T_MacdRci": 1597,
        "4911.T_MacdRci": 199,
        "9433.T_MacdRci": 1016,
        "6723.T_MacdRci": 1471,
        "8058.T_MacdRci": 447,
    }
    total_oos = sum(v for k, v in expected_oos.items() if "9432" not in k)
    return {
        "name": "案 D: 中低額銘柄のみ universe",
        "symbols": LOW_COST,
        "total_oos_expected": total_oos,
        "concurrent_capability": "5 並走可能 (1ポジ平均 25-35万)",
        "missed": "9984/8316/6613 など高 OOS 銘柄を捨てる (合計 +21,964)",
    }


def main() -> None:
    log = simulate_cash_timeline()
    print("=== 5/1 paper cash/position timeline ===\n")
    print(f"{'time':<10}{'event':<55}{'cash':>10}{'locked':>10}{'#':>3}{'  active'}")
    for e in log:
        print(f"{e['ts']:<10}{e['event'][:55]:<55}{e['cash_after']:>10,.0f}{e['locked_cash']:>10,.0f}{e['n_concurrent']:>3}  {','.join(e['active_pos'])}")

    print("\n=== 改革案の試算 ===\n")
    for fn in (simulate_alternative_a_remove_9984,
               simulate_alternative_b_lot_50,
               simulate_alternative_c_priority_queue,
               simulate_alternative_d_low_cost_universe):
        r = fn()
        print(f"\n{r['name']}:")
        for k, v in r.items():
            if k == "name":
                continue
            print(f"  {k}: {v}")

    Path("data/capital_timeline_5_1.json").write_text(
        json.dumps({"timeline": [{**e, "ts": e["ts"]} for e in log]},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8"
    )
    print("\nsaved: data/capital_timeline_5_1.json")


if __name__ == "__main__":
    main()
