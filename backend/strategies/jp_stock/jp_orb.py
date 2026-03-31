"""JP Stock Opening Range Breakout — 任意銘柄対応版.

日計り信用（松井証券）: 手数料0円
寄り付き15分のレンジをブレイクしたら順張りエントリー。
avoid_opening_minutes: 寄り付き直後の高ボラ帯を避けるオプション（0=即エントリー可）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase, StrategyMeta


class JPOpeningRangeBreakout(StrategyBase):

    def __init__(self, symbol: str, name: str,
                 range_minutes: int = 15,
                 tp_ratio: float = 1.5,
                 sl_ratio: float = 1.0,
                 force_exit_hour: int = 14,
                 force_exit_minute: int = 30,
                 avoid_opening_minutes: int = 0,
                 avoid_slots: list[str] | None = None) -> None:
        """
        avoid_opening_minutes: レンジ確定後さらに何分待ってからエントリーを許可するか
                               (0=即OK, 5=5分待つ など。寄り付き高ボラを避けるため)
        avoid_slots: 特定時間帯スロットを避ける ["09:00","09:30" など]
        """
        self.meta = StrategyMeta(
            id=f"jp_orb_{symbol.replace('.', '_')}",
            name=f"ORB {name}",
            symbol=symbol,
            interval="1m",
            description=f"Opening range breakout — {name} (日計り信用)",
            params={"range_minutes": range_minutes, "tp_ratio": tp_ratio,
                    "sl_ratio": sl_ratio, "avoid_opening_min": avoid_opening_minutes},
        )
        self.range_minutes           = range_minutes
        self.tp_ratio                = tp_ratio
        self.sl_ratio                = sl_ratio
        self.force_exit_hour         = force_exit_hour
        self.force_exit_minute       = force_exit_minute
        self.avoid_opening_minutes   = avoid_opening_minutes
        self.avoid_slots: set[str]   = set(avoid_slots or [])

    def _in_avoid_slot(self, hour: int, minute: int) -> bool:
        slot = f"{hour:02d}:{0 if minute < 30 else 30:02d}"
        return slot in self.avoid_slots

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        d["signal"]      = 0
        d["stop_loss"]   = np.nan
        d["take_profit"] = np.nan
        d["date"]        = d.index.date

        for date, day_df in d.groupby("date"):
            opening = day_df.iloc[:self.range_minutes]
            if len(opening) < self.range_minutes:
                continue

            range_high = opening["high"].max()
            range_low  = opening["low"].min()
            range_size = range_high - range_low
            if range_size <= 0:
                continue

            tp_price = range_high + range_size * self.tp_ratio
            sl_price = range_low  - range_size * (self.sl_ratio - 1.0)

            # avoid_opening_minutes 分だけ追加待機
            skip_extra = self.avoid_opening_minutes
            post = day_df.iloc[self.range_minutes + skip_extra:]

            for idx, row in post.iterrows():
                h = idx.hour   if hasattr(idx, "hour")   else 15
                m = idx.minute if hasattr(idx, "minute") else 0
                # 強制決済時刻チェック
                if h > self.force_exit_hour or (h == self.force_exit_hour and m >= self.force_exit_minute):
                    d.loc[idx, "signal"] = -1
                    break
                # 避けるスロットはスキップ
                if self._in_avoid_slot(h, m):
                    continue
                if row["close"] > range_high:
                    d.loc[idx, "signal"]      = 1
                    d.loc[idx, "stop_loss"]   = sl_price
                    d.loc[idx, "take_profit"] = tp_price
                    break

        return d
