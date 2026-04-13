"""Enhanced Scalp — ブースト条件（BB/RSI動的エグジット + 日中複数エントリー + ピラミッド）.

通常のScalp(EMAクロス)をベースに:
1. RSI/BBベースの動的TP（利を伸ばす）
2. VWAP割れで早期撤退
3. 日中複数エントリー（クールダウン3バー=15分）
4. 含み益でピラミッド
5. 出来高急増時にロット2倍
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase, StrategyMeta


class EnhancedScalp(StrategyBase):
    def __init__(
        self,
        symbol: str,
        name: str = "",
        interval: str = "5m",
        ema_fast: int = 5,
        ema_slow: int = 13,
        tp_pct: float = 0.004,
        sl_pct: float = 0.002,
        bb_period: int = 20,
        bb_std: float = 2.0,
        rsi_period: int = 14,
        rsi_exit_high: float = 70,
        max_pyramid: int = 1,
    ):
        try:
            from backend.capital_tier import get_tier
            from backend.lab.runner import JP_CAPITAL_JPY
            tier = get_tier(JP_CAPITAL_JPY)
            auto_pyr = tier.pyramid_max(symbol, 1500)
            max_pyramid = min(max_pyramid, auto_pyr) if auto_pyr > 0 else max_pyramid
        except Exception:
            pass

        self.meta = StrategyMeta(
            id=f"enhanced_scalp_{symbol}_{interval}",
            name=f"E-Scalp {name} [{interval}]",
            symbol=symbol,
            interval=interval,
            params={
                "ema_fast": ema_fast, "ema_slow": ema_slow,
                "tp_pct": tp_pct, "sl_pct": sl_pct,
                "bb_period": bb_period, "bb_std": bb_std,
                "rsi_period": rsi_period, "rsi_exit_high": rsi_exit_high,
            },
            max_pyramid=max_pyramid,
        )
        self._ema_fast = ema_fast
        self._ema_slow = ema_slow
        self._tp_pct = tp_pct
        self._sl_pct = sl_pct
        self._bb_period = bb_period
        self._bb_std = bb_std
        self._rsi_period = rsi_period
        self._rsi_exit_high = rsi_exit_high

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # EMA
        ema_f = close.ewm(span=self._ema_fast, adjust=False).mean()
        ema_s = close.ewm(span=self._ema_slow, adjust=False).mean()

        # BB
        sma = close.rolling(self._bb_period).mean()
        std = close.rolling(self._bb_period).std()
        bb_upper = sma + self._bb_std * std
        bb_lower = sma - self._bb_std * std

        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(self._rsi_period).mean()
        loss_val = (-delta.where(delta < 0, 0)).rolling(self._rsi_period).mean()
        rs = gain / loss_val.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))

        # VWAP
        typical = (high + low + close) / 3
        cum_vol = volume.cumsum()
        cum_tp_vol = (typical * volume).cumsum()
        vwap = cum_tp_vol / cum_vol.replace(0, np.nan)

        # ATR
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        atr_pct = atr / close * 100

        # 出来高平均
        vol_avg = volume.rolling(20).mean()

        signal = pd.Series(0, index=df.index)
        stop_loss = pd.Series(np.nan, index=df.index)
        take_profit = pd.Series(np.nan, index=df.index)
        lot_multiplier = pd.Series(1.0, index=df.index)

        in_position = False
        entry_price = 0.0
        bars_since_exit = 999

        for i in range(1, len(df)):
            if hasattr(df.index[i], 'hour'):
                h, m = df.index[i].hour, df.index[i].minute
                t = h * 60 + m
                # Scalpは14:50まで許可
                if not (550 <= t <= 690 or 750 <= t <= 890):
                    if in_position and t >= 890:
                        signal.iloc[i] = -1
                        in_position = False
                        bars_since_exit = 0
                    continue

            if in_position:
                current = close.iloc[i]
                unrealized_pct = (current - entry_price) / entry_price

                # ピラミッド: 含み益0.15%以上 + EMAクロス維持 + VWAP上
                if (unrealized_pct >= 0.0015
                        and ema_f.iloc[i] > ema_s.iloc[i]
                        and not np.isnan(vwap.iloc[i])
                        and current > vwap.iloc[i]):
                    signal.iloc[i] = 2
                    continue

                # 動的エグジット
                should_exit = False
                if rsi.iloc[i] > self._rsi_exit_high:
                    should_exit = True
                if not np.isnan(bb_upper.iloc[i]) and current >= bb_upper.iloc[i]:
                    should_exit = True
                if not np.isnan(vwap.iloc[i]) and current < vwap.iloc[i] and unrealized_pct < 0:
                    should_exit = True
                # EMAクロスダウン
                if ema_f.iloc[i] < ema_s.iloc[i] and ema_f.iloc[i-1] >= ema_s.iloc[i-1]:
                    should_exit = True

                if should_exit:
                    signal.iloc[i] = -1
                    in_position = False
                    bars_since_exit = 0
                    continue
            else:
                bars_since_exit += 1
                # クールダウンなし — シグナル精度とレジーム判定に委ねる

                # エントリー判定
                ema_cross_up = ema_f.iloc[i] > ema_s.iloc[i] and ema_f.iloc[i-1] <= ema_s.iloc[i-1]
                ema_cross_down = ema_f.iloc[i] < ema_s.iloc[i] and ema_f.iloc[i-1] >= ema_s.iloc[i-1]
                atr_ok = not np.isnan(atr_pct.iloc[i]) and atr_pct.iloc[i] >= 0.05

                # ショートエントリー: EMAクロスダウン + VWAP下
                if (ema_cross_down and atr_ok
                        and not np.isnan(vwap.iloc[i])
                        and close.iloc[i] < vwap.iloc[i]):
                    signal.iloc[i] = -2
                    entry_price = close.iloc[i]
                    stop_loss.iloc[i] = entry_price * (1 + self._sl_pct)
                    if not np.isnan(bb_lower.iloc[i]):
                        take_profit.iloc[i] = min(bb_lower.iloc[i], entry_price * (1 - self._tp_pct))
                    else:
                        take_profit.iloc[i] = entry_price * (1 - self._tp_pct)
                    lot_multiplier.iloc[i] = 1.0
                    in_position = True
                    continue

                # ロングエントリー: EMAクロスアップ + VWAP近辺
                not_overextended = True
                if not np.isnan(vwap.iloc[i]):
                    dev = (close.iloc[i] - vwap.iloc[i]) / vwap.iloc[i]
                    not_overextended = dev < 0.005

                if ema_cross_up and atr_ok and not_overextended:
                    signal.iloc[i] = 1
                    entry_price = close.iloc[i]
                    stop_loss.iloc[i] = entry_price * (1 - self._sl_pct)
                    if not np.isnan(bb_upper.iloc[i]):
                        take_profit.iloc[i] = max(bb_upper.iloc[i], entry_price * (1 + self._tp_pct))
                    else:
                        take_profit.iloc[i] = entry_price * (1 + self._tp_pct)
                    if (not np.isnan(vol_avg.iloc[i]) and vol_avg.iloc[i] > 0
                            and volume.iloc[i] > vol_avg.iloc[i] * 1.5):
                        lot_multiplier.iloc[i] = 2.0
                    in_position = True

        df["signal"] = signal
        df["stop_loss"] = stop_loss
        df["take_profit"] = take_profit
        df["lot_multiplier"] = lot_multiplier
        return df
