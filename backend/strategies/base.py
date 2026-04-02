"""Abstract strategy base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import pandas as pd


@dataclass
class StrategyMeta:
    id: str            # unique slug, e.g. "btc_ema_cross"
    name: str          # display name
    symbol: str        # e.g. "BTC-USD", "7203.T"
    interval: str      # "1m", "5m", "15m", "1h"
    description: str   = ""
    params: dict       = field(default_factory=dict)
    max_pyramid: int   = 0   # ピラミッドの最大追加回数 (0=無効, signal=2で追加買い)


class StrategyBase(ABC):
    meta: StrategyMeta

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Input:  OHLCV DataFrame with columns [open, high, low, close, volume]
                and DatetimeIndex.
        Output: same DataFrame with added columns:
                  signal   : 1 = enter long, -1 = exit long, 0 = hold
                  stop_loss: price level for stop loss (or NaN)
                  take_profit: price level for take profit (or NaN)
        """
        ...
