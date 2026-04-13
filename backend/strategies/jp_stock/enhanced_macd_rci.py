"""Enhanced MACD×RCI — ブースト条件（動的TP/SL + インジケーター連動）を組み込んだ改良版.

通常のMacdRciをベースに:
1. BB3σ到達でTP拡張（利を伸ばす）
2. RSI>70で利確シグナル（天井で逃げる）
3. VWAP割れで早期撤退シグナル
4. 同一銘柄の日中複数エントリー対応
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase, StrategyMeta


class EnhancedMacdRci(StrategyBase):
    def __init__(
        self,
        symbol: str,
        name: str = "",
        interval: str = "5m",
        # MACD params
        macd_fast: int = 3,
        macd_slow: int = 7,
        macd_signal: int = 9,
        # RCI params
        rci_periods: list[int] | None = None,
        rci_min_agree: int = 1,
        # TP/SL
        tp_pct: float = 0.009,    # デフォルトを広めに（OOS+1000超のパラメータ傾向から）
        sl_pct: float = 0.003,
        # ブースト設定
        bb_period: int = 30,
        bb_std: float = 3.0,      # low_volの最良設定
        rsi_period: int = 14,
        rsi_exit_high: float = 70,  # RSIがこれ以上なら利確シグナル
        vwap_stop: bool = True,     # VWAP割れで早期撤退
        allow_reentry: bool = True, # 日中複数エントリー
        # Pyramid
        max_pyramid: int = 1,  # デフォルト1回ピラミッド許可
    ):
        # ティアに応じたピラミッド回数を自動設定
        if max_pyramid < 0:
            # -1 = 自動（ティアから計算）
            max_pyramid = 1
        try:
            from backend.capital_tier import get_tier
            from backend.lab.runner import JP_CAPITAL_JPY
            tier = get_tier(JP_CAPITAL_JPY)
            auto_pyramid = tier.pyramid_max(symbol, 1500)  # 概算株価1500円で計算
            max_pyramid = min(max_pyramid, auto_pyramid) if auto_pyramid > 0 else max_pyramid
        except Exception:
            pass

        self.meta = StrategyMeta(
            id=f"enhanced_macd_rci_{symbol}_{interval}",
            name=f"E-MacdRci {name} [{interval}]",
            symbol=symbol,
            interval=interval,
            params={
                "macd_fast": macd_fast, "macd_slow": macd_slow,
                "macd_signal": macd_signal,
                "rci_min_agree": rci_min_agree,
                "tp_pct": tp_pct, "sl_pct": sl_pct,
                "bb_period": bb_period, "bb_std": bb_std,
                "rsi_period": rsi_period, "rsi_exit_high": rsi_exit_high,
                "vwap_stop": vwap_stop, "allow_reentry": allow_reentry,
            },
            max_pyramid=max_pyramid,
        )
        self._macd_fast = macd_fast
        self._macd_slow = macd_slow
        self._macd_signal = macd_signal
        self._rci_periods = rci_periods or [10, 12, 15]
        self._rci_min_agree = rci_min_agree
        self._tp_pct = tp_pct
        self._sl_pct = sl_pct
        self._bb_period = bb_period
        self._bb_std = bb_std
        self._rsi_period = rsi_period
        self._rsi_exit_high = rsi_exit_high
        self._vwap_stop = vwap_stop
        self._allow_reentry = allow_reentry

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # ── MACD ──
        ema_fast = close.ewm(span=self._macd_fast, adjust=False).mean()
        ema_slow = close.ewm(span=self._macd_slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=self._macd_signal, adjust=False).mean()
        histogram = macd_line - signal_line

        # ── RCI ──
        def _rci(series, period):
            result = pd.Series(np.nan, index=series.index)
            for i in range(period - 1, len(series)):
                window = series.iloc[i - period + 1: i + 1]
                price_rank = window.rank()
                time_rank = pd.Series(range(1, period + 1), index=window.index)
                d_sq = ((price_rank - time_rank) ** 2).sum()
                rci = (1 - 6 * d_sq / (period * (period ** 2 - 1))) * 100
                result.iloc[i] = rci
            return result

        rci_signals = []
        for p in self._rci_periods:
            rci = _rci(close, p)
            rci_up = (rci > rci.shift(1)) & (rci.shift(1) <= rci.shift(2))
            rci_signals.append(rci_up.astype(int))

        rci_agree = sum(rci_signals)

        # ── ボリンジャーバンド（ブースト用）──
        sma = close.rolling(self._bb_period).mean()
        std = close.rolling(self._bb_period).std()
        bb_upper = sma + self._bb_std * std
        bb_lower = sma - self._bb_std * std

        # ── RSI（エグジット用）──
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(self._rsi_period).mean()
        loss_val = (-delta.where(delta < 0, 0)).rolling(self._rsi_period).mean()
        rs = gain / loss_val.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))

        # ── VWAP（早期撤退用）──
        typical = (high + low + close) / 3
        cum_vol = volume.cumsum()
        cum_tp_vol = (typical * volume).cumsum()
        vwap = cum_tp_vol / cum_vol.replace(0, np.nan)

        # ── シグナル生成 ──
        signal = pd.Series(0, index=df.index)
        stop_loss = pd.Series(np.nan, index=df.index)
        take_profit = pd.Series(np.nan, index=df.index)
        lot_multiplier = pd.Series(1.0, index=df.index)

        in_position = False
        entry_price = 0.0
        bars_since_exit = 999  # 日中複数エントリー用

        for i in range(1, len(df)):
            # 場時間フィルター（9:05-11:30, 12:30-14:30）
            # 14:30以降は新規エントリー禁止（強制決済で利益減少を防ぐ）
            if hasattr(df.index[i], 'hour'):
                h, m = df.index[i].hour, df.index[i].minute
                t = h * 60 + m
                if not (545 <= t <= 690 or 750 <= t <= 870):
                    if in_position and t >= 870:
                        # 14:30以降は保有中でも利確シグナルを出す
                        signal.iloc[i] = -1
                        in_position = False
                        bars_since_exit = 0
                    continue

            if in_position:
                current_price = close.iloc[i]
                unrealized_pct = (current_price - entry_price) / entry_price

                # ── ピラミッド判定（含み益 + モメンタム継続）──
                # 含み益0.3%以上 + MACDヒストグラム拡大中 + まだピラミッドしていない
                if (unrealized_pct >= 0.002
                        and histogram.iloc[i] > histogram.iloc[i-1]
                        and histogram.iloc[i] > 0
                        and not np.isnan(vwap.iloc[i])
                        and current_price > vwap.iloc[i]):
                    signal.iloc[i] = 2  # ピラミッド追加買い
                    continue

                # ── 動的エグジット判定 ──
                should_exit = False

                # RSI > exit_high → 天井で利確
                if rsi.iloc[i] > self._rsi_exit_high:
                    should_exit = True

                # VWAP割れ → 早期撤退
                if self._vwap_stop and current_price < vwap.iloc[i]:
                    should_exit = True

                # BB上バンド到達 → 利確
                if not np.isnan(bb_upper.iloc[i]) and current_price >= bb_upper.iloc[i]:
                    should_exit = True

                # ヒストグラム反転 → 利確
                if histogram.iloc[i] < histogram.iloc[i-1] and histogram.iloc[i-1] > 0:
                    should_exit = True

                if should_exit:
                    signal.iloc[i] = -1
                    in_position = False
                    bars_since_exit = 0
                    continue
            else:
                bars_since_exit += 1

                # クールダウンなし — シグナル精度とレジーム判定に委ねる
                if not self._allow_reentry:
                    continue

                # エントリー条件: MACD + RCI
                macd_long = macd_line.iloc[i] > 0 and signal_line.iloc[i] > 0
                macd_short = macd_line.iloc[i] < 0 and signal_line.iloc[i] < 0
                rci_ok = rci_agree.iloc[i] >= self._rci_min_agree

                # ショートエントリー: MACD < 0 + RCI下向き + VWAP下
                if (macd_short and rci_ok
                        and not np.isnan(vwap.iloc[i])
                        and close.iloc[i] < vwap.iloc[i]):
                    signal.iloc[i] = -2  # ショート
                    entry_price = close.iloc[i]
                    stop_loss.iloc[i] = entry_price * (1 + self._sl_pct)
                    if not np.isnan(bb_lower.iloc[i]):
                        take_profit.iloc[i] = min(bb_lower.iloc[i], entry_price * (1 - self._tp_pct))
                    else:
                        take_profit.iloc[i] = entry_price * (1 - self._tp_pct)
                    lot_multiplier.iloc[i] = 1.0
                    in_position = True
                    continue

                if macd_long and rci_ok:
                    signal.iloc[i] = 1
                    entry_price = close.iloc[i]
                    stop_loss.iloc[i] = entry_price * (1 - self._sl_pct)

                    # 動的TP: BBバンドが近ければそこまで、遠ければ固定%
                    if not np.isnan(bb_upper.iloc[i]):
                        bb_tp = bb_upper.iloc[i]
                        fixed_tp = entry_price * (1 + self._tp_pct)
                        take_profit.iloc[i] = max(bb_tp, fixed_tp)
                    else:
                        take_profit.iloc[i] = entry_price * (1 + self._tp_pct)

                    # 動的ロット倍率（100株単位: 1.0=100株, 2.0=200株）
                    # ブースト条件一致時は200株（集中投資）
                    boost = 1.0
                    # BB下バンド近 + RSI<40 → 反発期待大 → 200株
                    if (not np.isnan(bb_lower.iloc[i])
                            and close.iloc[i] < bb_lower.iloc[i] * 1.01
                            and not np.isnan(rsi.iloc[i])
                            and rsi.iloc[i] < 40):
                        boost = 2.0
                    # 出来高急増 + VWAP上 → モメンタム確認 → 200株
                    elif (volume.iloc[i] > volume.rolling(20).mean().iloc[i] * 1.5
                            and not np.isnan(vwap.iloc[i])
                            and close.iloc[i] > vwap.iloc[i]):
                        boost = 2.0
                    lot_multiplier.iloc[i] = boost

                    in_position = True

        df["signal"] = signal
        df["stop_loss"] = stop_loss
        df["take_profit"] = take_profit
        df["lot_multiplier"] = lot_multiplier
        return df
