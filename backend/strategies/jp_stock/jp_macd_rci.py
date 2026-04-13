"""JP Stock MACD × RCI Strategy — ユーザー手法の実装.

エントリー（ロング）:
  - MACD fast EMA > 0 かつ slow EMA > 0
  - 両EMAが前バーより上向き（スロープ陽転）
  - RCI(10/12/15) のうち過半数が上向き（角度 > 0）
  - 寄り付きギャップで戦略切り替え（順張り/寄り天）

エグジット:
  - MACDヒストグラムが前バーより小さくなった（勢い鈍化）
  - 15:25 EOD（engine 側で制御）

ショート:
  - ロングの逆条件

セッション: 前場9:05〜11:30 / 後場12:30〜15:25
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase, StrategyMeta


def _rci(series: pd.Series, period: int) -> pd.Series:
    """Rank Correlation Index（スピアマンの順位相関ベース）を計算する。
    値域: -100〜+100。+100 = 完全上昇トレンド、-100 = 完全下降トレンド。
    """
    n = period

    def _calc(window) -> float:
        if len(window) < n:
            return np.nan
        prices = np.array(window)
        # 時間ランク: 古い方が小さい (1〜n)
        time_rank = np.arange(1, n + 1)
        # 価格ランク: 高い価格ほど小さいランク (1=最高値)
        price_rank = n + 1 - pd.Series(prices).rank(ascending=False).values
        d2 = (time_rank - price_rank) ** 2
        rci_val = (1 - 6 * d2.sum() / (n * (n ** 2 - 1))) * 100
        return float(rci_val)

    return series.rolling(n).apply(_calc, raw=True)


class JPMacdRci(StrategyBase):
    """MACD(fast/slow/signal) × RCI(3本) 複合戦略。"""

    def __init__(
        self,
        symbol:       str,
        name:         str,
        macd_fast:    int   = 3,
        macd_slow:    int   = 7,
        macd_signal:  int   = 9,
        rci_periods:  list[int] | None = None,  # デフォルト [10, 12, 15]
        rci_min_agree: int  = 2,   # RCI上向き本数の下限（3本中2本以上）
        tp_pct:       float = 0.003,  # 利確 0.3%
        sl_pct:       float = 0.002,  # 損切 0.2%
        gap_thresh_pct: float = 0.005,  # ギャップ判定閾値 0.5%
        max_pyramid:  int   = 0,     # ピラミッド最大追加回数（0=無効）
        interval:     str   = "5m",
    ) -> None:
        rci_periods = rci_periods or [10, 12, 15]
        self.meta = StrategyMeta(
            id=f"jp_macd_rci_{symbol.replace('.', '_')}_{interval}",
            name=f"MACD×RCI {name} [{interval}]",
            symbol=symbol,
            interval=interval,
            description=f"MACD({macd_fast},{macd_slow},{macd_signal}) × RCI({rci_periods}) — {name}",
            params={
                "macd_fast": macd_fast, "macd_slow": macd_slow,
                "macd_signal": macd_signal, "rci_periods": rci_periods,
                "tp_pct": tp_pct, "sl_pct": sl_pct,
            },
            max_pyramid=max_pyramid,
        )
        self.macd_fast     = macd_fast
        self.macd_slow     = macd_slow
        self.macd_signal   = macd_signal
        self.rci_periods   = rci_periods
        self.rci_min_agree = rci_min_agree
        self.tp_pct        = tp_pct
        self.sl_pct        = sl_pct
        self.gap_thresh_pct = gap_thresh_pct

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        d["signal"]      = 0
        d["stop_loss"]   = np.nan
        d["take_profit"] = np.nan

        close = d["close"]

        # ── MACD ─────────────────────────────────────────────────────────────
        fast_ema = close.ewm(span=self.macd_fast, adjust=False).mean()
        slow_ema = close.ewm(span=self.macd_slow, adjust=False).mean()
        macd_line   = fast_ema - slow_ema          # MACDライン（短期-長期）
        signal_line = macd_line.ewm(span=self.macd_signal, adjust=False).mean()
        histogram   = macd_line - signal_line      # ヒストグラム（勢いバー）

        d["macd"]      = macd_line
        d["macd_sig"]  = signal_line
        d["macd_hist"] = histogram
        d["fast_ema"]  = fast_ema
        d["slow_ema"]  = slow_ema

        # ── RCI ──────────────────────────────────────────────────────────────
        rci_cols = []
        for p in self.rci_periods:
            col = f"rci_{p}"
            d[col] = _rci(close, p)
            rci_cols.append(col)

        # ── 派生シグナル ──────────────────────────────────────────────────────
        # MACD条件:「短期線」= MACDライン、「長期線」= シグナルライン
        macd_above0  = macd_line   > 0
        signal_above0= signal_line > 0
        macd_rising  = macd_line   > macd_line.shift(1)
        signal_rising= signal_line > signal_line.shift(1)
        # 「上向きになってきた」= 前バーは下向きで今バーが上向きに転換した瞬間
        macd_turning_up   = macd_rising  & ~(macd_line.shift(1)   > macd_line.shift(2))
        signal_turning_up = signal_rising & ~(signal_line.shift(1) > signal_line.shift(2))
        macd_long    = macd_above0 & signal_above0 & (macd_turning_up | signal_turning_up)

        macd_below0   = macd_line   < 0
        signal_below0 = signal_line < 0
        macd_fall     = macd_line   < macd_line.shift(1)
        signal_fall   = signal_line < signal_line.shift(1)
        macd_turning_down   = macd_fall  & ~(macd_line.shift(1)   < macd_line.shift(2))
        signal_turning_down = signal_fall & ~(signal_line.shift(1) < signal_line.shift(2))
        macd_short    = macd_below0 & signal_below0 & (macd_turning_down | signal_turning_down)

        # RCI角度（前バーとの差分、上向き=正）
        rci_up_count   = sum((d[c] > d[c].shift(1)).astype(int) for c in rci_cols)
        rci_down_count = sum((d[c] < d[c].shift(1)).astype(int) for c in rci_cols)
        rci_bullish = rci_up_count   >= self.rci_min_agree
        rci_bearish = rci_down_count >= self.rci_min_agree

        # ヒストグラム勢い鈍化（エグジット判定用）
        hist_weakening_long  = histogram < histogram.shift(1)   # 上昇勢い鈍化
        hist_weakening_short = histogram > histogram.shift(1)   # 下降勢い鈍化

        # ── セッションフィルター ──────────────────────────────────────────────
        idx      = d.index
        time_min = pd.Series(idx.hour * 60 + idx.minute, index=d.index)
        # エントリー可能ゾーン
        # 前場: 9:05〜11:25、後場: 12:30〜14:30
        # 14:30以降は新規エントリー禁止（強制決済で利益減少を防ぐ）
        am_entry = (time_min >= 9 * 60 + 5) & (time_min <= 11 * 60 + 25)
        pm_entry = (time_min >= 12 * 60 + 30) & (time_min <= 14 * 60 + 30)
        in_session = am_entry | pm_entry
        # エグジット判定ゾーン（エントリーより広め）
        am_exit = (time_min >= 9 * 60 + 5) & (time_min <= 11 * 60 + 30)
        pm_exit = (time_min >= 12 * 60 + 30) & (time_min <= 15 * 60 + 25)
        in_exit_session = am_exit | pm_exit

        # ── 寄り付きギャップ判定 ─────────────────────────────────────────────
        # 前日終値 vs 本日始値のギャップ（日次）
        prev_close = close.resample("D").last().shift(1)
        # インデックスをバー単位に展開
        daily_prev_close = prev_close.reindex(d.index, method="ffill")
        today_open = d["open"].resample("D").first()
        daily_open = today_open.reindex(d.index, method="ffill")
        gap_pct    = (daily_open - daily_prev_close) / daily_prev_close.replace(0, np.nan)

        gap_up   = gap_pct >= self.gap_thresh_pct    # ギャップアップ → 順張り有利
        gap_down = gap_pct <= -self.gap_thresh_pct   # ギャップダウン

        # ── エントリー ────────────────────────────────────────────────────────
        # ロング: MACD条件 & RCI上向き & セッション内
        long_entry = macd_long & rci_bullish & in_session
        # ショート: MACD条件 & RCI下向き & セッション内
        short_entry = macd_short & rci_bearish & in_session

        # ── エグジット ────────────────────────────────────────────────────────
        d.loc[hist_weakening_long  & in_exit_session, "signal"] = -1
        d.loc[hist_weakening_short & in_exit_session, "signal"] =  1
        # 前場→後場の持ち越しは許容（日跨ぎはエンジンの eod_close_time で防止）

        # ── ピラミッディング (signal=2): ヒストグラム加速中に追加買い ─────────────
        # エンジン側でポジション保有中のみ有効（保有なし時は無視される）
        # 条件: ヒストグラム拡大 & 両線ゼロ以上 & RCI強気 & セッション内
        hist_growing  = histogram > histogram.shift(1)
        pyramid_long  = hist_growing & macd_above0 & signal_above0 & rci_bullish & in_session
        # signal=0 のバーのみに適用（エグジット/エントリーを上書きしない）
        d.loc[pyramid_long & (d["signal"] == 0), "signal"] = 2

        # ── エントリーシグナルを設定（エグジット・ピラミッドより後で上書き）
        d.loc[long_entry, "signal"]      = 1
        d.loc[long_entry, "stop_loss"]   = d.loc[long_entry, "close"] * (1 - self.sl_pct)
        d.loc[long_entry, "take_profit"] = d.loc[long_entry, "close"] * (1 + self.tp_pct)

        # ショートは signal=-2 で区別（engine がサポートする場合のみ）
        # 現状エンジンはロングのみのため、ショートは将来拡張用にコメントアウト
        # d.loc[short_entry, "signal"] = -2

        return d
