"""BTC RSI + Bollinger Band Mean-Reversion Strategy.

Logic:
  Entry : close < lower_BB  AND  RSI < rsi_oversold  AND  close > EMA200
          (oversold bounce — trend filter prevents catching falling knives)
  Exit  : close > middle_BB  OR  RSI > rsi_exit  OR  stop_loss hit

High win-rate design: only buy dips within uptrend.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase, StrategyMeta


class BTCRsiBollinger(StrategyBase):

    def __init__(self, bb_period: int = 20, bb_std: float = 2.0,
                 rsi_period: int = 14, rsi_entry: int = 35, rsi_exit: int = 55,
                 stop_pct: float = 0.004, trend_ema: int = 200,
                 interval: str = "5m") -> None:
        self.bb_period  = bb_period
        self.bb_std     = bb_std
        self.rsi_period = rsi_period
        self.rsi_entry  = rsi_entry
        self.rsi_exit   = rsi_exit
        self.stop_pct   = stop_pct
        self.trend_ema  = trend_ema
        self.meta = StrategyMeta(
            id=f"btc_rsi_bb_{interval}",
            name=f"BTC RSI+BB [{interval}]",
            symbol="BTC-USD",
            interval=interval,
            description=f"Oversold bounce on {interval}: lower BB + RSI<{rsi_entry}, exit at mid BB.",
            params={"bb_period": bb_period, "bb_std": bb_std, "rsi_period": rsi_period,
                    "rsi_entry": rsi_entry, "rsi_exit": rsi_exit,
                    "stop_pct": stop_pct, "trend_ema": trend_ema, "interval": interval},
        )

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()

        # Bollinger Bands
        d["bb_mid"]   = d["close"].rolling(self.bb_period).mean()
        bb_std        = d["close"].rolling(self.bb_period).std()
        d["bb_lower"] = d["bb_mid"] - self.bb_std * bb_std
        d["bb_upper"] = d["bb_mid"] + self.bb_std * bb_std

        # RSI
        delta = d["close"].diff()
        gain  = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss  = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rs    = gain / loss.replace(0, np.nan)
        d["rsi"] = 100 - 100 / (1 + rs)

        # Trend filter
        d["ema_trend"] = d["close"].ewm(span=self.trend_ema, adjust=False).mean()

        # Signals
        d["signal"]      = 0
        d["stop_loss"]   = np.nan
        d["take_profit"] = np.nan

        entry = (
            (d["close"] < d["bb_lower"]) &
            (d["rsi"] < self.rsi_entry) &
            (d["close"] > d["ema_trend"])
        )
        exit_ = (
            (d["close"] > d["bb_mid"]) |
            (d["rsi"] > self.rsi_exit)
        )

        d.loc[entry, "signal"] = 1
        d.loc[exit_,  "signal"] = -1
        d.loc[entry, "stop_loss"]   = d.loc[entry, "close"] * (1 - self.stop_pct)
        d.loc[entry, "take_profit"] = d.loc[entry, "bb_mid"]

        return d
