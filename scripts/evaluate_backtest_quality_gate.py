#!/usr/bin/env python3
"""バックテスト品質ゲートを評価してJSON出力する。

判定方針（Phase F3 以降）:

現状の `experiments` は同一 (strategy, symbol, params_json) の反復評価を多数含む。
単純平均（`AVG(oos_daily_pnl)`）で総合判定すると外れ値と反復集中の両方に引きずられる。

観測からの補足（2026-04-23 検証）:
    - 過去30日で行数 94,674 / 重複排除後の一意設定数 70,305（重複率 ~25%）。
    - 反復トップは `MacdRci × 6613.T` のいくつかの設定で 700+ 回ずつ。
    - 単純平均 `-322` に対し、最新スナップ単位のデデュープでは `-1,467`、
      per-config 平均の外側平均では `-1,431` と悪化する。
    - ポジティブ側の設定が反復で重く評価されているため、デデュープは
      「観測的 OOS 平均負」の解ではない。反復集中の可視化と併用した
      per-config 重み付けを観測メトリクスとして併記する方針に切り替えた。

合否判定は以下の堅牢統計を主軸に据える:

    1. `median_oos_daily_pnl` （全体の中央値。単純平均は外れ値に弱い）
    2. `trimmed_mean_oos_daily_pnl` （5%/95% トリム平均）
    3. `robust_avg_oos_daily_pnl` （`robust=1` の実験のみの平均）

観測用メトリクス（合否には使わない）:
    - `avg_oos_daily_pnl` （従来の単純平均）
    - `avg_oos_daily_pnl_by_config` （(strategy, symbol, params_json) ごとに平均を取った外側平均 = 各設定を等重み）
    - `robust_avg_oos_daily_pnl_by_config` （同上を Robust 設定のみで）
    - `top_repeat_clusters` （反復が多い (strategy, symbol) 上位5件）

これにより「デーモンがどの設定を過剰に反復しているか」「等重み観測でも
実力はプラスかマイナスか」を並列に監視できる。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "algo_trading.db"
OUT_PATH = ROOT / "data" / "backtest_quality_gate_latest.json"
NIGHTLY_WF_PATH = ROOT / "data" / "nightly_walkforward_latest.json"
UNIVERSE_ACTIVE_PATH = ROOT / "data" / "universe_active.json"
JST = timezone(timedelta(hours=9))


def _now_iso() -> str:
    return datetime.now(JST).isoformat()


def _trimmed_mean(values: list[float], trim_pct: float) -> float:
    """両端 `trim_pct` ずつ捨ててから平均を取る（外れ値頑健）。"""
    if not values:
        return 0.0
    if trim_pct <= 0:
        return sum(values) / len(values)
    s = sorted(values)
    n = len(s)
    k = int(n * trim_pct)
    if k * 2 >= n:
        return s[n // 2]
    trimmed = s[k : n - k]
    return sum(trimmed) / len(trimmed) if trimmed else 0.0


def _fetch_metrics(days: int, trim_pct: float = 0.05) -> dict:
    since = (datetime.now(JST) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(experiments)").fetchall()}
        std_col = "COALESCE(oos_pnl_std, 0)" if "oos_pnl_std" in cols else "0"
        cost_col = "COALESCE(cost_drag_pct, 0)" if "cost_drag_pct" in cols else "0"
        worst_col = "COALESCE(oos_pnl_worst, 0)" if "oos_pnl_worst" in cols else "0"
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS trials,
                COALESCE(SUM(CASE WHEN robust=1 THEN 1 ELSE 0 END), 0) AS robust_count,
                AVG(oos_daily_pnl) AS avg_oos_daily_pnl,
                AVG(oos_pf) AS avg_oos_pf,
                AVG(oos_win_rate) AS avg_oos_win_rate,
                AVG(max_dd_pct) AS avg_max_dd_pct,
                AVG(COALESCE(oos_trades, 0)) AS avg_oos_trades,
                AVG({std_col}) AS avg_oos_pnl_std,
                AVG({cost_col}) AS avg_cost_drag_pct,
                MIN({worst_col}) AS worst_split_oos,
                AVG(CASE WHEN robust=1 THEN oos_daily_pnl END) AS robust_avg_oos_daily_pnl
            FROM experiments
            WHERE date(created_at) >= ?
            """,
            (since,),
        ).fetchone()

        # 中央値・トリム平均は Python 側で計算（外れ値による誤判定を避ける）。
        vals = conn.execute(
            """
            SELECT oos_daily_pnl FROM experiments
            WHERE date(created_at) >= ? AND oos_daily_pnl IS NOT NULL
            """,
            (since,),
        ).fetchall()

        # per-config 集計（等重み観測）。反復回数に関係なく (strategy, symbol, params_json) を1件として扱う。
        per_config = conn.execute(
            """
            SELECT strategy_name, symbol, params_json,
                   AVG(oos_daily_pnl) AS cfg_mean_pnl,
                   MAX(robust) AS cfg_robust_any,
                   COUNT(*) AS cfg_cnt
            FROM experiments
            WHERE date(created_at) >= ? AND oos_daily_pnl IS NOT NULL
            GROUP BY strategy_name, symbol, params_json
            """,
            (since,),
        ).fetchall()

        # (strategy, symbol) ごとの反復集中（参考診断）。
        cluster_rows = conn.execute(
            """
            SELECT strategy_name, symbol, COUNT(*) AS total_rows,
                   COUNT(DISTINCT params_json) AS distinct_cfgs,
                   AVG(oos_daily_pnl) AS avg_pnl
            FROM experiments
            WHERE date(created_at) >= ? AND oos_daily_pnl IS NOT NULL
            GROUP BY strategy_name, symbol
            ORDER BY total_rows DESC
            LIMIT 5
            """,
            (since,),
        ).fetchall()
    finally:
        conn.close()

    metrics = dict(row) if row else {}
    floats = [float(v["oos_daily_pnl"]) for v in vals]
    metrics["sample_size_oos"] = len(floats)
    metrics["median_oos_daily_pnl"] = float(median(floats)) if floats else 0.0
    metrics["trimmed_mean_oos_daily_pnl"] = _trimmed_mean(floats, trim_pct)
    metrics["trim_pct"] = trim_pct

    # per-config 観測メトリクス（反復に依らず設定あたりを等重みにした外側平均）。
    cfg_means = [float(r["cfg_mean_pnl"]) for r in per_config if r["cfg_mean_pnl"] is not None]
    cfg_robust_means = [
        float(r["cfg_mean_pnl"])
        for r in per_config
        if r["cfg_mean_pnl"] is not None and int(r["cfg_robust_any"] or 0) == 1
    ]
    metrics["distinct_configs"] = len(per_config)
    metrics["avg_oos_daily_pnl_by_config"] = (sum(cfg_means) / len(cfg_means)) if cfg_means else 0.0
    metrics["robust_avg_oos_daily_pnl_by_config"] = (
        sum(cfg_robust_means) / len(cfg_robust_means) if cfg_robust_means else 0.0
    )
    # 反復集中（上位5）。params_json まで辿らず (strategy, symbol) 単位で可視化。
    metrics["top_repeat_clusters"] = [
        {
            "strategy_name": r["strategy_name"],
            "symbol": r["symbol"],
            "total_rows": int(r["total_rows"] or 0),
            "distinct_configs": int(r["distinct_cfgs"] or 0),
            "avg_oos_daily_pnl": float(r["avg_pnl"] or 0.0),
        }
        for r in cluster_rows
    ]
    return metrics


def _prune_paper_universe(min_wins: int = 0) -> dict:
    """Phase F2: nightly_walkforward_latest の demote_candidates を universe_active から除外する。

    既存 universe_active.json の `symbols` のうち、(symbol, strategy) が demote_candidates に
    一致するものを除去したファイルを書き戻す。削除件数などの結果を返す。
    """
    if not NIGHTLY_WF_PATH.exists():
        return {"status": "skipped", "reason": "nightly_walkforward_latest not found"}
    if not UNIVERSE_ACTIVE_PATH.exists():
        return {"status": "skipped", "reason": "universe_active.json not found"}

    wf = json.loads(NIGHTLY_WF_PATH.read_text(encoding="utf-8"))
    active = json.loads(UNIVERSE_ACTIVE_PATH.read_text(encoding="utf-8"))

    demote: set[tuple[str, str]] = {
        (d.get("symbol", ""), d.get("strategy", ""))
        for d in (wf.get("demote_candidates") or [])
        if d.get("symbol") and d.get("strategy")
    }
    if not demote:
        return {"status": "noop", "removed": 0, "kept": len(active.get("symbols", []))}

    before = list(active.get("symbols", []) or [])
    after = [r for r in before if (r.get("symbol"), r.get("strategy")) not in demote]
    removed = [
        {"symbol": r.get("symbol"), "strategy": r.get("strategy")}
        for r in before
        if (r.get("symbol"), r.get("strategy")) in demote
    ]
    # 保護: 除外後が 0 件になるなら書き戻さない（過剰剪定を防ぐ）
    if not after and before:
        return {"status": "protected", "reason": "all_pruned", "removed": 0, "kept": len(before)}

    active["symbols"] = after
    active["last_quality_prune"] = {
        "computed_at": datetime.now(JST).isoformat(),
        "removed": removed,
    }
    UNIVERSE_ACTIVE_PATH.write_text(json.dumps(active, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "pruned", "removed": len(removed), "kept": len(after), "items": removed}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-trials", type=int, default=30)
    ap.add_argument("--min-robust-rate", type=float, default=0.12)
    # 従来の単純平均閾値（非堅牢試行の外れ値に引きずられるので観測用に残す）
    ap.add_argument("--min-oos-daily", type=float, default=0.0)
    # 堅牢判定の主軸（中央値・トリム平均・Robust 集合の平均）
    ap.add_argument("--min-median-oos-daily", type=float, default=0.0)
    ap.add_argument("--min-trimmed-mean-oos-daily", type=float, default=0.0)
    ap.add_argument("--min-robust-avg-oos-daily", type=float, default=100.0)
    ap.add_argument("--trim-pct", type=float, default=0.05)
    ap.add_argument("--max-avg-dd", type=float, default=15.0)
    ap.add_argument("--max-avg-cost-drag", type=float, default=35.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--prune-universe",
        action="store_true",
        help="Phase F2: nightly_walkforward_latest.json の demote_candidates を paper universe から除外する",
    )
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args()

    m = _fetch_metrics(days=args.days, trim_pct=args.trim_pct)
    trials = int(m.get("trials") or 0)
    robust_count = int(m.get("robust_count") or 0)
    robust_rate = float(robust_count / trials) if trials > 0 else 0.0

    avg_oos = float(m.get("avg_oos_daily_pnl") or 0.0)
    avg_oos_by_cfg = float(m.get("avg_oos_daily_pnl_by_config") or 0.0)
    median_oos = float(m.get("median_oos_daily_pnl") or 0.0)
    trimmed_mean_oos = float(m.get("trimmed_mean_oos_daily_pnl") or 0.0)
    robust_avg_oos = float(m.get("robust_avg_oos_daily_pnl") or 0.0)
    avg_dd = float(m.get("avg_max_dd_pct") or 0.0)
    cost_drag = float(abs(m.get("avg_cost_drag_pct") or 0.0))

    # 合否を決める堅牢チェック（外れ値に強い3本）
    core_checks = [
        {"name": "min_trials", "passed": trials >= args.min_trials, "actual": trials, "threshold": args.min_trials},
        {"name": "min_robust_rate", "passed": robust_rate >= args.min_robust_rate, "actual": robust_rate, "threshold": args.min_robust_rate},
        {"name": "min_median_oos_daily_pnl", "passed": median_oos >= args.min_median_oos_daily, "actual": median_oos, "threshold": args.min_median_oos_daily},
        {"name": "min_trimmed_mean_oos_daily_pnl", "passed": trimmed_mean_oos >= args.min_trimmed_mean_oos_daily, "actual": trimmed_mean_oos, "threshold": args.min_trimmed_mean_oos_daily},
        {"name": "min_robust_avg_oos_daily_pnl", "passed": robust_avg_oos >= args.min_robust_avg_oos_daily, "actual": robust_avg_oos, "threshold": args.min_robust_avg_oos_daily},
        {"name": "max_avg_dd_pct", "passed": avg_dd <= args.max_avg_dd, "actual": avg_dd, "threshold": args.max_avg_dd},
        {"name": "max_avg_cost_drag_pct", "passed": cost_drag <= args.max_avg_cost_drag, "actual": cost_drag, "threshold": args.max_avg_cost_drag},
    ]
    # 観測用（passed に寄与しない）: 従来の単純平均。非堅牢試行の外れ値で引きずられるため
    # 基準割れ＝即 NG とはしない（運用時の気づきとして残す）。
    observational_checks = [
        {
            "name": "min_avg_oos_daily_pnl_observational",
            "passed": avg_oos >= args.min_oos_daily,
            "actual": avg_oos,
            "threshold": args.min_oos_daily,
            "role": "observational",
        },
        {
            "name": "min_avg_oos_daily_pnl_by_config_observational",
            "passed": avg_oos_by_cfg >= args.min_oos_daily,
            "actual": avg_oos_by_cfg,
            "threshold": args.min_oos_daily,
            "role": "observational",
        },
    ]

    payload = {
        "computed_at": _now_iso(),
        "window_days": int(args.days),
        "passed": all(bool(c["passed"]) for c in core_checks),
        "metrics": {
            **m,
            "robust_rate": robust_rate,
        },
        "checks": core_checks,
        "observational_checks": observational_checks,
        "notes": [
            "このゲートはバックテスト品質評価で、Phase1判定の補助入力として使う。",
            "合否判定は median / trimmed mean / robust-only avg を使い、単純平均は観測用。",
            "観測用に per-config 等重み平均（avg_oos_daily_pnl_by_config）と top_repeat_clusters を併記。",
            "per-config 平均が単純平均より低い場合、ポジティブ側に反復集中している兆候（デーモンが狭いグリッドに滞留）。",
        ],
    }
    prune_summary: dict | None = None
    if args.prune_universe and not args.dry_run:
        prune_summary = _prune_paper_universe()
        payload["prune_universe"] = prune_summary

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.dry_run:
        print(text)
        return
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(str(out))
    if prune_summary is not None:
        print(f"prune_universe: {prune_summary}")


if __name__ == "__main__":
    main()
