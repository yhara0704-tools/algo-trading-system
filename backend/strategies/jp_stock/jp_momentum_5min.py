"""JP Stock 5-Minute Momentum Strategy — 5分前比値上がりモメンタム.

エントリー条件:
  1. 5本前（25分前）比で mom_pct 以上の上昇
  2. 出来高が直近20本平均の vol_mult 倍以上（出来高急増）
  3. 時間帯フィルター: 9:30〜14:00 JST（寄り付き30分・引け前30分を回避）

決済:
  - テイクプロフィット: +tp_pct
  - ストップロス:      -sl_pct
  - 14:30 JST 強制決済（日計り信用前提）
  - モメンタム反転: close が5本前終値を下回ったら exit signal

1,000円/日の達成イメージ:
  50,000円ポジション × 1% TP × 2件 = 1,000円
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase, StrategyMeta


class JPMomentum5Min(StrategyBase):
    """5分足モメンタムスキャルピング戦略（日本株）。"""

    def __init__(
        self,
        symbol:       str,
        name:         str,
        mom_pct:      float = 0.008,   # 5分前比モメンタム閾値 (0.8%)
        sl_pct:       float = 0.005,   # ストップロス (0.5%)
        tp_pct:       float = 0.010,   # テイクプロフィット (1.0%)
        vol_mult:     float = 1.5,     # 出来高スパイク倍率
        lookback:     int   = 5,       # 何本前と比較（5本=25分）
        vol_ma:       int   = 20,      # 出来高移動平均期間
        avoid_slots:  list[str] | None = None,
    ) -> None:
        self.meta = StrategyMeta(
            id=f"jp_mom5m_{symbol.replace('.', '_')}",
            name=f"Momentum5m {name}",
            symbol=symbol,
            interval="5m",
            description=f"5分前比モメンタム — {name} (日計り信用)",
            params={
                "mom_pct": mom_pct,
                "sl_pct":  sl_pct,
                "tp_pct":  tp_pct,
                "vol_mult": vol_mult,
                "lookback": lookback,
            },
        )
        self.mom_pct     = mom_pct
        self.sl_pct      = sl_pct
        self.tp_pct      = tp_pct
        self.vol_mult    = vol_mult
        self.lookback    = lookback
        self.vol_ma      = vol_ma
        self.avoid_slots = set(avoid_slots or [])

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        d["signal"]      = 0
        d["stop_loss"]   = np.nan
        d["take_profit"] = np.nan

        # ── インジケーター計算 ────────────────────────────────────────────────
        # 5分前比モメンタム
        d["mom"] = d["close"] / d["close"].shift(self.lookback) - 1

        # 出来高移動平均
        d["vol_ma"] = d["volume"].rolling(self.vol_ma).mean()
        d["vol_spike"] = d["volume"] > d["vol_ma"] * self.vol_mult

        # 時間帯フィルター（JST） — すべて pandas Series で統一
        idx = d.index
        time_min = pd.Series(idx.hour * 60 + idx.minute, index=d.index)

        d["in_session"] = (time_min >= 9 * 60 + 30) & (time_min <= 14 * 60)
        d["force_exit"] = time_min >= 14 * 60 + 30

        # 14:30 以降は強制決済（exit signal）
        d.loc[d["force_exit"], "signal"] = -1

        # ── エントリーシグナル ────────────────────────────────────────────────
        entry_mask = (
            (d["mom"] >= self.mom_pct)   # 5分前比で閾値以上の上昇
            & d["vol_spike"]              # 出来高スパイクあり
            & d["in_session"]             # 許容時間帯
            & ~d["force_exit"]            # 強制決済時間外
        )

        # avoid_slots フィルター（時間帯スロット文字列 "09:30" 形式）
        if self.avoid_slots:
            time_str = pd.Series(
                idx.strftime("%H:%M"), index=d.index
            )
            entry_mask = entry_mask & ~time_str.isin(self.avoid_slots)

        d.loc[entry_mask, "signal"]      = 1
        d.loc[entry_mask, "stop_loss"]   = d.loc[entry_mask, "close"] * (1 - self.sl_pct)
        d.loc[entry_mask, "take_profit"] = d.loc[entry_mask, "close"] * (1 + self.tp_pct)

        # ── エグジットシグナル（モメンタム反転） ──────────────────────────────
        reversal = d["close"] < d["close"].shift(self.lookback)
        d.loc[reversal & ~d["force_exit"] & (d["signal"] == 0), "signal"] = -1

        return d
