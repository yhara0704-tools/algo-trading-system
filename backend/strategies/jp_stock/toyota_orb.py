"""Toyota Opening Range Breakout — 日計り信用（手数料無料）専用.

Logic:
  Opening range = high/low of first N minutes (default 15m).
  Entry : close breaks above range_high  → long
  Exit  : close reaches target (range * tp_ratio)
          OR close falls below range_low (stop)
          OR 14:30 JST forced exit (日計り信用は当日清算)

Matsui Securities 日計り信用: 手数料 0円.
Symbol: 7203.T (Toyota)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase, StrategyMeta


class ToyotaOpeningRangeBreakout(StrategyBase):
    meta = StrategyMeta(
        id="toyota_orb",
        name="Toyota ORB (日計り信用)",
        symbol="7203.T",
        interval="1m",
        description="Opening range breakout on Toyota. 手数料0円の日計り信用.",
        params={"range_minutes": 15, "tp_ratio": 1.5, "sl_ratio": 1.0,
                "force_exit_hour": 14, "force_exit_minute": 30},
    )

    def __init__(self, range_minutes: int = 15, tp_ratio: float = 1.5,
                 sl_ratio: float = 1.0) -> None:
        self.range_minutes = range_minutes
        self.tp_ratio      = tp_ratio
        self.sl_ratio      = sl_ratio

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        d["signal"]      = 0
        d["stop_loss"]   = np.nan
        d["take_profit"] = np.nan

        # Group by trading day
        d["date"] = d.index.date

        for date, day_df in d.groupby("date"):
            # Opening range: first range_minutes candles (9:00–9:15 JST)
            opening = day_df.iloc[:self.range_minutes]
            if len(opening) < self.range_minutes:
                continue

            range_high = opening["high"].max()
            range_low  = opening["low"].min()
            range_size = range_high - range_low
            if range_size <= 0:
                continue

            tp_price = range_high + range_size * self.tp_ratio
            sl_price = range_low

            # After opening range, look for breakout
            post_range = day_df.iloc[self.range_minutes:]
            for i, (idx, row) in enumerate(post_range.iterrows()):
                # Force exit at 14:30 JST
                if hasattr(idx, 'hour') and (idx.hour > 14 or (idx.hour == 14 and idx.minute >= 30)):
                    d.loc[idx, "signal"] = -1
                    break
                if row["close"] > range_high:
                    d.loc[idx, "signal"]      = 1
                    d.loc[idx, "stop_loss"]   = sl_price
                    d.loc[idx, "take_profit"] = tp_price
                    break

        return d
