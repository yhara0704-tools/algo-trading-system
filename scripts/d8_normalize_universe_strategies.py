#!/usr/bin/env python3
"""D8b: universe の戦略名を strategy_factory に整合させる + 無効戦略を除外.

問題:
  - universe に `BBShort` (大文字) と書かれているが、strategy_factory は `BbShort`
  - universe に `ORB`, `Momentum5Min` があるが strategy_factory に未登録 (= 動かない)
  - jp_live_runner の `_resolve_lot_multiplier` は lower() 正規化で吸収していたが、
    他の経路 (例: create_strategy 直接呼出) で fail する可能性

対応:
  1. BBShort → BbShort に rename (factory に合わせる)
  2. ORB / Momentum5Min entry は observation_only=true (force_paper=false) に降格
     (oos_daily=0 なので実害は少ないが universe を綺麗にする)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

STRATEGY_RENAME = {
    "BBShort": "BbShort",  # universe に大文字で混入していた表記揺れを正規化
}
DEMOTE_STRATEGIES = {"ORB", "Momentum5Min"}  # factory 未登録、実 paper 不可


def main() -> None:
    p = Path("data/universe_active.json")
    u = json.load(open(p))
    syms = u.get("symbols", [])
    rename_log = []
    demote_log = []

    for entry in syms:
        strat = entry.get("strategy")
        # rename
        if strat in STRATEGY_RENAME:
            new_strat = STRATEGY_RENAME[strat]
            entry["strategy"] = new_strat
            rename_log.append({"symbol": entry.get("symbol"),
                              "from": strat, "to": new_strat})
        # demote
        if strat in DEMOTE_STRATEGIES:
            if not entry.get("observation_only", False) or entry.get("force_paper", False):
                entry["observation_only"] = True
                entry["force_paper"] = False
                entry["demote_note"] = (
                    f"strategy_factory 未登録 ({strat})。oos_daily=0 のため実 paper trade 不可。"
                )
                entry["demoted_at"] = datetime.now(JST).isoformat()
                demote_log.append({"symbol": entry.get("symbol"), "strategy": strat})

    # active_count 再計算
    active_count = sum(
        1
        for s in syms
        if not s.get("observation_only", False) or s.get("force_paper", False)
    )
    u["active_count"] = active_count
    u["updated_at"] = datetime.now(JST).isoformat()

    print(f"=== D8b: universe 整合 ===\n")
    print(f"rename: {len(rename_log)} entries")
    for r in rename_log:
        print(f"  {r['symbol']}: {r['from']} → {r['to']}")
    print(f"\ndemote (factory 未登録): {len(demote_log)} entries")
    for d in demote_log:
        print(f"  {d['symbol']} {d['strategy']}")
    print(f"\nactive_count: {active_count}")

    p.write_text(json.dumps(u, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: {p}")


if __name__ == "__main__":
    main()
