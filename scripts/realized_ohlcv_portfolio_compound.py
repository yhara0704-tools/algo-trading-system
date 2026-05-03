#!/usr/bin/env python3
"""universe_active の採用パラで、実 OHLCV を取り `portfolio_sim.simulate` を1回回す.

`backtest_daily_agg` の合算とは独立し、**単一の余力プール**で約定が並ぶ現実寄りの窓。
取得日数は ``fetch_ohlcv(..., days=)`` の要求と、J-Quants 側 ``JQUANTS_5M_MAX_DAYS``（既定 14）
の **実効 cap** の小さい方になる（5 分足の場合）。

出力: JSON を stdout + `--out`（既定 data/realized_ohlcv_portfolio_latest.json）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.backtesting.portfolio_sim import simulate  # noqa: E402
from backend.backtesting.portfolio_sim import BUYING_POWER  # noqa: E402
from backend.backtesting.strategy_factory import resolve_jp_ohlcv_interval  # noqa: E402
from backend.lab.paper_backtest_sync import collect_universe_specs  # noqa: E402
from backend.lab.runner import fetch_ohlcv, JP_CAPITAL_JPY, MARGIN_RATIO  # noqa: E402
from backend.lab.runner import _fetch_file_cache  # noqa: E402

JST = timezone(timedelta(hours=9))


def _span_days(df) -> float | None:
    if df is None or df.empty or len(df.index) < 2:
        return None
    try:
        delta = df.index.max() - df.index.min()
        return float(delta / timedelta(days=1))
    except Exception:
        return None


async def _run(
    *,
    fetch_days_requested: int,
    max_configs: int,
    sample_filter: bool,
) -> dict:
    _FIVEM_MAX_DAYS = int(os.getenv("JQUANTS_5M_MAX_DAYS", "14"))

    specs = collect_universe_specs(
        max_count=max(2, max_configs),
        force_macd_max_pyramid=-1,
        apply_sample_filter=sample_filter,
    )
    if len(specs) < 2:
        return {"error": "universe specs < 2", "specs_n": len(specs)}

    configs: list[dict] = []
    for s in specs:
        configs.append({
            "strategy_name": s["strategy_name"],
            "symbol": s["symbol"],
            "params": dict(s.get("params") or {}),
            # N5: lot_multiplier を portfolio_sim に渡す
            "lot_multiplier": float(s.get("lot_multiplier", 1.0) or 1.0),
        })

    df_cache: dict[str, object] = {}
    bar_reports: list[dict] = []
    eff_5m_cap = min(int(fetch_days_requested), int(_FIVEM_MAX_DAYS))

    for cfg in configs:
        iv = resolve_jp_ohlcv_interval(cfg["strategy_name"], cfg.get("params") or {})
        days_use = int(fetch_days_requested)
        api_days_use = int(fetch_days_requested) if iv != "5m" else eff_5m_cap
        # 長期検証では、まずローリング蓄積済みファイルキャッシュを優先する。
        # fetch_ohlcv() は J-Quants 5m(最大14日)を先に返すため、ここで先取りすると
        # 蓄積窓があるのに短窓へ退化してしまう。
        df = _fetch_file_cache(cfg["symbol"], iv, days_use)
        if df is None or df.empty:
            df = await fetch_ohlcv(cfg["symbol"], iv, api_days_use)
        ck = f"{cfg['symbol']}::{iv}"
        df_cache[ck] = df
        span = _span_days(df)
        bar_reports.append({
            "symbol": cfg["symbol"],
            "strategy": cfg["strategy_name"],
            "interval": iv,
            "days_requested": days_use,
            "api_days_used_when_fallback": api_days_use,
            "bars": 0 if df is None else len(df),
            "span_calendar_days_approx": span,
        })

    result = simulate(configs, df_cache)  # type: ignore[arg-type]

    dmap = result.daily_pnl_by_date or {}
    dates = sorted(dmap.keys())
    daily_list = [dmap[d] for d in dates]
    start_eq = float(BUYING_POWER)
    end_eq = start_eq + float(result.total_pnl_jpy or 0.0)
    n_days = len(daily_list)
    geom = None
    if n_days > 0 and start_eq > 0 and end_eq > 0:
        geom = (pow(end_eq / start_eq, 1.0 / n_days) - 1.0) * 100.0

    return {
        "computed_at": datetime.now(JST).isoformat(),
        "fetch_days_requested": int(fetch_days_requested),
        "jquants_5m_max_days_cap": int(_FIVEM_MAX_DAYS),
        "effective_5m_fetch_days": eff_5m_cap,
        "jp_capital_jpy": JP_CAPITAL_JPY,
        "margin_ratio": MARGIN_RATIO,
        "simulate_starting_cash_jpy": start_eq,
        "end_equity_jpy": round(end_eq, 2),
        "total_pnl_jpy": round(float(result.total_pnl_jpy or 0.0), 2),
        "total_return_pct": round((end_eq / start_eq - 1.0) * 100.0, 4) if start_eq else None,
        "trading_days_with_pnl": n_days,
        "first_pnl_date": dates[0] if dates else None,
        "last_pnl_date": dates[-1] if dates else None,
        "avg_daily_pnl_jpy": round(float(result.daily_pnl_jpy or 0.0), 2),
        "implied_geom_daily_return_pct": round(geom, 6) if geom is not None else None,
        "sharpe": round(float(result.sharpe or 0.0), 4),
        "max_drawdown_pct": round(float(result.max_drawdown_pct or 0.0), 4),
        "total_trades": int(result.total_trades or 0),
        "configs_used": [{"symbol": c["symbol"], "strategy": c["strategy_name"]} for c in configs],
        "ohlcv_fetch_report": bar_reports,
        "sample_filter": bool(sample_filter),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-days", type=int, default=90, help="要求日数（5m は J-Quants cap で切詰め）")
    ap.add_argument("--max-configs", type=int, default=12)
    ap.add_argument("--no-sample-filter", action="store_true",
                    help="OOS trades 閾値で universe から落とさない（研究用）")
    ap.add_argument("--out", default=str(ROOT / "data" / "realized_ohlcv_portfolio_latest.json"))
    args = ap.parse_args()

    payload = asyncio.run(_run(
        fetch_days_requested=args.fetch_days,
        max_configs=args.max_configs,
        sample_filter=not args.no_sample_filter,
    ))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
