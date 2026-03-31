"""BTC EMA Cross Strategy — scalping on 5m chart.

Logic:
  Entry : EMA_fast crosses above EMA_slow  (golden cross)
  Exit  : EMA_fast crosses below EMA_slow  (death cross)
          OR stop_loss hit  OR take_profit hit

Tunable params: ema_fast, ema_slow, stop_pct, tp_pct
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase, StrategyMeta


class BTCEmaCross(StrategyBase):

    def __init__(self, ema_fast: int = 9, ema_slow: int = 21,
                 stop_pct: float = 0.003, tp_pct: float = 0.006,
                 interval: str = "5m") -> None:
        self.ema_fast  = ema_fast
        self.ema_slow  = ema_slow
        self.stop_pct  = stop_pct
        self.tp_pct    = tp_pct
        self.meta = StrategyMeta(
            id=f"btc_ema_cross_{interval}",
            name=f"BTC EMA Cross [{interval}]",
            symbol="BTC-USD",
            interval=interval,
            description=f"EMA{ema_fast}/{ema_slow} golden-cross scalping on BTC {interval} candles.",
            params={"ema_fast": ema_fast, "ema_slow": ema_slow,
                    "stop_pct": stop_pct, "tp_pct": tp_pct, "interval": interval},
        )

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        d["ema_fast"] = d["close"].ewm(span=self.ema_fast, adjust=False).mean()
        d["ema_slow"] = d["close"].ewm(span=self.ema_slow, adjust=False).mean()

        d["cross_up"]   = (d["ema_fast"] > d["ema_slow"]) & (d["ema_fast"].shift(1) <= d["ema_slow"].shift(1))
        d["cross_down"] = (d["ema_fast"] < d["ema_slow"]) & (d["ema_fast"].shift(1) >= d["ema_slow"].shift(1))

        d["signal"]      = 0
        d["stop_loss"]   = np.nan
        d["take_profit"] = np.nan

        d.loc[d["cross_up"],   "signal"] = 1
        d.loc[d["cross_down"], "signal"] = -1

        # Attach SL/TP to entry signals
        entry_mask = d["signal"] == 1
        d.loc[entry_mask, "stop_loss"]   = d.loc[entry_mask, "close"] * (1 - self.stop_pct)
        d.loc[entry_mask, "take_profit"] = d.loc[entry_mask, "close"] * (1 + self.tp_pct)

        return d
