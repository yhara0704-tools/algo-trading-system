#!/usr/bin/env python3
"""バックテスト報告のチェックポイントを現在の DB 先端に更新する.

報告完了後に実行すると、次回は `get_method_pdca_aggregate_since` /
`aggregate_macd_rci_slope_since` で差分のみ集計できる。

加えて、`macd_rci_params.json` の robust=True 銘柄集合をスナップショットとして
保存・比較し、**Robust の入替差分（fall-out / new-in）** を dry-run で可視化する。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.storage.backtest_report_checkpoint import (  # noqa: E402
    CHECKPOINT_PATH,
    build_robust_snapshot,
    diff_robust_snapshots,
    load_checkpoint,
    save_checkpoint,
)
from backend.storage.db import (  # noqa: E402
    aggregate_macd_rci_slope_since,
    get_db,
    get_experiments_table_bounds,
    get_method_pdca_aggregate_since,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="報告チェックポイントを DB 先端に更新")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="保存せず現在値と差分サマリーのみ表示",
    )
    ap.add_argument("--note", default="", help="任意メモ（JSON に保存）")
    ap.add_argument(
        "--sync-from-vps",
        action="store_true",
        help="実行の最初に scripts/sync_canonical_from_vps.py で VPS 正本を取得（ローカル報告用）",
    )
    ap.add_argument(
        "--skip-slope",
        action="store_true",
        help=(
            "MacdRci OOS 傾き集計をスキップ（pandas 等が無い環境向け）。"
            "未指定でも import 失敗時は自動でスキップする。"
        ),
    )
    args = ap.parse_args()

    if args.sync_from_vps:
        sync_py = ROOT / "scripts" / "sync_canonical_from_vps.py"
        r = subprocess.run([sys.executable, str(sync_py)], cwd=str(ROOT))
        if r.returncode != 0:
            print(
                "\n警告: VPS 同期に失敗しました。以降の集計は同期前のローカル data/ 基準です。",
                file=sys.stderr,
            )

    get_db()

    bounds = get_experiments_table_bounds()
    prev = load_checkpoint()
    last_id = prev.get("last_experiment_id")
    if last_id is None:
        last_id = 0
    else:
        last_id = int(last_id)

    delta = get_method_pdca_aggregate_since(last_id)
    slope_agg: dict | None
    slope_skip_reason = ""
    if args.skip_slope:
        slope_agg = None
        slope_skip_reason = "--skip-slope 指定"
    else:
        try:
            slope_agg = aggregate_macd_rci_slope_since(last_id)
        except ModuleNotFoundError as e:
            slope_agg = None
            slope_skip_reason = f"依存モジュール未導入のためスキップ: {e.name}"
        except Exception as e:
            slope_agg = None
            slope_skip_reason = f"集計失敗のためスキップ: {type(e).__name__}: {e}"
    curr_snapshot = build_robust_snapshot()
    prev_snapshot = prev.get("robust_snapshot")
    robust_diff = diff_robust_snapshots(prev_snapshot, curr_snapshot)
    print("=== experiments テーブル先端 ===")
    print(json.dumps(bounds, ensure_ascii=False, indent=2))
    print("\n=== 前回チェックポイント以降の method_pdca（参考） ===")
    print(json.dumps(delta, ensure_ascii=False, indent=2))
    print("\n=== 前回チェックポイント以降の MacdRci OOS 傾き集計（rci_slope_summary_json） ===")
    if slope_agg is None:
        print(json.dumps({"skipped": True, "reason": slope_skip_reason}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(slope_agg, ensure_ascii=False, indent=2))
    print("\n=== Robust 集合 入替差分（macd_rci_params.json） ===")
    if prev_snapshot is None:
        print(
            "前回スナップショット未保存のため入替差分は計算不可（本回の保存で初期化）。\n"
            f"現在の Robust 銘柄数: {robust_diff['curr_count']}"
        )
        print(json.dumps(
            {"curr_symbols": curr_snapshot.get("symbols", [])},
            ensure_ascii=False,
            indent=2,
        ))
    else:
        summary = {
            "prev_count": robust_diff["prev_count"],
            "curr_count": robust_diff["curr_count"],
            "new_in_count": len(robust_diff["new_in"]),
            "fall_out_count": len(robust_diff["fall_out"]),
            "intact_count": robust_diff["intact_count"],
            "new_in": robust_diff["new_in"],
            "fall_out": robust_diff["fall_out"],
            "intact_top_oos_changes": robust_diff["intact_top_oos_changes"],
            "prev_captured_at": robust_diff["prev_captured_at"],
            "curr_captured_at": robust_diff["curr_captured_at"],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nチェックポイントファイル: {CHECKPOINT_PATH}")

    if args.dry_run:
        print("\n(dry-run: 保存しません)")
        return

    save_checkpoint(
        last_generation=bounds.get("max_generation"),
        last_experiment_id=bounds.get("max_experiment_id"),
        note=args.note.strip(),
        robust_snapshot=curr_snapshot,
    )
    print("\n保存しました。")


if __name__ == "__main__":
    main()
