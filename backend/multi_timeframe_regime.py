"""Multi-Timeframe Regime Alignment (MTFRA) — D9 Phase 2 実装.

D9 Phase 1.6 の発見に基づく実装:
  - 「3m + 30m + 60m」整合 = 実用最強解 (WR 54.2%, n=430)
  - 「1m + 3m + 15m + 60m」整合 = 高品質モード (WR 55.0%, n=109)
  - per-symbol 最適化: 6723.T, 6752.T などは MTFRA 無効

使い方:
    from backend.multi_timeframe_regime import MTFRADetector
    det = MTFRADetector(mode="default")  # or "aggressive" / "per_symbol" / "off"
    decision = det.evaluate(symbol="3103.T", df_1m=df_1m)
    if decision["allow_long"]:
        # ロング許可
    if decision["allow_short"]:
        # ショート許可

参照: docs/IMPLEMENTATION_LOG.md D9 Phase 1.5/1.6
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from backend.market_regime import _calc_adx, _detect

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
PER_SYMBOL_PATH = ROOT / "data/mtfra_optimal_per_symbol.json"

Direction = Literal["up", "down", "flat", "unknown"]
Mode = Literal["off", "default", "aggressive", "per_symbol"]

# D9 Phase 1.6 の Top 解 (各 mode のデフォルトフィルタ)
DEFAULT_TF_COMBO = ("3m", "30m", "60m")          # WR 54.2%, n=430
AGGRESSIVE_TF_COMBO = ("1m", "3m", "15m", "60m")  # WR 55.0%, n=109

# 全候補時間足 (resample rule)
TF_RULE: dict[str, str] = {
    "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min",
    "30m": "30min", "60m": "60min", "240m": "240min",
}
# 各時間足の最低必要バー数 (簡易判定 14 本、長時間足は緩和)
MIN_BARS: dict[str, int] = {
    "1m": 14, "3m": 14, "5m": 14, "15m": 14,
    "30m": 10, "60m": 5, "240m": 3,
}


@dataclass
class MTFRADecision:
    """MTFRA 判定結果."""
    symbol: str
    mode: str
    combo: tuple[str, ...]
    directions: dict[str, Direction]
    aligned_up: bool
    aligned_down: bool
    allow_long: bool
    allow_short: bool
    skip_reason: str | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "mode": self.mode,
            "combo": list(self.combo),
            "directions": dict(self.directions),
            "aligned_up": self.aligned_up,
            "aligned_down": self.aligned_down,
            "allow_long": self.allow_long,
            "allow_short": self.allow_short,
            "skip_reason": self.skip_reason,
            "detail": self.detail,
        }


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """OHLCV DataFrame を任意時間足にリサンプル."""
    if df.empty:
        return df
    agg = {
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }
    return df.resample(rule, label="right", closed="right").agg(agg).dropna()


def _detect_dir(df: pd.DataFrame) -> Direction:
    """簡易方向判定: up / down / flat / unknown.

    D9 で確認済の判定ロジック (analyze_mtfra_combination_search.py と同じ):
      - len < 14         → unknown
      - len >= 50        → market_regime._detect でフル判定
      - else             → 簡易判定 (EMA20 傾き + ADX)
    """
    if len(df) < 14:
        return "unknown"
    if len(df) >= 50:
        try:
            r = _detect("MTFRA", df).regime
            if r == "trending_up":
                return "up"
            if r == "trending_down":
                return "down"
            return "flat"
        except Exception:
            pass
    close = df["close"]
    high = df["high"]
    low = df["low"]
    ema20 = close.ewm(span=20, adjust=False).mean()
    if len(ema20) < 5:
        return "unknown"
    slope = (ema20.iloc[-1] - ema20.iloc[-5]) / max(1e-9, ema20.iloc[-5]) * 100
    adx = _calc_adx(high, low, close, period=min(14, len(df) - 1))
    if adx > 25:
        return "up" if slope > 0 else "down"
    if adx < 18:
        return "flat"
    if slope > 0.05:
        return "up"
    if slope < -0.05:
        return "down"
    return "flat"


class MTFRADetector:
    """マルチタイムフレーム レジーム整合判定器 (D9 Phase 2).

    Args:
        mode: "off" (フィルタ無効), "default" (3m+30m+60m),
              "aggressive" (1m+3m+15m+60m), "per_symbol" (銘柄別最適)
    """

    def __init__(self, mode: Mode = "default") -> None:
        self.mode = mode
        self._per_symbol: dict[str, dict] = {}
        if mode == "per_symbol":
            self._load_per_symbol()

    def _load_per_symbol(self) -> None:
        if not PER_SYMBOL_PATH.exists():
            logger.warning(
                "MTFRA per_symbol mode but %s not found. fallback to default",
                PER_SYMBOL_PATH,
            )
            return
        try:
            data = json.loads(PER_SYMBOL_PATH.read_text())
            self._per_symbol = data.get("symbols", {})
        except Exception as e:
            logger.error("MTFRA per_symbol load error: %s", e)

    def _resolve_combo(self, symbol: str) -> tuple[tuple[str, ...], str]:
        """銘柄ごとに使う時間足組み合わせを決定.

        Returns:
            (combo, mode_used)
        """
        if self.mode == "off":
            return (), "off"
        if self.mode == "default":
            return DEFAULT_TF_COMBO, "default"
        if self.mode == "aggressive":
            return AGGRESSIVE_TF_COMBO, "aggressive"
        # per_symbol: テーブルから引く、無ければ default フォールバック
        sym_cfg = self._per_symbol.get(symbol)
        if sym_cfg is None:
            return DEFAULT_TF_COMBO, "per_symbol_fallback_default"
        action = sym_cfg.get("action", "use")
        if action == "disable":
            return (), "per_symbol_disabled"
        combo = tuple(sym_cfg.get("combo", DEFAULT_TF_COMBO))
        return combo, "per_symbol"

    def evaluate(self, symbol: str, df_1m: pd.DataFrame) -> MTFRADecision:
        """エントリー前に呼ばれる主要メソッド.

        Args:
            symbol: "3103.T" など
            df_1m: 1m OHLCV (DatetimeIndex, columns: open/high/low/close/volume)

        Returns:
            MTFRADecision (allow_long / allow_short などの判断結果)
        """
        combo, mode_used = self._resolve_combo(symbol)
        if not combo:
            # off / per_symbol_disabled = 全方向許可 (フィルタ無効)
            return MTFRADecision(
                symbol=symbol, mode=mode_used, combo=(),
                directions={}, aligned_up=False, aligned_down=False,
                allow_long=True, allow_short=True,
                skip_reason=None, detail={"note": "MTFRA filter disabled"},
            )

        if df_1m is None or df_1m.empty or len(df_1m) < 60:
            # データ不足 → ガード保守的 (False, False)
            return MTFRADecision(
                symbol=symbol, mode=mode_used, combo=combo,
                directions={}, aligned_up=False, aligned_down=False,
                allow_long=False, allow_short=False,
                skip_reason="insufficient_data",
                detail={"n_bars_1m": int(len(df_1m)) if df_1m is not None else 0},
            )

        # 各時間足の direction を計算
        directions: dict[str, Direction] = {}
        for tf in combo:
            if tf == "1m":
                d_sub = df_1m.tail(60)
            else:
                d_sub = _resample(df_1m, TF_RULE[tf]).tail(60)
            if len(d_sub) < MIN_BARS.get(tf, 14):
                directions[tf] = "unknown"
            else:
                directions[tf] = _detect_dir(d_sub)

        if any(d == "unknown" for d in directions.values()):
            return MTFRADecision(
                symbol=symbol, mode=mode_used, combo=combo,
                directions=directions, aligned_up=False, aligned_down=False,
                allow_long=False, allow_short=False,
                skip_reason="mtfra_unknown",
                detail={"reason": "1 つ以上の時間足が unknown"},
            )

        ups = sum(1 for d in directions.values() if d == "up")
        downs = sum(1 for d in directions.values() if d == "down")
        n = len(directions)
        aligned_up = (ups == n)
        aligned_down = (downs == n)

        if aligned_up:
            return MTFRADecision(
                symbol=symbol, mode=mode_used, combo=combo,
                directions=directions, aligned_up=True, aligned_down=False,
                allow_long=True, allow_short=False, skip_reason=None,
                detail={"alignment": "all_up"},
            )
        if aligned_down:
            return MTFRADecision(
                symbol=symbol, mode=mode_used, combo=combo,
                directions=directions, aligned_up=False, aligned_down=True,
                allow_long=False, allow_short=True, skip_reason=None,
                detail={"alignment": "all_down"},
            )
        # 不整合: D9 Phase 1 で「partial 整合は逆効果」と判明済
        return MTFRADecision(
            symbol=symbol, mode=mode_used, combo=combo,
            directions=directions, aligned_up=False, aligned_down=False,
            allow_long=False, allow_short=False,
            skip_reason="mtfra_misaligned",
            detail={"ups": ups, "downs": downs, "n": n},
        )


# 簡便関数 (フィルタチェックを 1 行で)
def mtfra_allow(symbol: str, df_1m: pd.DataFrame, side: str,
                mode: Mode = "default") -> tuple[bool, str | None]:
    """エントリーが許可されるかを判定して bool + 理由を返す.

    Args:
        symbol: "3103.T"
        df_1m: 1m OHLCV
        side: "long" or "short"
        mode: "off" / "default" / "aggressive" / "per_symbol"

    Returns:
        (allow, skip_reason) — allow=True ならエントリー可、False なら skip_reason
    """
    det = MTFRADetector(mode=mode)
    decision = det.evaluate(symbol, df_1m)
    if side == "long":
        return decision.allow_long, decision.skip_reason
    return decision.allow_short, decision.skip_reason


__all__ = [
    "MTFRADetector", "MTFRADecision", "mtfra_allow",
    "DEFAULT_TF_COMBO", "AGGRESSIVE_TF_COMBO",
]
