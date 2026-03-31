"""JP Stock Breakout Strategy — ちくわ氏スタイル.

直近N本の高値ブレイクアウト + 安値切り上げ確認でエントリー。
「高値越えたら買い、安値も切り上げているのでトレンド確認」型。

前場集中・素早い利確・微損撤退がコンセプト。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase, StrategyMeta


class JPBreakout(StrategyBase):
    """ブレイクアウト戦略（ちくわ氏スタイル）。"""

    def __init__(
        self,
        symbol:       str,
        name:         str,
        lookback:     int   = 10,    # 直近何本の高値を見るか（5分足で50分）
        tp_pct:       float = 0.0020, # 利確 0.20%（素早く取る）
        sl_pct:       float = 0.0015, # 損切 0.15%（微損撤退）
        trend_bars:   int   = 3,     # 安値切り上げ確認期間
        avoid_slots:  list[str] | None = None,
        interval:     str   = "5m",
    ) -> None:
        self.meta = StrategyMeta(
            id=f"jp_breakout_{symbol.replace('.', '_')}_{interval}",
            name=f"Breakout {name} [{interval}]",
            symbol=symbol,
            interval=interval,
            description=f"ブレイクアウト戦略 — {name} (ちくわ式前場特化)",
            params={"lookback": lookback, "tp_pct": tp_pct, "sl_pct": sl_pct},
        )
        self.lookback    = lookback
        self.tp_pct      = tp_pct
        self.sl_pct      = sl_pct
        self.trend_bars  = trend_bars
        self.avoid_slots = set(avoid_slots or [])

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        d["signal"]      = 0
        d["stop_loss"]   = np.nan
        d["take_profit"] = np.nan

        # ── インジケーター ────────────────────────────────────────────────────
        # 直近N本の高値・安値
        d["recent_high"] = d["high"].shift(1).rolling(self.lookback).max()
        d["recent_low"]  = d["low"].shift(1).rolling(self.lookback).min()

        # ブレイクアウト: 今の終値が直近高値を上回った
        d["breakout"] = d["close"] > d["recent_high"]

        # 安値切り上げ確認: 直近trend_bars本の安値が上昇トレンド
        d["low_rising"] = d["low"] > d["low"].shift(self.trend_bars)

        # 出来高増加: 直近平均より多い（機関の参加を確認）
        d["vol_ma"]      = d["volume"].rolling(20).mean()
        d["vol_confirm"] = d["volume"] > d["vol_ma"] * 1.3

        # ── 時間帯フィルター（前場のみ: 9:10〜11:30） ────────────────────────
        idx      = d.index
        time_min = pd.Series(idx.hour * 60 + idx.minute, index=d.index)
        d["in_session"] = (time_min >= 9 * 60 + 10) & (time_min <= 11 * 60 + 30)
        d["force_exit"] = time_min >= 11 * 60 + 30
        d.loc[d["force_exit"], "signal"] = -1

        # ── エントリー ────────────────────────────────────────────────────────
        entry_mask = (
            d["breakout"]
            & d["low_rising"]
            & d["vol_confirm"]
            & d["in_session"]
            & ~d["force_exit"]
        )
        if self.avoid_slots:
            time_str = pd.Series(idx.strftime("%H:%M"), index=d.index)
            entry_mask = entry_mask & ~time_str.isin(self.avoid_slots)

        d.loc[entry_mask, "signal"]      = 1
        d.loc[entry_mask, "stop_loss"]   = d.loc[entry_mask, "close"] * (1 - self.sl_pct)
        d.loc[entry_mask, "take_profit"] = d.loc[entry_mask, "close"] * (1 + self.tp_pct)

        # ── エグジット: 高値ブレイクの失敗（close が recent_high を下回る） ──
        reversal = (d["close"] < d["recent_high"]) & ~d["force_exit"]
        d.loc[reversal & (d["signal"] == 0), "signal"] = -1

        return d
