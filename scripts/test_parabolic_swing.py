#!/usr/bin/env python3
"""JPParabolicSwing 戦略のスタンドアロン検証スクリプト.

engine 統合前の動作確認用:
- 1d / 1h / 15m を fetch_ohlcv で取得
- 戦略インスタンスに attach
- generate_signals を呼び、signal=1 / -1 の発生回数と内訳をダンプ

使い方::

    cd /path/to/algo-trading-system
    .venv/bin/python scripts/test_parabolic_swing.py --symbols 6613.T,3103.T,9984.T --days-15m 60

15m と 1h は yfinance の制約により実質的に直近 60 日強までしか遡れない点に注意。
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

import pandas as pd  # noqa: E402

from backend.lab.runner import fetch_ohlcv  # noqa: E402
from backend.strategies.jp_stock.jp_parabolic_swing import JPParabolicSwing  # noqa: E402

JST = timezone(timedelta(hours=9))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_parabolic_swing")


async def _evaluate_one(
    symbol: str,
    *,
    days_15m: int,
    days_1h: int,
    days_d: int,
) -> dict:
    df_15 = await fetch_ohlcv(symbol, "15m", days_15m)
    df_h1 = await fetch_ohlcv(symbol, "1h", days_1h)
    df_d = await fetch_ohlcv(symbol, "1d", days_d)
    if df_15 is None or df_15.empty:
        return {"symbol": symbol, "error": "no_15m"}
    if df_h1 is None or df_h1.empty:
        return {"symbol": symbol, "error": "no_1h"}
    if df_d is None or df_d.empty:
        return {"symbol": symbol, "error": "no_1d"}

    name = symbol.replace(".T", "")
    strat = JPParabolicSwing(symbol, name)
    strat.attach(df_d=df_d, df_h1=df_h1)

    sig = strat.generate_signals(df_15)
    n_total = len(sig)
    n_entry = int((sig["signal"] == 1).sum())
    n_exit = int((sig["signal"] == -1).sum())

    # entry の例（最大 5 件）
    entries = sig[sig["signal"] == 1].tail(5).copy()
    entry_examples = []
    for ts, row in entries.iterrows():
        entry_examples.append({
            "ts": str(ts),
            "close": float(row["close"]),
            "psar_d": float(row.get("psar_d", float("nan"))),
            "psar_15m": float(row.get("psar_15m", float("nan"))),
            "rci_d": float(row.get("rci_d", float("nan"))),
            "stop_loss": float(row["stop_loss"]) if not (row["stop_loss"] != row["stop_loss"]) else None,
            "armed_lookback_bars_with_overshoot": int(row.get("_armed", False)),
        })

    # 直近の状態スナップ
    last = sig.iloc[-1]
    snap = {
        "ts": str(sig.index[-1]),
        "close": float(last["close"]),
        "psar_15m": float(last.get("psar_15m", float("nan"))),
        "psar_15m_trend": float(last.get("psar_15m_trend", float("nan"))),
        "psar_d": float(last.get("psar_d", float("nan"))),
        "psar_trend_d": float(last.get("psar_trend_d", float("nan"))),
        "rci_d": float(last.get("rci_d", float("nan"))),
        "rci_up_d": float(last.get("rci_up_d", float("nan"))),
        "rci_high_count_h1": int(last.get("_rci_high_count_h1", 0)),
        "ma_dev_h1": float(last.get("_ma_dev_h1", float("nan"))),
        "armed": bool(last.get("_armed", False)),
        "overshoot_level": float(last.get("_overshoot_level", float("nan"))),
        "entry_level": float(last.get("_entry_level", float("nan"))),
    }

    return {
        "symbol": symbol,
        "bars_15m": int(len(df_15)),
        "bars_1h": int(len(df_h1)),
        "bars_1d": int(len(df_d)),
        "first_15m_ts": str(df_15.index[0]),
        "last_15m_ts": str(df_15.index[-1]),
        "n_signals_total": n_total,
        "n_signal_long_entry": n_entry,
        "n_signal_long_exit": n_exit,
        "entry_examples_tail5": entry_examples,
        "last_bar_snapshot": snap,
        "skip_reason": str(sig.get("_skip_reason", pd.Series([None])).iloc[-1])
        if "_skip_reason" in sig.columns
        else None,
    }


async def _main(args: argparse.Namespace) -> None:
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    results = []
    for sym in symbols:
        try:
            r = await _evaluate_one(
                sym,
                days_15m=args.days_15m,
                days_1h=args.days_1h,
                days_d=args.days_d,
            )
        except Exception as exc:
            r = {"symbol": sym, "error": f"exception: {exc!r}"}
        results.append(r)
        logger.info("done: %s", sym)

    payload = {
        "generated_at": datetime.now(JST).isoformat(),
        "params_default": True,
        "args": {
            "symbols": symbols,
            "days_15m": args.days_15m,
            "days_1h": args.days_1h,
            "days_d": args.days_d,
        },
        "results": results,
    }
    out = Path(args.out) if args.out else ROOT / "data" / "test_parabolic_swing_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="6613.T,3103.T,9984.T,6752.T,1605.T")
    ap.add_argument("--days-15m", type=int, default=60)
    ap.add_argument("--days-1h", type=int, default=60)
    ap.add_argument("--days-d", type=int, default=730)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
