#!/usr/bin/env python3
"""N4: 6954.T (FANUC) MacdRci 60d 5m 実測ヘルスチェック.

5/3 daemon で新規 Robust 入りした 6954.T MacdRci を 60d 5m 実測で検証し、
N2 の四象限分類 (Q1/Q2/Q3/Q4) でどこに位置するかを判定する。

判定:
  - WF OOS=+4,267 / OOS_pf=2.28 / OOS_trades=30 (sample sufficient)
  - 60d 5m 実測 PnL/日 が:
    - 正  → Q1 (両方+) → universe 投入 mult=1.0 で OK
    - 負  → Q2 (WFのみ+) → universe 投入見送り or mult=0.5 慎重
    - +200 円/日未満 → Q2 寄り → mult=0.5 で慎重投入

8411.T (oos_trades=17) との比較:
  - 6954.T は oos_trades=30 で paper_low_sample_excluded の閾値ちょうど
    → low sample で弾かれない、サンプル基盤は 8411.T より厚い

出力: data/n4_validate_6954_macdrci.json
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.backtesting.engine import run_backtest
from backend.backtesting.strategy_factory import create as create_strategy

JST = timezone(timedelta(hours=9))

SYMBOL = "6954.T"

PARAMS = {
    "interval": "5m",
    "tp_pct": 0.010357,
    "sl_pct": 0.005,
    "rci_min_agree": 2,
    "macd_signal": 5,
    "macd_fast": 3,
    "macd_slow": 12,
    "entry_profile": 1,
    "exit_profile": 2,
    "hist_exit_delay_bars": 3,
    "rci_exit_min_agree": 3,
    "rci_entry_mode": 1,
    "rci_gc_slope_lookback": 4,
    "rci_gc_slope_enabled": 1,
    "rci_gc_slope_min": -30.0,
    "rci_gc_slope_max": -3.106588,
}

WF_OOS_DAILY = 4267.198840590456
WF_OOS_PF = 2.2829660718484517
WF_OOS_WR = 53.33
WF_OOS_TRADES = 30


def fetch_5m(symbol: str) -> pd.DataFrame:
    end = datetime.now(JST) + timedelta(days=1)
    start = end - timedelta(days=59)
    df = yf.download(symbol, start=start, end=end, interval="5m",
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert("Asia/Tokyo")
    else:
        df.index = df.index.tz_convert("Asia/Tokyo")
    df = df[df.index.map(lambda t: 9 <= t.hour < 15 or (t.hour == 15 and t.minute < 30))]
    return df


def main() -> None:
    print(f"=== N4: {SYMBOL} MacdRci 60d 5m 実測ヘルスチェック ===\n")
    print(f"WF reference: oos_daily=+{WF_OOS_DAILY:.0f} / pf={WF_OOS_PF:.2f} "
          f"/ wr={WF_OOS_WR:.1f}% / trades={WF_OOS_TRADES}\n")

    df = fetch_5m(SYMBOL)
    if df.empty:
        print(f"  no data")
        return
    n_days = len(set(df.index.date))
    print(f"data: {len(df)} bars / {n_days} days\n")

    strat = create_strategy("MacdRci", SYMBOL, params=PARAMS)
    result = run_backtest(strat, df, starting_cash=990_000, fee_pct=0.0,
                          position_pct=1.0, usd_jpy=1.0, lot_size=100,
                          limit_slip_pct=0.0008, eod_close_time=(15, 25))
    trades = result.trades

    if not trades:
        print(f"no trades — Q3 (60d=0, WF+)、慎重判定 mult=0.5 で投入を検討")
        out = {
            "symbol": SYMBOL, "strategy": "MacdRci",
            "n_trades_60d": 0, "wr_60d": 0, "pf_60d": 0, "pnl_60d": 0, "pnl_per_day_60d": 0,
            "wf_oos_daily": WF_OOS_DAILY,
            "verdict": "Q2_NO_TRADES", "lot_multiplier": 0.5,
            "generated_at": datetime.now(JST).isoformat(),
        }
        Path("data/n4_validate_6954_macdrci.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return

    wins = [t for t in trades if t.pnl > 0]
    gw = sum(t.pnl for t in wins)
    gl = abs(sum(t.pnl for t in trades if t.pnl <= 0))
    wr = len(wins) / len(trades) * 100
    pf = gw / gl if gl > 0 else 0
    total = sum(t.pnl for t in trades)
    pnl_per_day = total / n_days if n_days else 0
    ratio = pnl_per_day / WF_OOS_DAILY if WF_OOS_DAILY > 0 else 0

    print(f"  n={len(trades)} wr={wr:.1f}% pf={pf:.2f} "
          f"total_pnl={total:+.0f} pnl/d={pnl_per_day:+.0f} 円")
    print(f"  ratio (60d / WF) = {ratio:.2f}\n")

    if pnl_per_day < 0:
        verdict = "Q2"
        lot_mult = 0.5
        action = "60d 実測が負 → universe 投入見送り、daemon 観察継続"
    elif pnl_per_day < 200:
        verdict = "Q2_WEAK"
        lot_mult = 0.5
        action = "60d 実測 +200円/日未満 → mult=0.5 で慎重投入"
    elif ratio < 0.3:
        verdict = "Q1_OVERESTIMATE"
        lot_mult = 0.7
        action = "60d 実測 < 30% of WF → mult=0.7 で過大評価リスク抑制"
    else:
        verdict = "Q1"
        lot_mult = 1.0
        action = "60d 実測も正 → mult=1.0 で本格投入"

    print(f"=== 判定 ===\n  verdict: {verdict}")
    print(f"  推奨 lot_multiplier: {lot_mult}")
    print(f"  アクション: {action}")

    out = {
        "symbol": SYMBOL, "strategy": "MacdRci",
        "n_trades_60d": len(trades), "wr_60d": round(wr, 1),
        "pf_60d": round(pf, 2), "pnl_60d": round(total, 0),
        "pnl_per_day_60d": round(pnl_per_day, 0),
        "wf_oos_daily": WF_OOS_DAILY, "wf_oos_pf": WF_OOS_PF,
        "wf_oos_trades": WF_OOS_TRADES,
        "ratio_60d_to_wf": round(ratio, 2),
        "verdict": verdict, "lot_multiplier": lot_mult,
        "action": action,
        "params": PARAMS,
        "generated_at": datetime.now(JST).isoformat(),
    }
    Path("data/n4_validate_6954_macdrci.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: data/n4_validate_6954_macdrci.json")


if __name__ == "__main__":
    main()
