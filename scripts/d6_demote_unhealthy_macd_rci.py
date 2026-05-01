#!/usr/bin/env python3
"""D6c: 60日 5m 実測で構造的負け銘柄の MacdRci を observation_only 化.

D6b PoC 結果より:
  - 8306.T MacdRci: -26,841 円 / 60d (-447 円/日)
  - 9433.T MacdRci: -53,479 円 / 60d (-891 円/日)

両銘柄は universe 既登録の代替戦略 (BBShort / Pullback / MicroScalp) で
カバーされており、MacdRci は paper trading から除外する。

universe_active.json の該当 entry に `observation_only=true` と
`force_paper=false` を設定する。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

DEMOTE = {
    ("8306.T", "MacdRci"): {"reason": "60d 5m: -26,841円 (-447円/日). BBShort + Pullback で代替", "delta": -447},
    ("9433.T", "MacdRci"): {"reason": "60d 5m: -53,479円 (-891円/日). MicroScalp + BBShort で代替", "delta": -891},
    # Phase 2 (2026-05-01 D6d): 60d 健全性チェックで実測負けの追加銘柄
    ("6613.T", "MacdRci"): {"reason": "60d 5m: -56,512円 (-1,487円/日). oos_daily=12,464 と大乖離。直近相場では機能停止", "delta": -1487},
    ("6501.T", "MacdRci"): {"reason": "60d 5m: -17,129円 (-439円/日). MicroScalp + BBShort で代替", "delta": -439},
    ("4911.T", "MacdRci"): {"reason": "60d 5m: -5,847円 (-150円/日). 構造的負け、観察のみ", "delta": -150},
}


def main() -> None:
    p = Path("data/universe_active.json")
    u = json.load(open(p))
    syms = u.get("symbols", [])
    log = []

    for entry in syms:
        key = (entry.get("symbol"), entry.get("strategy"))
        if key not in DEMOTE:
            continue
        info = DEMOTE[key]
        before = {
            "observation_only": entry.get("observation_only"),
            "force_paper": entry.get("force_paper"),
        }
        entry["observation_only"] = True
        entry["force_paper"] = False
        entry["demote_note"] = info["reason"]
        entry["demoted_at"] = datetime.now(JST).isoformat()
        log.append({
            "symbol": key[0],
            "strategy": key[1],
            "before": before,
            "after": {"observation_only": True, "force_paper": False},
            "reason": info["reason"],
            "expected_loss_avoided_per_day": -info["delta"],
        })
        print(f"  demoted {key[0]} {key[1]}: {info['reason']}")

    # ── active_count を再計算 (observation_only=True を除外) ──
    active_count = sum(
        1
        for s in syms
        if not s.get("observation_only", False) or s.get("force_paper", False)
    )
    u["active_count"] = active_count
    u["updated_at"] = datetime.now(JST).isoformat()
    u["d6_demote_log"] = log

    p.write_text(
        json.dumps(u, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    total_avoided = sum(x["expected_loss_avoided_per_day"] for x in log)
    print(f"\n active_count: {active_count}")
    print(f" expected daily loss avoided: +{total_avoided:.0f} 円/日")
    print(f"\nsaved: {p}")


if __name__ == "__main__":
    main()
