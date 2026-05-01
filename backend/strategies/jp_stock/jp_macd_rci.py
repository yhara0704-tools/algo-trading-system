"""JP Stock MACD × RCI Strategy — ユーザー手法の実装.

エントリー（ロング）:
  - MACD fast EMA > 0 かつ slow EMA > 0
  - 両EMAが前バーより上向き（スロープ陽転）
  - RCI 条件は ``rci_entry_mode`` で切替:
      0 = RCI(各期間) のうち ``rci_min_agree`` 本以上が上向き（従来の多数決）
      1 = 最短期RCI が最長期RCI を下から上に抜けた（ゴールデンクロス）**翌足**でエントリー
      2 = 上記ゴールデンクロスが成立した**同一足**でエントリー（比較検証用）
  - 最短RCI の傾き ``rci_short_slope``（ポイント/バー）と ``rci_gc_slope_arctan_deg`` を DataFrame に常時出力。
    ``rci_gc_slope_enabled`` かつ mode 1/2 のとき、エントリー足の傾きを ``rci_gc_slope_min``〜``max`` で任意フィルタ。
  - 寄り付きギャップで戦略切り替え（順張り/寄り天）

事故防止フィルタ（Phase F7、すべて opt-in / デフォルト OFF。
3103.T / 6613.T の OOS 実トレード解析で再現性確認済み）:
  - F1 ``disable_lunch_session_entry`` (0/1): 11:30〜13:00 のエントリー禁止
    （3103.T では同帯 sum -84,907 JPY と最大の損失帯）
  - F2 ``require_macd_above_signal`` (0/1): MACD>シグナル を必須化
    （False 帯は両銘柄で一貫して負け越し）
  - F3 ``rci_danger_zone_enabled`` (0/1) + ``rci_danger_low`` / ``rci_danger_high``:
    エントリー足の最短 RCI が `[low, high]` の禁止帯にあれば発注しない。
    銘柄ごとに「危険ゾーン」が異なる: 3103 は `low=-100, high=-80`（極オーバーソールド）、
    6613 は `low=-80, high=-40`（中途半端な売られ）。
  - F4 ``volume_surge_max_ratio`` (>0 で有効): 直前 5 本平均出来高に対し
    ``volume_surge_max_ratio`` 倍を超える出来高サージのバーではエントリー不可。
    （3103.T では 2〜4 倍帯で sum -62,200 JPY のフェイク多発）

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
        # 0=RCI多数決 / 1=最短×最長GCの翌足 / 2=最短×最長GCの当足
        rci_entry_mode: int = 0,
        # 最短RCI の傾き（検証・記録用 + GC 時オプションフィルタ）
        rci_gc_slope_lookback: int = 3,
        rci_gc_slope_enabled: int = 0,
        rci_gc_slope_min: float = -999.0,
        rci_gc_slope_max: float = 999.0,
        tp_pct:       float = 0.003,  # 利確 0.3%
        sl_pct:       float = 0.002,  # 損切 0.2%
        gap_thresh_pct: float = 0.005,  # ギャップ判定閾値 0.5%
        max_pyramid:  int   = 0,     # ピラミッド最大追加回数（0=無効）
        # 手法内部PDCA用: 入口/出口のロジックバリアント
        # entry_profile: 0=turning(従来), 1=continuation, 2=hybrid
        # exit_profile : 0=hist_weak(従来), 1=hist_delay, 2=rci_confirm
        entry_profile: int = 0,
        exit_profile: int = 0,
        hist_exit_delay_bars: int = 1,
        rci_exit_min_agree: int = 2,
        # Phase F7 事故防止フィルタ（すべて opt-in / デフォルト OFF）
        disable_lunch_session_entry: int = 0,
        require_macd_above_signal: int = 0,
        rci_danger_zone_enabled: int = 0,
        rci_danger_low: float = -80.0,
        rci_danger_high: float = 80.0,
        volume_surge_max_ratio: float = 0.0,
        volume_surge_lookback: int = 5,
        # F8 (2026-05-01): 寄付直後の短時間 short ブロック。
        # 5/1 paper で 9:39-9:43 の連続 stop -4,200 JPY (損失 70%) が発生し、
        # 4/30 にも類似パターンあり再現性高い。寄付直後はボラ高で short の SL/TP が
        # 不利に動きやすい (gap up 後の反発で逆行) ため、9:00〜09:30 の short のみ
        # 禁止する。long は据え置き (寄付き直後の上昇順張りは温存)。
        morning_first_30min_short_block: int = 0,
        morning_block_until_min: int = 30,
        # F9 (2026-05-01): 後場終盤の long エントリーを禁止する。
        # D6a 60日 5m 解析で 3103.T (long -187,496 円), 6723.T (-70,339 円),
        # 9468.T (-13,423 円) など 14:00 以降の long が大幅敗北パターンが多発。
        # 大引け前の利益確定売り → 連れ安 → SL hit / EOD 強制決済 で損失が膨らむ。
        # 14:00 以降の long entry を打ち切り、short / 既存ポジは据え置く。
        afternoon_late_long_block: int = 0,
        afternoon_late_block_from_min: int = 14 * 60,  # 14:00 JST
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
                "entry_profile": entry_profile,
                "exit_profile": exit_profile,
                "hist_exit_delay_bars": hist_exit_delay_bars,
                "rci_exit_min_agree": rci_exit_min_agree,
                "rci_entry_mode": rci_entry_mode,
                "rci_gc_slope_lookback": rci_gc_slope_lookback,
                "rci_gc_slope_enabled": rci_gc_slope_enabled,
                "rci_gc_slope_min": rci_gc_slope_min,
                "rci_gc_slope_max": rci_gc_slope_max,
                "disable_lunch_session_entry": disable_lunch_session_entry,
                "require_macd_above_signal": require_macd_above_signal,
                "rci_danger_zone_enabled": rci_danger_zone_enabled,
                "rci_danger_low": rci_danger_low,
                "rci_danger_high": rci_danger_high,
                "volume_surge_max_ratio": volume_surge_max_ratio,
                "volume_surge_lookback": volume_surge_lookback,
                "morning_first_30min_short_block": morning_first_30min_short_block,
                "morning_block_until_min": morning_block_until_min,
                "afternoon_late_long_block": afternoon_late_long_block,
                "afternoon_late_block_from_min": afternoon_late_block_from_min,
            },
            max_pyramid=max_pyramid,
        )
        self.macd_fast     = macd_fast
        self.macd_slow     = macd_slow
        self.macd_signal   = macd_signal
        self.rci_periods   = rci_periods
        self.rci_min_agree = rci_min_agree
        self.rci_entry_mode = int(rci_entry_mode)
        self.rci_gc_slope_lookback = max(1, int(rci_gc_slope_lookback))
        self.rci_gc_slope_enabled = bool(int(rci_gc_slope_enabled))
        self.rci_gc_slope_min = float(rci_gc_slope_min)
        self.rci_gc_slope_max = float(rci_gc_slope_max)
        if self.rci_gc_slope_min > self.rci_gc_slope_max:
            self.rci_gc_slope_min, self.rci_gc_slope_max = self.rci_gc_slope_max, self.rci_gc_slope_min
        self.tp_pct        = tp_pct
        self.sl_pct        = sl_pct
        self.gap_thresh_pct = gap_thresh_pct
        self.entry_profile = int(entry_profile)
        self.exit_profile = int(exit_profile)
        self.hist_exit_delay_bars = max(1, int(hist_exit_delay_bars))
        self.rci_exit_min_agree = max(1, int(rci_exit_min_agree))
        # Phase F7 事故防止フィルタ
        self.disable_lunch_session_entry = bool(int(disable_lunch_session_entry))
        self.require_macd_above_signal = bool(int(require_macd_above_signal))
        self.rci_danger_zone_enabled = bool(int(rci_danger_zone_enabled))
        self.rci_danger_low = float(rci_danger_low)
        self.rci_danger_high = float(rci_danger_high)
        if self.rci_danger_low > self.rci_danger_high:
            self.rci_danger_low, self.rci_danger_high = self.rci_danger_high, self.rci_danger_low
        self.volume_surge_max_ratio = float(volume_surge_max_ratio)
        self.volume_surge_lookback = max(1, int(volume_surge_lookback))
        self.morning_first_30min_short_block = bool(int(morning_first_30min_short_block))
        self.morning_block_until_min = max(0, int(morning_block_until_min))
        self.afternoon_late_long_block = bool(int(afternoon_late_long_block))
        self.afternoon_late_block_from_min = max(0, int(afternoon_late_block_from_min))

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

        # ── RCI エントリー軸: 多数決 vs 最短×最長ゴールデン/デッドクロス ─────────────
        p_short = min(self.rci_periods)
        p_long = max(self.rci_periods)
        col_s = f"rci_{p_short}"
        col_l = f"rci_{p_long}"
        rci_gc = (d[col_s] > d[col_l]) & (d[col_s].shift(1) <= d[col_l].shift(1))
        rci_dc = (d[col_s] < d[col_l]) & (d[col_s].shift(1) >= d[col_l].shift(1))
        d["rci_gc_bar"] = rci_gc.astype(np.int8)
        d["rci_dc_bar"] = rci_dc.astype(np.int8)

        # 最短RCI の傾き: (現在 - k本前) / k （RCI ポイント/バー）。観測・後分析用に常に列を出す。
        k_sl = self.rci_gc_slope_lookback
        rci_short_slope = (d[col_s] - d[col_s].shift(k_sl)) / float(k_sl)
        d["rci_short_slope"] = rci_short_slope
        # arctan(傾き) を度にした可視化用スカラー（横軸1バーに対する立ち上がり角のイメージ）
        _clipped = rci_short_slope.clip(-1e6, 1e6)
        d["rci_gc_slope_arctan_deg"] = np.degrees(np.arctan(_clipped))
        # GC/DC が成立したバーでの傾き（イベント分析用。非イベントは NaN）
        d["rci_slope_at_gc_bar"] = rci_short_slope.where(rci_gc)
        d["rci_slope_at_dc_bar"] = rci_short_slope.where(rci_dc)

        if self.rci_entry_mode == 1:
            # クロス成立の翌足でエントリー
            rci_long_filter = rci_gc.shift(1).fillna(False).astype(bool)
            rci_short_filter = rci_dc.shift(1).fillna(False).astype(bool)
        elif self.rci_entry_mode == 2:
            # クロス成立と同じ足でエントリー
            rci_long_filter = rci_gc.fillna(False).astype(bool)
            rci_short_filter = rci_dc.fillna(False).astype(bool)
        else:
            rci_long_filter = rci_bullish
            rci_short_filter = rci_bearish
        if len(self.rci_periods) < 2 and self.rci_entry_mode in (1, 2):
            rci_long_filter = rci_bullish
            rci_short_filter = rci_bearish

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

        # ── エントリー（profile別） ────────────────────────────────────────────
        cont_long = macd_above0 & signal_above0 & macd_rising & signal_rising & (histogram > 0)
        cont_short = macd_below0 & signal_below0 & macd_fall & signal_fall & (histogram < 0)
        if self.entry_profile == 1:  # continuation
            long_entry = cont_long & rci_long_filter & in_session
            short_entry = cont_short & rci_short_filter & in_session
        elif self.entry_profile == 2:  # hybrid
            long_entry = (macd_long | cont_long) & rci_long_filter & in_session
            short_entry = (macd_short | cont_short) & rci_short_filter & in_session
        else:  # turning (従来)
            long_entry = macd_long & rci_long_filter & in_session
            short_entry = macd_short & rci_short_filter & in_session

        # GC/DC モード時のみ: エントリー足の最短RCI傾きが帯域内（任意）
        if (
            self.rci_gc_slope_enabled
            and self.rci_entry_mode in (1, 2)
            and len(self.rci_periods) >= 2
        ):
            slope_ok = (rci_short_slope >= self.rci_gc_slope_min) & (
                rci_short_slope <= self.rci_gc_slope_max
            )
            long_entry = long_entry & slope_ok
            short_entry = short_entry & slope_ok

        # ── Phase F7 事故防止フィルタ ─────────────────────────────────────────
        # F1: 11:30〜13:00 のエントリー禁止（昼休み挟み罠）
        if self.disable_lunch_session_entry:
            lunch_zone = (time_min >= 11 * 60 + 30) & (time_min < 13 * 60)
            long_entry = long_entry & ~lunch_zone
            short_entry = short_entry & ~lunch_zone

        # F2: MACD > Signal を必須化（False 帯は両銘柄で一貫負け）
        if self.require_macd_above_signal:
            macd_above_sig_now = macd_line > signal_line
            long_entry = long_entry & macd_above_sig_now
            short_entry = short_entry & ~macd_above_sig_now

        # F3: 最短 RCI が禁止帯にあればエントリー不可
        if self.rci_danger_zone_enabled and len(self.rci_periods) >= 1:
            short_rci = d[col_s]
            danger = (short_rci >= self.rci_danger_low) & (short_rci <= self.rci_danger_high)
            long_entry = long_entry & ~danger
            short_entry = short_entry & ~danger

        # F4: 出来高サージのバーではエントリー不可（フェイク回避）
        if self.volume_surge_max_ratio > 0:
            vol = d["volume"].astype(float)
            vol_avg = vol.shift(1).rolling(self.volume_surge_lookback).mean()
            vol_ratio = vol / vol_avg.replace(0, np.nan)
            surge = vol_ratio >= self.volume_surge_max_ratio
            surge = surge.fillna(False)
            long_entry = long_entry & ~surge
            short_entry = short_entry & ~surge

        # F8: 寄付直後 (9:00〜9:00+morning_block_until_min) の short エントリー禁止
        # 5/1 paper で 9:39-9:43 連続 stop -4,200 JPY、4/30 にも類似損失あり。
        # 寄付き直後はボラが高く short の TP/SL が不利、long の機会のみ温存する。
        if self.morning_first_30min_short_block and self.morning_block_until_min > 0:
            morning_block_zone = (time_min >= 9 * 60) & (
                time_min < 9 * 60 + self.morning_block_until_min
            )
            short_entry = short_entry & ~morning_block_zone

        # F9: 後場終盤 (14:00 以降) の long エントリー禁止
        # D6a 60日 5m 解析で 3103.T (-187,496 円) / 6723.T (-70,339 円) /
        # 9468.T (-13,423 円) など終盤 long 大敗パターンを構造的に避ける。
        # short / EOD 強制決済は据え置き。
        if self.afternoon_late_long_block and self.afternoon_late_block_from_min > 0:
            late_block_zone = time_min >= self.afternoon_late_block_from_min
            long_entry = long_entry & ~late_block_zone

        # ── エグジット（profile別） ────────────────────────────────────────────
        weaken_long = hist_weakening_long
        weaken_short = hist_weakening_short
        if self.hist_exit_delay_bars > 1:
            weaken_long = hist_weakening_long.rolling(self.hist_exit_delay_bars).sum() >= self.hist_exit_delay_bars
            weaken_short = hist_weakening_short.rolling(self.hist_exit_delay_bars).sum() >= self.hist_exit_delay_bars

        if self.exit_profile == 1:  # hist_delay
            long_exit = weaken_long
            short_exit = weaken_short
        elif self.exit_profile == 2:  # rci_confirm
            long_exit = weaken_long & (rci_down_count >= self.rci_exit_min_agree)
            short_exit = weaken_short & (rci_up_count >= self.rci_exit_min_agree)
        else:  # hist_weak (従来)
            long_exit = hist_weakening_long
            short_exit = hist_weakening_short

        d.loc[long_exit & in_exit_session, "signal"] = -1
        d.loc[short_exit & in_exit_session, "signal"] = 1
        # 前場→後場の持ち越しは許容（日跨ぎはエンジンの eod_close_time で防止）

        # ── ピラミッディング (signal=2): ヒストグラム加速中に追加買い ─────────────
        # エンジン側でポジション保有中のみ有効（保有なし時は無視される）
        # 条件: ヒストグラム拡大 & 両線ゼロ以上 & RCI強気 & セッション内
        hist_growing  = histogram > histogram.shift(1)
        if len(self.rci_periods) >= 2 and self.rci_entry_mode in (1, 2):
            rci_pyramid_ok = d[col_s] > d[col_l]
        else:
            rci_pyramid_ok = rci_bullish
        pyramid_long  = hist_growing & macd_above0 & signal_above0 & rci_pyramid_ok & in_session
        # signal=0 のバーのみに適用（エグジット/エントリーを上書きしない）
        d.loc[pyramid_long & (d["signal"] == 0), "signal"] = 2

        # ── エントリーシグナルを設定（エグジット・ピラミッドより後で上書き）
        d.loc[long_entry, "signal"]      = 1
        d.loc[long_entry, "stop_loss"]   = d.loc[long_entry, "close"] * (1 - self.sl_pct)
        d.loc[long_entry, "take_profit"] = d.loc[long_entry, "close"] * (1 + self.tp_pct)

        # ショートは signal=-2（engine: 高値が SL 以上で損切・安値が TP 以下で利確）
        d.loc[short_entry, "signal"] = -2
        d.loc[short_entry, "stop_loss"] = d.loc[short_entry, "close"] * (1 + self.sl_pct)
        d.loc[short_entry, "take_profit"] = d.loc[short_entry, "close"] * (1 - self.tp_pct)

        return d
