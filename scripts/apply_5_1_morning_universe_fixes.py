#!/usr/bin/env python3
"""5/1 朝バックテスト報告に基づく universe_active.json 修正.

修正 1: 6613.T EnhancedMacdRci を除外
  - low_robust_yield_warnings: trials=395, robust_rate=0.0%, avg_oos=-1,847 円/日
  - alt: MacdRci (robust 率 72.1%, avg_oos -1,214 で同様マイナスだが 1 robust 化試行有)
  - → EnhancedMacdRci を削除し MacdRci 単独に

修正 2: 3103.T MacdRci を observation 化 (削除はしない)
  - oos_trades=14 で過適合疑い (IS_pf=1.00 なのに OOS_pf=7.67)
  - paper_low_sample で実質除外済 (oos_trades<20 wf_relaxed)
  - score を大幅減点 + meta フラグ追加で「観察対象」明示
  - 削除しないのは、Breakout 側が機能している可能性とロジックの整合性確認のため

参照:
  - data/backtest_quality_gate_latest.json low_robust_yield_warnings.adopted_low_yield_pairs
  - data/macd_rci_params.json (3103.T 5/1 更新)
  - data/paper_low_sample_excluded_latest.json
"""
from __future__ import annotations
import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_PATH = ROOT / "data/universe_active.json"


def main() -> None:
    universe = json.loads(UNIVERSE_PATH.read_text())
    entries = universe.get("symbols", [])

    new_entries = []
    actions = []

    for e in entries:
        sym = e["symbol"]
        strat = e.get("strategy")

        # 修正 1: 6613.T EnhancedMacdRci を完全削除
        if sym == "6613.T" and strat == "EnhancedMacdRci":
            actions.append(f"REMOVE 6613.T EnhancedMacdRci (low_robust_yield robust_rate=0%)")
            continue

        # 修正 2: 3103.T MacdRci を observation 化
        if sym == "3103.T" and strat == "MacdRci":
            new_e = dict(e)
            new_e["observation_only"] = True
            new_e["observation_reason"] = (
                "oos_trades=14 (paper_low_sample 閾値 20 未満)、IS_pf=1.00 / IS_daily=+27円/日 で "
                "本体未機能。OOS の偶発当たりだけでの Robust 化疑い。"
                "paper_low_sample_excluded で除外済 = paper では既に使われない。"
                "OOS trades が 20+ に増えるまで実弾候補から除外。"
            )
            # score を大幅減点 (旧スコア × 0.1)、ただし IS/OOS 数値はそのまま残す
            old_score = e.get("score", 0)
            new_e["score"] = round(old_score * 0.1, 1)
            new_e["score_demoted_from"] = old_score
            new_e["observed_since"] = datetime.now().isoformat()
            actions.append(
                f"DEMOTE 3103.T MacdRci to observation (score {old_score} → {new_e['score']:.1f})"
            )
            new_entries.append(new_e)
            continue

        new_entries.append(e)

    if not actions:
        print("No changes needed.")
        return

    # バックアップ
    backup = UNIVERSE_PATH.with_suffix(
        f".bak.5_1_morning.{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    shutil.copy(UNIVERSE_PATH, backup)

    universe["symbols"] = new_entries
    universe["active_count"] = len(new_entries)
    universe["last_5_1_morning_fix"] = datetime.now().isoformat()
    universe["5_1_morning_fix_actions"] = actions
    UNIVERSE_PATH.write_text(
        json.dumps(universe, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print(f"=== 5/1 朝 universe 修正 ===")
    for a in actions:
        print(f"  {a}")
    print(f"\n  バックアップ: {backup.relative_to(ROOT)}")
    print(f"  修正後 active_count: {len(new_entries)}")
    print(f"  oos_daily 合計: {sum(e.get('oos_daily', 0) for e in new_entries):+.0f} 円/日")


if __name__ == "__main__":
    main()
