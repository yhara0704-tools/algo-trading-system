"""相場類似期間マッチャー — 直近の相場特性に似た過去期間を自動検索してバックテストに使う.

フロー:
  1. 直近N日の相場フィンガープリントを計算
  2. 過去2年の日足データから最も似た期間をTOP-K抽出
  3. 該当期間の5m足でバックテストを実行
  4. 相場が変わるたびに自動で期間が切り替わる

使い方:
    matcher = RegimeMatcher()
    periods = await matcher.find_similar_periods("7203.T", lookback_days=504)
    for p in periods:
        df_5m = await fetch_ohlcv("7203.T", "5m", ...)
        # df_5m をその期間にスライスしてバックテスト
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


@dataclass
class MarketFingerprint:
    """相場の特性を数値化したベクトル。"""
    trend_5d:      float   # 5日リターン%
    trend_20d:     float   # 20日リターン%
    volatility:    float   # ATR/価格 の平均
    vol_ratio:     float   # 直近5日出来高 / 20日平均
    momentum:      float   # RSI(14)相当
    regime:        str     # "uptrend"|"downtrend"|"sideways"|"volatile"

    def to_vector(self) -> np.ndarray:
        """コサイン類似度計算用ベクトル（正規化済み）。"""
        # regime をone-hot
        regimes = ["uptrend", "downtrend", "sideways", "volatile"]
        regime_vec = [1.0 if self.regime == r else 0.0 for r in regimes]
        v = np.array([
            self.trend_5d   / 10,    # スケール調整
            self.trend_20d  / 20,
            self.volatility * 100,
            self.vol_ratio  - 1.0,
            (self.momentum  - 50) / 50,
        ] + regime_vec)
        return v


@dataclass
class SimilarPeriod:
    """類似相場期間の情報。"""
    start_date:  str
    end_date:    str
    similarity:  float     # 0〜1（高いほど類似）
    fingerprint: MarketFingerprint
    description: str       # 人間が読める説明


def _compute_rsi(close: pd.Series, period: int = 14) -> float:
    """簡易RSI計算。"""
    delta = close.diff().dropna()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - 100 / (1 + rs)
    return float(rsi.iloc[-1]) if not rsi.empty else 50.0


def _compute_fingerprint(df: pd.DataFrame) -> MarketFingerprint | None:
    """DataFrameから相場フィンガープリントを計算する。"""
    try:
        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        volume = df["volume"]

        n = len(close)
        if n < 25:
            return None

        trend_5d  = (close.iloc[-1] / close.iloc[-6] - 1) * 100  if n >= 6  else 0.0
        trend_20d = (close.iloc[-1] / close.iloc[-21] - 1) * 100 if n >= 21 else trend_5d

        atr       = ((high - low) / close).rolling(10).mean().iloc[-1]
        vol_ratio = (volume.iloc[-5:].mean() / volume.iloc[-20:].mean()
                     if n >= 20 else 1.0)
        rsi       = _compute_rsi(close)

        # レジーム判定
        if abs(trend_20d) < 3 and atr < 0.01:
            regime = "sideways"
        elif atr > 0.025:
            regime = "volatile"
        elif trend_20d > 3:
            regime = "uptrend"
        else:
            regime = "downtrend"

        return MarketFingerprint(
            trend_5d   = round(trend_5d, 3),
            trend_20d  = round(trend_20d, 3),
            volatility = round(float(atr), 5),
            vol_ratio  = round(float(vol_ratio), 3),
            momentum   = round(rsi, 2),
            regime     = regime,
        )
    except Exception as e:
        logger.debug("fingerprint計算失敗: %s", e)
        return None


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


class RegimeMatcher:
    """直近相場に類似した過去期間を自動検索する。"""

    def __init__(
        self,
        window_days: int = 20,    # フィンガープリント計算に使う日数
        top_k:       int = 3,     # 返す類似期間の数
        min_gap_days: int = 30,   # 類似期間どうしの最小間隔（重複防止）
    ):
        self.window_days  = window_days
        self.top_k        = top_k
        self.min_gap_days = min_gap_days
        self._fp_cache: dict[str, tuple[MarketFingerprint, datetime]] = {}

    async def get_current_fingerprint(self, symbol: str) -> MarketFingerprint | None:
        """直近の相場フィンガープリントを取得（キャッシュ1時間）。"""
        now = datetime.now(JST)
        cached = self._fp_cache.get(symbol)
        if cached:
            fp, ts = cached
            if (now - ts).total_seconds() < 3600:
                return fp

        df = await self._fetch_daily(symbol, days=60)
        if df is None or df.empty:
            return None

        fp = _compute_fingerprint(df.tail(self.window_days))
        if fp:
            self._fp_cache[symbol] = (fp, now)
        return fp

    async def find_similar_periods(
        self, symbol: str, lookback_days: int = 504
    ) -> list[SimilarPeriod]:
        """過去lookback_days日間から類似相場期間TOP-Kを返す。"""
        df_hist = await self._fetch_daily(symbol, days=lookback_days)
        if df_hist is None or df_hist.empty:
            return []

        current_fp = _compute_fingerprint(df_hist.tail(self.window_days))
        if current_fp is None:
            return []

        current_vec = current_fp.to_vector()

        # 全ウィンドウでフィンガープリントを計算して類似度をスコアリング
        candidates: list[tuple[float, int, MarketFingerprint]] = []
        step = max(1, self.window_days // 4)   # オーバーラップさせながらスライド

        for end_idx in range(self.window_days * 2, len(df_hist) - self.window_days, step):
            window_df = df_hist.iloc[end_idx - self.window_days: end_idx]
            fp = _compute_fingerprint(window_df)
            if fp is None:
                continue
            sim = _cosine_similarity(current_vec, fp.to_vector())
            candidates.append((sim, end_idx, fp))

        # 類似度降順でソートし、重複期間を除去
        candidates.sort(key=lambda x: x[0], reverse=True)
        selected: list[SimilarPeriod] = []
        used_indices: list[int] = []

        for sim, end_idx, fp in candidates:
            # 既選択期間と重なりすぎないか確認
            too_close = any(
                abs(end_idx - ui) < self.min_gap_days
                for ui in used_indices
            )
            if too_close:
                continue

            start_idx = end_idx - self.window_days
            start_date = str(df_hist.index[start_idx])[:10]
            end_date   = str(df_hist.index[end_idx - 1])[:10]

            desc = (
                f"{fp.regime} / 20日リターン{fp.trend_20d:+.1f}% "
                f"/ ボラ{fp.volatility*100:.2f}% / RSI{fp.momentum:.0f}"
            )
            selected.append(SimilarPeriod(
                start_date  = start_date,
                end_date    = end_date,
                similarity  = round(sim, 4),
                fingerprint = fp,
                description = desc,
            ))
            used_indices.append(end_idx)

            if len(selected) >= self.top_k:
                break

        return selected

    async def _fetch_daily(self, symbol: str, days: int) -> pd.DataFrame | None:
        """日足データを取得する（yfinance経由）。"""
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, lambda: self._yf_daily(symbol, days)),
                timeout=20,
            )
        except Exception as e:
            logger.debug("daily fetch失敗 %s: %s", symbol, e)
            return None

    @staticmethod
    def _yf_daily(symbol: str, days: int) -> pd.DataFrame | None:
        import yfinance as yf
        import warnings; warnings.filterwarnings("ignore")
        period = f"{min(days // 30 + 1, 24)}mo"
        df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
        if df is None or df.empty:
            return None
        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index).tz_convert("Asia/Tokyo")
        return df[["open", "high", "low", "close", "volume"]].tail(days)


# ── グローバルシングルトン ────────────────────────────────────────────────
_matcher: RegimeMatcher | None = None

def get_matcher() -> RegimeMatcher:
    global _matcher
    if _matcher is None:
        _matcher = RegimeMatcher()
    return _matcher
