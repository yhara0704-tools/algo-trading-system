"""BTC VWAP Mean-Reversion Strategy.

Logic:
  VWAP is recalculated each session (UTC midnight).
  Entry : price deviates > dev_pct below VWAP  AND  RSI < 45
  Exit  : price returns within exit_pct of VWAP  OR  stop hit

Good for ranging / choppy BTC sessions.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase, StrategyMeta


class BTCVwapReversion(StrategyBase):

    def __init__(self, dev_pct: float = 0.004, exit_pct: float = 0.001,
                 rsi_period: int = 14, rsi_max: int = 45,
                 stop_pct: float = 0.005, interval: str = "5m") -> None:
        self.dev_pct    = dev_pct
        self.exit_pct   = exit_pct
        self.rsi_period = rsi_period
        self.rsi_max    = rsi_max
        self.stop_pct   = stop_pct
        self.meta = StrategyMeta(
            id=f"btc_vwap_{interval}",
            name=f"BTC VWAP [{interval}]",
            symbol="BTC-USD",
            interval=interval,
            description=f"Buy >{dev_pct*100:.1f}% below VWAP on {interval}; exit at VWAP.",
            params={"dev_pct": dev_pct, "exit_pct": exit_pct,
                    "rsi_period": rsi_period, "rsi_max": rsi_max,
                    "stop_pct": stop_pct, "interval": interval},
        )

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()

        # VWAP (session = UTC day)
        d["session"] = d.index.normalize()  # requires DatetimeIndex
        d["tp"]      = (d["high"] + d["low"] + d["close"]) / 3
        d["cum_vol"] = d.groupby("session")["volume"].cumsum()
        d["cum_tpv"] = d.groupby("session").apply(
            lambda g: (g["tp"] * g["volume"]).cumsum()
        ).values
        d["vwap"] = d["cum_tpv"] / d["cum_vol"]

        # RSI
        delta = d["close"].diff()
        gain  = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss  = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rs    = gain / loss.replace(0, np.nan)
        d["rsi"] = 100 - 100 / (1 + rs)

        # VWAP deviation
        d["vwap_dev"] = (d["close"] - d["vwap"]) / d["vwap"]

        d["signal"]      = 0
        d["stop_loss"]   = np.nan
        d["take_profit"] = np.nan

        entry = (d["vwap_dev"] < -self.dev_pct) & (d["rsi"] < self.rsi_max)
        exit_ = d["vwap_dev"] > -self.exit_pct

        d.loc[entry, "signal"] = 1
        d.loc[exit_,  "signal"] = -1
        d.loc[entry, "stop_loss"]   = d.loc[entry, "close"] * (1 - self.stop_pct)
        d.loc[entry, "take_profit"] = d.loc[entry, "vwap"]

        return d
