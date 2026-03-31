"""JP Stock VWAP Reversion — 任意銘柄対応版.

VWAPから乖離したら逆張りで平均回帰を狙う。
日計り信用（手数料0円）前提。14:30強制決済。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase, StrategyMeta


class JPVwapReversion(StrategyBase):

    def __init__(self, symbol: str, name: str,
                 dev_pct: float = 0.006,
                 exit_pct: float = 0.002,
                 stop_pct: float = 0.008,
                 rsi_period: int = 14,
                 rsi_max: int = 40,
                 avoid_slots: list[str] | None = None,
                 only_slots: list[str] | None = None) -> None:
        """
        avoid_slots: エントリーを禁止する時間帯スロット ["09:00","09:30"] など
        only_slots:  エントリーを許可する時間帯スロット（指定なし=全時間帯OK）
        """
        self.meta = StrategyMeta(
            id=f"jp_vwap_{symbol.replace('.', '_')}",
            name=f"VWAP {name}",
            symbol=symbol,
            interval="1m",
            description=f"VWAP乖離逆張り — {name} (日計り信用)",
            params={"dev_pct": dev_pct, "stop_pct": stop_pct},
        )
        self.dev_pct    = dev_pct
        self.exit_pct   = exit_pct
        self.stop_pct   = stop_pct
        self.rsi_period = rsi_period
        self.rsi_max    = rsi_max
        self.avoid_slots: set[str] = set(avoid_slots or [])
        self.only_slots:  set[str] = set(only_slots  or [])

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        d["signal"]      = 0
        d["stop_loss"]   = np.nan
        d["take_profit"] = np.nan
        d["date"]        = d.index.date

        # RSI
        delta = d["close"].diff()
        gain  = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss  = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rs    = gain / loss.replace(0, np.nan)
        d["rsi"] = 100 - 100 / (1 + rs)

        # セッションVWAP（日ごとに再計算）
        d["tp"] = (d["high"] + d["low"] + d["close"]) / 3
        vwaps = []
        for date, day_df in d.groupby("date"):
            cum_vol = day_df["volume"].cumsum()
            cum_tpv = (day_df["tp"] * day_df["volume"]).cumsum()
            vwap = cum_tpv / cum_vol.replace(0, np.nan)
            vwaps.append(vwap)
        d["vwap"] = pd.concat(vwaps).sort_index()

        d["vwap_dev"] = (d["close"] - d["vwap"]) / d["vwap"].replace(0, np.nan)

        # 時間帯フィルター
        def _slot(idx) -> str:
            h, m = idx.hour, idx.minute
            return f"{h:02d}:{0 if m < 30 else 30:02d}"

        time_ok = pd.Series(True, index=d.index)
        if self.avoid_slots:
            time_ok = time_ok & (~d.index.map(lambda i: _slot(i) in self.avoid_slots))
        if self.only_slots:
            time_ok = time_ok & (d.index.map(lambda i: _slot(i) in self.only_slots))

        # エントリー: VWAPから dev_pct 以上下落 + RSI過売り + 時間帯OK
        entry = (d["vwap_dev"] < -self.dev_pct) & (d["rsi"] < self.rsi_max) & time_ok
        # エグジット: VWAPに戻った
        exit_ = d["vwap_dev"] > -self.exit_pct

        # 14:30強制決済
        force = d.index.hour > 14 | ((d.index.hour == 14) & (d.index.minute >= 30))
        exit_ = exit_ | force

        d.loc[entry, "signal"] = 1
        d.loc[exit_,  "signal"] = -1
        d.loc[entry, "stop_loss"]   = d.loc[entry, "close"] * (1 - self.stop_pct)
        d.loc[entry, "take_profit"] = d.loc[entry, "vwap"]

        return d
