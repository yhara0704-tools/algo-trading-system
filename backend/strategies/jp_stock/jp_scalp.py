"""JP Stock High-Frequency Scalping — EMAクロス + VWAP微乗離.

手数料ゼロ（日計り信用）前提で超高頻度エントリーを狙う。

戦略ロジック:
  - EMA(fast) が EMA(slow) をクロスアップ
  - かつ close が VWAP を下から上に抜けていない（買われすぎ回避）
  - かつ直近の値幅（ATR）が最低限ある（閑散時間帯を弾く）
  - TP: +tp_pct (デフォルト 0.15%)
  - SL: -sl_pct (デフォルト 0.10%)

手数料あり (fee_pct > 0) の場合:
  - TP >= 2 × fee_pct × 2 (往復) が必要
  - 例: 手数料0.05%往復なら TP >= 0.20%

1,000円/日達成イメージ（手数料ゼロ前提）:
  50,000円ポジション × 0.15% 純利益 × 55%WR = 41円/取引
  24取引/日で ≈ 1,000円 (単銘柄)
  5銘柄並走なら 5取引/日×銘柄でOK
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.strategies.base import StrategyBase, StrategyMeta


class JPScalp(StrategyBase):
    """EMAクロス高頻度スキャルピング（日本株・日計り信用）。"""

    def __init__(
        self,
        symbol:      str,
        name:        str,
        ema_fast:    int   = 3,      # 短期EMA期間
        ema_slow:    int   = 5,      # 長期EMA期間
        tp_pct:      float = 0.0025, # テイクプロフィット 0.25%
        sl_pct:      float = 0.0015, # ストップロス 0.15%
        atr_period:  int   = 10,     # ATRフィルター期間
        atr_min_pct: float = 0.001,  # 最低ATR（閑散弾き）0.1%
        avoid_slots:     list[str] | None = None,
        interval:        str   = "5m",    # バックテスト用 (ライブは1m)
        morning_only:    bool  = True,    # True=前場のみ(9:10-11:30), False=全日(9:10-14:30)
        allow_short:     bool  = True,    # 空売りを許可するか（貸株料コスト試算済みでマイナスなら False）
    ) -> None:
        self.meta = StrategyMeta(
            id=f"jp_scalp_{symbol.replace('.', '_')}_{interval}",
            name=f"Scalp {name} [{interval}]",
            symbol=symbol,
            interval=interval,
            description=f"EMAクロス高頻度スキャル — {name} (手数料ゼロ前提)",
            params={
                "ema_fast":    ema_fast,
                "ema_slow":    ema_slow,
                "tp_pct":      tp_pct,
                "sl_pct":      sl_pct,
                "atr_min_pct": atr_min_pct,
            },
        )
        self.ema_fast    = ema_fast
        self.ema_slow    = ema_slow
        self.tp_pct      = tp_pct
        self.sl_pct      = sl_pct
        self.atr_period    = atr_period
        self.atr_min_pct   = atr_min_pct
        self.avoid_slots   = set(avoid_slots or [])
        self.morning_only  = morning_only
        self.allow_short   = allow_short

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        d["signal"]      = 0
        d["stop_loss"]   = np.nan
        d["take_profit"] = np.nan

        # ── インジケーター ────────────────────────────────────────────────────
        d["ema_fast"] = d["close"].ewm(span=self.ema_fast, adjust=False).mean()
        d["ema_slow"] = d["close"].ewm(span=self.ema_slow, adjust=False).mean()

        # EMAクロスアップ: 前足は fast <= slow、今足は fast > slow
        d["cross_up"] = (
            (d["ema_fast"] > d["ema_slow"])
            & (d["ema_fast"].shift(1) <= d["ema_slow"].shift(1))
        )

        # セッションVWAP（日ごとに累積平均を計算）
        d["date"] = d.index.date
        d["tp_val"] = (d["high"] + d["low"] + d["close"]) / 3
        d["tp_vol"] = d["tp_val"] * d["volume"]
        d["cum_tp_vol"] = d.groupby("date", group_keys=False)["tp_vol"].cumsum()
        d["cum_vol"] = d.groupby("date", group_keys=False)["volume"].cumsum()
        d["vwap"] = d["cum_tp_vol"] / d["cum_vol"].replace(0, np.nan)

        # VWAP乖離率: close が VWAP から何%離れているか
        d["vwap_dev"] = (d["close"] - d["vwap"]) / d["vwap"]

        # ATRフィルター（閑散時間帯・低ボラ弾き）
        d["atr"] = (d["high"] - d["low"]) / d["close"]
        d["atr_ma"] = d["atr"].rolling(self.atr_period).mean()
        d["has_vol"] = d["atr_ma"] >= self.atr_min_pct

        # ── 時間帯フィルター ──────────────────────────────────────────────────
        idx      = d.index
        time_min = pd.Series(idx.hour * 60 + idx.minute, index=d.index)

        # 前場のみ(9:10-11:30) or 全日(9:10-14:50)
        # Scalpは短時間決済なので大引け間際でもOK（他手法は14:30締切）
        if self.morning_only:
            d["in_session"] = (time_min >= 9 * 60 + 10) & (time_min <= 11 * 60 + 30)
            d["force_exit"] = time_min >= 11 * 60 + 30
        else:
            d["in_session"] = (time_min >= 9 * 60 + 10) & (time_min <= 14 * 60 + 50)
            d["force_exit"] = time_min >= 14 * 60 + 50

        # 強制決済マーク
        d.loc[d["force_exit"], "signal"] = -1

        # ── エントリー条件 ────────────────────────────────────────────────────
        # 基本: EMAクロスアップ + 閑散ではない + 時間帯OK
        # 過買いフィルター: VWAP より 0.5% 以上高い場合はエントリー回避
        entry_mask = (
            d["cross_up"]
            & d["has_vol"]
            & d["in_session"]
            & ~d["force_exit"]
            & (d["vwap_dev"] < 0.005)   # VWAPから+0.5%以内
        )

        if self.avoid_slots:
            time_str = pd.Series(idx.strftime("%H:%M"), index=d.index)
            entry_mask = entry_mask & ~time_str.isin(self.avoid_slots)

        d.loc[entry_mask, "signal"]      = 1
        # 逆指値狩り対策: SLをキリ番からわずかにずらす
        from backend.capital_tier import sl_anti_hunt_offset
        d.loc[entry_mask, "stop_loss"]   = d.loc[entry_mask, "close"].apply(
            lambda p: sl_anti_hunt_offset(p, self.sl_pct, "long")
        )
        d.loc[entry_mask, "take_profit"] = d.loc[entry_mask, "close"] * (1 + self.tp_pct)

        # ── EMAデッドクロス: 売りエントリー（空売り） ────────────────────────
        # allow_short=False の銘柄はスキップ（貸株料コスト負けする場合）
        if not self.allow_short:
            return d

        dead_cross = (
            (d["ema_fast"] < d["ema_slow"])
            & (d["ema_fast"].shift(1) >= d["ema_slow"].shift(1))
            & ~d["force_exit"]
        )
        short_mask = (
            dead_cross
            & d["has_vol"]
            & d["in_session"]
            & ~d["force_exit"]
            & (d["vwap_dev"] > -0.005)   # VWAPから-0.5%以内（売られすぎ直後は避ける）
        )
        if self.avoid_slots:
            time_str = pd.Series(idx.strftime("%H:%M"), index=d.index)
            short_mask = short_mask & ~time_str.isin(self.avoid_slots)

        # signal=-2: 売りエントリー（既存のシグナルがない箇所のみ）
        short_entry = short_mask & (d["signal"] == 0)
        d.loc[short_entry, "signal"]      = -2
        d.loc[short_entry, "stop_loss"]   = d.loc[short_entry, "close"].apply(
            lambda p: sl_anti_hunt_offset(p, self.sl_pct, "short")
        )
        d.loc[short_entry, "take_profit"] = d.loc[short_entry, "close"] * (1 - self.tp_pct)

        # 強制決済（force_exit）は -1 のまま（ロング・ショート共通クローズ）
        return d
