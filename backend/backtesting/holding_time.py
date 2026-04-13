"""保有時間分析 — 手法ごとの平均保有時間からエントリー締切を動的に計算.

保有時間の95パーセンタイルを基準に:
  締切時刻 = 15:25(強制決済) - 95pct保有時間

例: 平均保有10分、95pct=30分 → 締切 = 14:55
例: 平均保有60分、95pct=120分 → 締切 = 13:25
"""
from __future__ import annotations

import json
import logging
import numpy as np
import pandas as pd

from backend.backtesting.engine import run_backtest
from backend.backtesting.strategy_factory import create as create_strategy
from backend.lab.runner import JP_CAPITAL_JPY, MARGIN_RATIO, POSITION_PCT, LOT_SIZE

logger = logging.getLogger(__name__)

EOD_MINUTES = 15 * 60 + 25  # 15:25 = 925分

# キャッシュ
_holding_stats: dict[str, dict] = {}


def measure_holding_time(strategy_name: str, symbol: str, params: dict,
                         df: pd.DataFrame) -> dict:
    """手法の保有時間統計を計測する。"""
    try:
        strat = create_strategy(strategy_name, symbol, params=params)
        result = run_backtest(
            strat, df,
            starting_cash=JP_CAPITAL_JPY * MARGIN_RATIO,
            fee_pct=0.0, position_pct=POSITION_PCT,
            usd_jpy=1.0, lot_size=LOT_SIZE,
            limit_slip_pct=0.003, eod_close_time=(15, 25),
        )
    except Exception:
        return {}

    if not result.trades:
        return {}

    durations = []
    for t in result.trades:
        try:
            entry = pd.Timestamp(t.entry_time)
            exit_ = pd.Timestamp(t.exit_time)
            dur_min = (exit_ - entry).total_seconds() / 60
            if dur_min > 0:
                durations.append(dur_min)
        except Exception:
            pass

    if not durations:
        return {}

    arr = np.array(durations)
    stats = {
        "strategy": strategy_name,
        "symbol": symbol,
        "n_trades": len(durations),
        "mean_min": round(float(np.mean(arr)), 1),
        "median_min": round(float(np.median(arr)), 1),
        "p75_min": round(float(np.percentile(arr, 75)), 1),
        "p95_min": round(float(np.percentile(arr, 95)), 1),
        "max_min": round(float(np.max(arr)), 1),
    }

    # エントリー締切 = EOD - 95pct保有時間（最低でも9:30、最大14:55）
    cutoff_min = max(9 * 60 + 30, min(EOD_MINUTES - stats["p95_min"], 14 * 60 + 55))
    stats["entry_cutoff_min"] = int(cutoff_min)
    stats["entry_cutoff"] = f"{int(cutoff_min // 60):02d}:{int(cutoff_min % 60):02d}"

    _holding_stats[strategy_name] = stats
    return stats


def get_entry_cutoff(strategy_name: str) -> int:
    """エントリー締切時刻を分で返す。データなければデフォルト。"""
    stats = _holding_stats.get(strategy_name)
    if stats:
        return stats["entry_cutoff_min"]
    # デフォルト: Scalpは14:50、それ以外は14:30
    defaults = {
        "Scalp": 14 * 60 + 50,
        "MacdRci": 14 * 60 + 30,
        "EnhancedMacdRci": 14 * 60 + 30,
        "Breakout": 14 * 60 + 30,
        "Momentum5Min": 14 * 60,
        "ORB": 14 * 60,
        "VwapReversion": 14 * 60 + 30,
    }
    return defaults.get(strategy_name, 14 * 60 + 30)


def get_all_stats() -> dict[str, dict]:
    """全手法の保有時間統計を返す。"""
    return dict(_holding_stats)
