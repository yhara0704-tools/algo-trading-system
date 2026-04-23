#!/usr/bin/env python3
"""指定日の「ペーパーと同じユニバースでバックテストしていたら」当日実現損益の近似。

材料は ``paper_backtest_sync.collect_universe_specs``（universe_active + fit + macd）と
``lab.runner.fetch_ohlcv`` / ``run_backtest``（エンジン既定に runner の JP 建玉・手数料に近い設定）。

注意（ペーパー本番との差）:
  - 戦略ごとに **独立した余力** で回している（1口座の資金配分・同時保有の競合は未再現）。
  - 当日の post_loss_gate / A-B 実験タグ / AgentGate / 場中の手動 halt は入れていない。
  - それでも「その日データで正本パラメータが何円動いたか」の目安には使える。
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _exit_date_jst(exit_ts: str) -> str:
    ts = pd.Timestamp(exit_ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("Asia/Tokyo", ambiguous="infer", nonexistent="shift_forward")
    else:
        ts = ts.tz_convert("Asia/Tokyo")
    return ts.strftime("%Y-%m-%d")


async def _one_day_row(target: str, row: dict, days: int) -> dict:
    from backend.backtesting.engine import run_backtest
    from backend.backtesting.strategy_factory import create as create_strategy
    from backend.capital_tier import get_tier
    from backend.lab.runner import JP_CAPITAL_JPY, LOT_SIZE, fetch_ohlcv

    sym = row["symbol"]
    sname = row["strategy_name"]
    try:
        strat = create_strategy(
            sname,
            sym,
            name=str(row.get("name") or sym.replace(".T", "")),
            params=row.get("params") or {},
        )
    except Exception as e:
        return {"symbol": sym, "strategy": sname, "error": str(e), "day_pnl": 0.0, "trades_today": 0}

    interval = strat.meta.interval
    df = await fetch_ohlcv(sym, interval, days)
    if df is None or df.empty:
        return {"symbol": sym, "strategy": sname, "error": "no_ohlcv", "day_pnl": 0.0, "trades_today": 0}

    _last = pd.Timestamp(df.index[-1])
    if _last.tzinfo is None:
        _last = _last.tz_localize("Asia/Tokyo", ambiguous="infer", nonexistent="shift_forward")
    else:
        _last = _last.tz_convert("Asia/Tokyo")
    last_d = _last.strftime("%Y-%m-%d")
    if last_d < target:
        return {
            "symbol": sym,
            "strategy": sname,
            "error": f"ohlcv_stale(last={last_d})",
            "day_pnl": 0.0,
            "trades_today": 0,
        }

    tier = get_tier(JP_CAPITAL_JPY)
    s_cash = JP_CAPITAL_JPY * tier.margin
    liq = tier.effective_position(sym)
    max_by_liq = liq / s_cash if s_cash > 0 else tier.position_pct
    s_pos_pct = min(tier.position_pct, max_by_liq)

    loop = asyncio.get_running_loop()
    run_kw = dict(
        starting_cash=s_cash,
        fee_pct=0.0,
        position_pct=s_pos_pct,
        usd_jpy=1.0,
        lot_size=LOT_SIZE,
        limit_slip_pct=0.003,
        short_borrow_fee_annual=0.011,
        eod_close_time=(15, 20),
    )
    result = await loop.run_in_executor(
        None,
        functools.partial(run_backtest, strat, df, **run_kw),
    )

    day_pnl = 0.0
    day_trades = 0
    for t in result.trades:
        try:
            if _exit_date_jst(t.exit_time) == target:
                day_pnl += float(t.pnl)
                day_trades += 1
        except Exception:
            continue

    return {
        "symbol": sym,
        "strategy": sname,
        "interval": interval,
        "error": "",
        "day_pnl": day_pnl,
        "trades_today": day_trades,
        "trades_window": int(result.num_trades or 0),
    }


async def _run(target: str, max_n: int, days: int) -> list[dict]:
    sys.path.insert(0, str(ROOT))
    from backend.lab.paper_backtest_sync import collect_universe_specs

    rows = collect_universe_specs(
        max_count=max_n,
        force_macd_max_pyramid=-1,
        apply_sample_filter=False,
    )
    if not rows:
        return []

    tasks = [_one_day_row(target, r, days) for r in rows]
    return await asyncio.gather(*tasks)


def main() -> int:
    ap = argparse.ArgumentParser(description="JP paper counterfactual PnL for one calendar day (JST)")
    ap.add_argument(
        "--date",
        default="",
        help="対象日 YYYY-MM-DD（省略時は今日 JST）",
    )
    ap.add_argument("--max", type=int, default=12, help="ユニバース先頭から最大何銘柄×手法を試すか")
    ap.add_argument("--days", type=int, default=14, help="fetch_ohlcv に渡す過去日数（指標ウォームアップ用）")
    args = ap.parse_args()

    target = (args.date or "").strip()
    if not target:
        target = datetime.now(JST).strftime("%Y-%m-%d")

    os.chdir(ROOT)  # noqa: PTH208 — スクリプト単体実行向け
    rows_out = asyncio.run(_run(target, args.max, args.days))

    if not rows_out:
        print("ユニバースが空です（universe_active / fit / macd を確認）")
        return 1

    print(f"対象日(JST): {target}  （各戦略は独立余力・本番ペーパーと完全一致ではありません）\n")
    total = 0.0
    tt = 0
    for r in rows_out:
        err = r.get("error") or ""
        pnl = float(r.get("day_pnl") or 0.0)
        n = int(r.get("trades_today") or 0)
        total += pnl
        tt += n
        ex = f"  ERR {err}" if err else ""
        print(
            f"{r['symbol']:8} {r['strategy']:14} {r.get('interval',''):4}  "
            f"当日実現 {pnl:+,.0f}円  ({n}件){ex}"
        )
    print("-" * 56)
    print(f"単純合算（参考）: {total:+,.0f}円  当日約定トレード数合計 {tt}（戦略独立のため二重計上の可能性あり）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
