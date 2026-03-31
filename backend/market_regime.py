"""地合い（マーケットレジーム）判定エンジン.

BTC/日本株の現在の地合いを分類し、最適な戦略タイプを推薦する。

レジーム分類:
  TRENDING_UP    — 上昇トレンド  → トレンドフォロー系が有効
  TRENDING_DOWN  — 下降トレンド  → ショート or 様子見
  RANGING        — レンジ相場    → 逆張り（RSI/BB/VWAP）が有効
  HIGH_VOL       — 高ボラ        → SL広め・ORBが有効
  LOW_VOL        — 低ボラ        → スキャル不向き・縮小or様子見
  UNKNOWN        — 判定不能      → 様子見

判定指標:
  ADX  > 25        → トレンド相場
  ADX  < 18        → レンジ相場
  ATR% > 過去平均×1.4  → 高ボラ
  ATR% < 過去平均×0.6  → 低ボラ
  EMA20 vs EMA50   → 方向性
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

Regime = Literal["trending_up", "trending_down", "ranging", "high_vol", "low_vol", "unknown"]

# 各レジームに推奨される戦略タイプ
REGIME_STRATEGY_MAP: dict[Regime, list[str]] = {
    "trending_up":   ["btc_ema_cross", "jp_orb"],       # クロス・ブレイクアウト
    "trending_down": [],                                  # 様子見（ロング戦略なし）
    "ranging":       ["btc_rsi_bb", "btc_vwap", "jp_vwap"],  # 逆張り
    "high_vol":      ["jp_orb", "btc_ema_cross"],        # ブレイクアウト系
    "low_vol":       ["btc_rsi_bb"],                     # 小動き逆張りのみ
    "unknown":       ["btc_rsi_bb"],                     # 保守的に
}

REGIME_JP: dict[Regime, str] = {
    "trending_up":   "📈 上昇トレンド",
    "trending_down": "📉 下降トレンド",
    "ranging":       "↔ レンジ相場",
    "high_vol":      "⚡ 高ボラ相場",
    "low_vol":       "😴 低ボラ相場",
    "unknown":       "❓ 判定中",
}


@dataclass
class RegimeResult:
    symbol:       str
    regime:       Regime
    regime_jp:    str
    adx:          float
    atr_pct:      float
    atr_vs_avg:   float    # 現在ATR / 過去平均ATR
    ema_trend:    str      # "up" / "down" / "flat"
    recommended:  list[str]
    description:  str
    ts:           float


class MarketRegimeDetector:
    def __init__(self) -> None:
        self._cache: dict[str, RegimeResult] = {}
        self._last_update: dict[str, float] = {}

    def get_all(self) -> dict[str, dict]:
        return {sym: self._to_dict(r) for sym, r in self._cache.items()}

    def get(self, symbol: str) -> dict | None:
        r = self._cache.get(symbol)
        return self._to_dict(r) if r else None

    async def update(self, symbol: str, df: pd.DataFrame) -> RegimeResult:
        """OHLCVデータから地合いを判定してキャッシュに保存。"""
        result = _detect(symbol, df)
        self._cache[symbol] = result
        self._last_update[symbol] = time.time()
        logger.info("Regime %s: %s (ADX=%.1f ATR=%.2f%% vs avg×%.2f)",
                    symbol, result.regime, result.adx,
                    result.atr_pct, result.atr_vs_avg)
        return result

    def _to_dict(self, r: RegimeResult) -> dict:
        return {
            "symbol":      r.symbol,
            "regime":      r.regime,
            "regime_jp":   r.regime_jp,
            "adx":         round(r.adx, 1),
            "atr_pct":     round(r.atr_pct, 3),
            "atr_vs_avg":  round(r.atr_vs_avg, 2),
            "ema_trend":   r.ema_trend,
            "recommended": r.recommended,
            "description": r.description,
            "ts":          r.ts,
        }


def _detect(symbol: str, df: pd.DataFrame) -> RegimeResult:
    """OHLCV DataFrameからレジームを判定。"""
    if len(df) < 50:
        return RegimeResult(symbol=symbol, regime="unknown",
                           regime_jp=REGIME_JP["unknown"],
                           adx=0, atr_pct=0, atr_vs_avg=1,
                           ema_trend="flat", recommended=["btc_rsi_bb"],
                           description="データ不足", ts=time.time())

    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    # ATR
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr14     = tr.rolling(14).mean()
    atr_now   = float(atr14.iloc[-1])
    atr_avg   = float(atr14.iloc[-60:-14].mean()) if len(atr14) > 74 else atr_now
    atr_pct   = atr_now / float(close.iloc[-1]) * 100
    atr_ratio = atr_now / atr_avg if atr_avg > 0 else 1.0

    # ADX
    adx = _calc_adx(high, low, close, period=14)

    # EMAトレンド
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    if ema20.iloc[-1] > ema50.iloc[-1] * 1.005:
        ema_trend = "up"
    elif ema20.iloc[-1] < ema50.iloc[-1] * 0.995:
        ema_trend = "down"
    else:
        ema_trend = "flat"

    # レジーム判定（優先順位順）
    if atr_ratio > 1.5:
        regime = "high_vol"
        desc   = f"通常の{atr_ratio:.1f}倍のボラ。ORB・ブレイクアウト系有利"
    elif atr_ratio < 0.6:
        regime = "low_vol"
        desc   = "ボラ低下中。スキャル縮小 or 様子見推奨"
    elif adx > 25:
        regime = "trending_up" if ema_trend == "up" else "trending_down"
        desc   = f"ADX={adx:.0f} 強トレンド相場({'上昇' if ema_trend=='up' else '下降'})"
    elif adx < 18:
        regime = "ranging"
        desc   = f"ADX={adx:.0f} レンジ相場。逆張り系が有効"
    else:
        regime = "ranging" if ema_trend == "flat" else (
            "trending_up" if ema_trend == "up" else "trending_down"
        )
        desc = f"中間的地合い (ADX={adx:.0f}, EMA={ema_trend})"

    return RegimeResult(
        symbol=symbol, regime=regime,
        regime_jp=REGIME_JP[regime],
        adx=adx, atr_pct=atr_pct, atr_vs_avg=atr_ratio,
        ema_trend=ema_trend,
        recommended=REGIME_STRATEGY_MAP.get(regime, []),
        description=desc,
        ts=time.time(),
    )


def _calc_adx(high: pd.Series, low: pd.Series, close: pd.Series,
              period: int = 14) -> float:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)

    dm_plus  = (high.diff()).clip(lower=0)
    dm_minus = (-low.diff()).clip(lower=0)
    dm_plus  = dm_plus.where(dm_plus > dm_minus, 0)
    dm_minus = dm_minus.where(dm_minus > dm_plus, 0)

    atr14    = tr.ewm(span=period, adjust=False).mean()
    di_plus  = 100 * dm_plus.ewm(span=period, adjust=False).mean()  / atr14.replace(0, np.nan)
    di_minus = 100 * dm_minus.ewm(span=period, adjust=False).mean() / atr14.replace(0, np.nan)

    dx  = (100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan))
    adx = dx.ewm(span=period, adjust=False).mean()
    return float(adx.iloc[-1]) if not adx.empty else 0.0
