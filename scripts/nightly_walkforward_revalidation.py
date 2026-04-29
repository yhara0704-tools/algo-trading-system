#!/usr/bin/env python3
"""Phase F1: 夜間ウォークフォワード再検証.

`universe_active.json` に載っている全戦略 × 銘柄に対し、
`walkforward_robust_macd_rci.py` と同じロジックで walkforward を実行し、
OOS 日次 PnL / win_rate / pass_ratio を記録する。

- 出力: data/nightly_walkforward_latest.json（最新 1 件）
- 閾値を下回った {symbol, strategy} は `demote_candidates` に集約し、
  `evaluate_backtest_quality_gate.py` が paper universe から除外する際の入力になる。
- Pushover: severe 乖離（>=3 銘柄 × 閾値割れ）は通知する。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.backtesting.engine import run_backtest  # noqa: E402
from backend.backtesting.strategy_factory import create as create_strategy  # noqa: E402
from backend.backtesting.walkforward import rolling_splits, summarize_oos  # noqa: E402
from backend.lab.runner import (  # noqa: E402
    JP_CAPITAL_JPY,
    LOT_SIZE,
    MARGIN_RATIO,
    POSITION_PCT,
    fetch_ohlcv,
)

JST = timezone(timedelta(hours=9))
DATA_DIR = ROOT / "data"
UNIVERSE_ACTIVE = DATA_DIR / "universe_active.json"
MACD_PARAMS_FILE = DATA_DIR / "macd_rci_params.json"
OUT_LATEST = DATA_DIR / "nightly_walkforward_latest.json"
OUT_HISTORY_DIR = DATA_DIR / "nightly_walkforward_history"

logger = logging.getLogger("nightly_wf")

MIN_OOS_WIN_RATE = 50.0  # OOS 勝率閾値
MIN_PASS_RATIO = 0.5     # 窓通過率閾値
MIN_TOTAL_TRADES_FOR_DEMOTE = 6  # 2026-04-28: 取引 0 件のペアを demote 対象から除外する閾値

# 2026-04-28: Premium-free 銘柄リスト（短期空売りプレミアム 0%）。
# `walkforward_strategy_compare.py` と一致させる。それ以外は 0.3%/日の保守値。
_PREMIUM_FREE_SYMBOLS = {
    "8306.T", "9432.T", "7203.T", "7267.T", "4568.T", "9433.T",
    "6758.T", "6954.T", "9984.T", "8316.T", "8411.T", "8604.T",
    "6098.T", "7201.T", "5401.T", "4502.T", "6752.T", "7269.T",
    "9468.T", "4911.T", "3382.T", "6613.T", "3103.T", "1605.T",
}

# 2026-04-28: macd_rci_params.json に堆積するメトリクスキーは、戦略コンストラクタに
# 渡しても無視されるが、念のため除去してデフォルトと衝突しないようにする。
_METRIC_KEYS = frozenset({
    "is_daily", "is_pf", "is_win_rate", "is_trades",
    "oos_daily", "oos_pf", "oos_win_rate", "oos_trades",
    "robust", "is_oos_pass", "last_updated",
    "regime_suitability", "best_regime", "worst_regime",
    "wf_window_pass_ratio", "calmar", "score",
})


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _strip_metrics(d: dict) -> dict:
    return {k: v for k, v in (d or {}).items() if k not in _METRIC_KEYS}


def _run_bt(strat, df, sym: str):
    # 2026-04-28: 旧実装は `starting_cash=JP_CAPITAL_JPY (=30万円)` のみで MARGIN_RATIO/
    # fee/slip/eod を渡しておらず、株価×100株が 30万円を超える銘柄は建玉できず
    # 全 split で「取引 0 件 / pnl=0」になっていた（4/28 03:10 cron で 12 件中 11 件が
    # 0/2 wins）。ここを paper 実運用と同じ条件に揃える。
    premium = 0.0 if sym in _PREMIUM_FREE_SYMBOLS else 0.003
    return run_backtest(
        strat, df,
        starting_cash=JP_CAPITAL_JPY * MARGIN_RATIO,
        fee_pct=0.0,
        position_pct=POSITION_PCT,
        usd_jpy=1.0,
        lot_size=LOT_SIZE,
        limit_slip_pct=0.003,
        eod_close_time=(15, 25),
        short_borrow_fee_annual=0.0,
        short_premium_daily_pct=premium,
    )


async def _evaluate_one(sym: str, strat_name: str, params: dict, days: int = 90) -> dict:
    params = _strip_metrics(params)
    df = await fetch_ohlcv(sym, interval=params.get("interval", "5m"), days=days)
    if df is None or len(df) < 120:
        return {"symbol": sym, "strategy": strat_name, "skipped": "insufficient_bars", "bars": 0 if df is None else len(df)}

    # 2026-04-29: universe_active.json には manual_observation 経由で正規 strategy_factory に
    # 未登録の戦略名（例: ORB, Momentum5Min）が混入する。1 件の ValueError でスクリプト全体を
    # 落として JSON 未出力のままになると、翌朝以降も demote_candidates が更新されない（4/29
    # 朝の cron がこの理由で死亡し、JSON が 4/28 のまま固着した）。create に失敗したら
    # warn して skip するだけに留め、他のペアの判定は継続させる。
    try:
        strat = create_strategy(strat_name, sym, name=sym.replace(".T", ""), params=params)
    except Exception as e:
        logger.warning("skip %s %s: create_strategy failed: %s", sym, strat_name, e)
        return {
            "symbol": sym,
            "strategy": strat_name,
            "skipped": "unknown_strategy" if isinstance(e, ValueError) else "create_failed",
            "error": str(e),
        }
    splits = rolling_splits(df)
    if not splits:
        return {"symbol": sym, "strategy": strat_name, "skipped": "no_splits"}

    oos_pnls: list[float] = []
    win_rates: list[float] = []
    daily_pnls: list[float] = []
    total_trades = 0
    for sp in splits:
        if len(sp.oos_df) < 40:
            continue
        try:
            r = _run_bt(strat, sp.oos_df, sym)
        except Exception as e:
            logger.debug("skip %s %s: %s", sym, strat_name, e)
            continue
        oos_pnls.append(float(getattr(r, "daily_pnl_jpy", 0.0)))
        win_rates.append(float(getattr(r, "win_rate", 0.0)))
        daily_pnls.append(float(getattr(r, "daily_pnl_jpy", 0.0)))
        total_trades += int(getattr(r, "num_trades", 0))

    summary = summarize_oos(oos_pnls)
    win_rate_mean = sum(win_rates) / len(win_rates) if win_rates else 0.0
    daily_mean = sum(daily_pnls) / len(daily_pnls) if daily_pnls else 0.0
    return {
        "symbol": sym,
        "strategy": strat_name,
        "oos_win_rate": round(win_rate_mean, 3),
        "oos_daily_mean": round(daily_mean, 2),
        "oos_total_trades": total_trades,
        "wins": int(summary.get("wins", 0)),
        "windows": int(summary.get("count", 0)),
        "pass_ratio": round(float(summary.get("pass_ratio", 0.0)), 3),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--max-symbols", type=int, default=50)
    ap.add_argument("--notify", action="store_true", help="閾値割れを Pushover 通知する")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    active = _load_json(UNIVERSE_ACTIVE, {})
    macd_params = _load_json(MACD_PARAMS_FILE, {})
    rows = (active or {}).get("symbols", [])
    if not rows:
        logger.warning("universe_active.json が空。何もしない。")
        return 0

    rows = rows[: args.max_symbols]
    results: list[dict] = []
    for row in rows:
        sym = row.get("symbol")
        strat_name = row.get("strategy")
        if not sym or not strat_name:
            continue
        params: dict = {}
        if strat_name == "MacdRci":
            params = dict(macd_params.get(sym, {}))
            params.setdefault("interval", "5m")
        # 1 ペアの想定外例外（OHLCV 取得失敗・戦略内 IndexError 等）でも cron 全体を
        # 落とさない。4/29 cron は ValueError: Unknown strategy: ORB で死亡し
        # nightly_walkforward_latest.json が 4/28 から固着していた。
        try:
            r = await _evaluate_one(sym, strat_name, params, days=args.days)
        except Exception as e:
            logger.exception("evaluate failed %s %s: %s", sym, strat_name, e)
            r = {
                "symbol": sym,
                "strategy": strat_name,
                "skipped": "evaluate_exception",
                "error": str(e),
            }
        results.append(r)
        logger.info("done %s %s: %s", sym, strat_name, r)

    # 閾値割れ集約
    # 2026-04-28: 取引数が極端に少ない（資金不足やデフォルト params で発火しない）
    # ペアは demote 対象から除外する。`MIN_TOTAL_TRADES_FOR_DEMOTE` 未満の場合は
    # `low_sample` として別カテゴリで観測のみとし、universe 除外には用いない。
    demote: list[dict] = []
    low_sample: list[dict] = []
    for r in results:
        if r.get("skipped"):
            continue
        total_trades = int(r.get("oos_total_trades", 0))
        below = (
            r.get("oos_win_rate", 0) < MIN_OOS_WIN_RATE
            or r.get("pass_ratio", 0) < MIN_PASS_RATIO
        )
        if not below:
            continue
        entry = {
            k: r.get(k)
            for k in (
                "symbol", "strategy", "oos_win_rate", "oos_daily_mean",
                "oos_total_trades", "pass_ratio", "windows",
            )
        }
        if total_trades < MIN_TOTAL_TRADES_FOR_DEMOTE:
            low_sample.append(entry)
        else:
            demote.append(entry)

    skipped_summary: dict[str, list[dict]] = {}
    for r in results:
        reason = r.get("skipped")
        if not reason:
            continue
        skipped_summary.setdefault(reason, []).append(
            {"symbol": r.get("symbol"), "strategy": r.get("strategy"), "error": r.get("error")}
        )

    payload = {
        "updated_at": datetime.now(JST).isoformat(),
        "total": len(results),
        "demote_candidates": demote,
        "low_sample_candidates": low_sample,
        "skipped_summary": skipped_summary,
        "thresholds": {
            "min_oos_win_rate": MIN_OOS_WIN_RATE,
            "min_pass_ratio": MIN_PASS_RATIO,
            "min_total_trades_for_demote": MIN_TOTAL_TRADES_FOR_DEMOTE,
        },
        "results": results,
    }
    OUT_LATEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    OUT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(JST).strftime("%Y%m%d")
    (OUT_HISTORY_DIR / f"{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info(
        "nightly_wf: total=%d demote=%d low_sample=%d",
        len(results), len(demote), len(low_sample),
    )

    if args.notify and len(demote) >= 3:
        try:
            from backend.notify import push
            msg = "\n".join(
                f"- {d['symbol']} × {d['strategy']}: win%={d.get('oos_win_rate')} pass={d.get('pass_ratio')}"
                for d in demote[:8]
            )
            await push(
                title=f"夜間WF再検証: {len(demote)}戦略が閾値割れ",
                message=msg,
                priority=0,
            )
        except Exception as exc:
            logger.warning("notify failed: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
