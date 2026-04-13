"""StrategyFactory — (手法名, 銘柄, パラメータ) → 戦略インスタンスを生成."""
from __future__ import annotations

from backend.strategies.jp_stock.jp_macd_rci import JPMacdRci
from backend.strategies.jp_stock.jp_breakout import JPBreakout
from backend.strategies.jp_stock.jp_scalp import JPScalp
from backend.strategies.jp_stock.jp_momentum_5min import JPMomentum5Min
from backend.strategies.jp_stock.jp_orb import JPOpeningRangeBreakout
from backend.strategies.jp_stock.jp_vwap import JPVwapReversion
from backend.strategies.jp_stock.enhanced_macd_rci import EnhancedMacdRci
from backend.strategies.jp_stock.enhanced_scalp import EnhancedScalp

# 全手法のデフォルトパラメータと許容範囲
STRATEGY_DEFAULTS = {
    "MacdRci": {
        "interval": "5m",
        "tp_pct": 0.003, "sl_pct": 0.001,
        "rci_min_agree": 1, "macd_signal": 9,
        "macd_fast": 3, "macd_slow": 7,
    },
    "Breakout": {
        "interval": "5m",
        "tp_pct": 0.005, "sl_pct": 0.003,
    },
    "Scalp": {
        "interval": "5m",
        "tp_pct": 0.002, "sl_pct": 0.001,
        "ema_fast": 5, "ema_slow": 13,
    },
    "Momentum5Min": {},
    "ORB": {},
    "VwapReversion": {
        "dev_pct": 0.003, "stop_pct": 0.005,
    },
}

# パラメータ範囲 (min, max, type)
PARAM_RANGES = {
    "MacdRci": {
        "tp_pct":        (0.001, 0.015, float),  # 利を伸ばす方向に拡張
        "sl_pct":        (0.0005, 0.005, float),
        "rci_min_agree": (1, 3, int),
        "macd_signal":   (5, 15, int),
        "macd_fast":     (2, 7, int),
        "macd_slow":     (5, 15, int),
    },
    "Scalp": {
        "tp_pct":   (0.001, 0.008, float),  # 拡張
        "sl_pct":   (0.0005, 0.003, float),
        "ema_fast": (2, 8, int),
        "ema_slow": (8, 20, int),
    },
    "Breakout": {
        "tp_pct": (0.003, 0.020, float),  # 拡張
        "sl_pct": (0.001, 0.008, float),
    },
    "ORB": {
        "tp_ratio": (1.0, 4.0, float),  # 拡張
        "sl_ratio": (0.5, 2.0, float),
    },
    "Momentum5Min": {
        "tp_pct": (0.002, 0.015, float),  # 拡張
        "sl_pct": (0.001, 0.005, float),
    },
    "VwapReversion": {
        "dev_pct":  (0.001, 0.010, float),  # 拡張
        "stop_pct": (0.002, 0.010, float),
    },
    "EnhancedScalp": {
        "tp_pct":   (0.002, 0.008, float),
        "sl_pct":   (0.001, 0.004, float),
        "ema_fast": (2, 8, int),
        "ema_slow": (8, 20, int),
        "bb_period": (15, 30, int),
        "bb_std":   (1.5, 3.0, float),
        "rsi_period": (7, 20, int),
        "rsi_exit_high": (65, 80, float),
    },
    "EnhancedMacdRci": {
        "tp_pct":        (0.005, 0.015, float),
        "sl_pct":        (0.001, 0.005, float),
        "rci_min_agree": (1, 3, int),
        "macd_signal":   (5, 15, int),
        "macd_fast":     (2, 7, int),
        "macd_slow":     (5, 15, int),
        "bb_period":     (15, 40, int),
        "bb_std":        (2.0, 3.5, float),
        "rsi_period":    (7, 20, int),
        "rsi_exit_high": (65, 80, float),
    },
}

STRATEGY_DEFAULTS["EnhancedScalp"] = {
    "interval": "5m",
    "tp_pct": 0.004, "sl_pct": 0.002,
    "ema_fast": 5, "ema_slow": 13,
    "bb_period": 20, "bb_std": 2.0,
    "rsi_period": 14, "rsi_exit_high": 70,
}

STRATEGY_DEFAULTS["EnhancedMacdRci"] = {
    "interval": "5m",
    "tp_pct": 0.009, "sl_pct": 0.003,
    "rci_min_agree": 1, "macd_signal": 9,
    "macd_fast": 2, "macd_slow": 10,
    "bb_period": 30, "bb_std": 3.0,
    "rsi_period": 14, "rsi_exit_high": 70,
}

ALL_STRATEGY_NAMES = list(STRATEGY_DEFAULTS.keys())


def create(strategy_name: str, symbol: str, name: str = "",
           params: dict | None = None, interval: str = "5m"):
    """手法名とパラメータから戦略インスタンスを生成する。"""
    if not name:
        name = symbol.replace(".T", "")
    p = {**STRATEGY_DEFAULTS.get(strategy_name, {}), **(params or {})}

    if strategy_name == "MacdRci":
        return JPMacdRci(
            symbol, name, interval=p.get("interval", interval),
            macd_fast=p.get("macd_fast", 3),
            macd_slow=p.get("macd_slow", 7),
            macd_signal=p.get("macd_signal", 9),
            rci_min_agree=p.get("rci_min_agree", 1),
            tp_pct=p.get("tp_pct", 0.003),
            sl_pct=p.get("sl_pct", 0.001),
        )
    elif strategy_name == "Breakout":
        return JPBreakout(
            symbol, name, interval=p.get("interval", interval),
            tp_pct=p.get("tp_pct", 0.005),
            sl_pct=p.get("sl_pct", 0.003),
        )
    elif strategy_name == "Scalp":
        return JPScalp(
            symbol, name, interval=p.get("interval", interval),
            ema_fast=p.get("ema_fast", 5),
            ema_slow=p.get("ema_slow", 13),
            tp_pct=p.get("tp_pct", 0.002),
            sl_pct=p.get("sl_pct", 0.001),
        )
    elif strategy_name == "Momentum5Min":
        return JPMomentum5Min(symbol, name)
    elif strategy_name == "ORB":
        return JPOpeningRangeBreakout(symbol, name)
    elif strategy_name == "VwapReversion":
        return JPVwapReversion(symbol, name)
    elif strategy_name == "EnhancedScalp":
        return EnhancedScalp(
            symbol, name, interval=p.get("interval", interval),
            ema_fast=p.get("ema_fast", 5),
            ema_slow=p.get("ema_slow", 13),
            tp_pct=p.get("tp_pct", 0.004),
            sl_pct=p.get("sl_pct", 0.002),
            bb_period=p.get("bb_period", 20),
            bb_std=p.get("bb_std", 2.0),
            rsi_period=p.get("rsi_period", 14),
            rsi_exit_high=p.get("rsi_exit_high", 70),
        )
    elif strategy_name == "EnhancedMacdRci":
        return EnhancedMacdRci(
            symbol, name, interval=p.get("interval", interval),
            macd_fast=p.get("macd_fast", 2),
            macd_slow=p.get("macd_slow", 10),
            macd_signal=p.get("macd_signal", 9),
            rci_min_agree=p.get("rci_min_agree", 1),
            tp_pct=p.get("tp_pct", 0.009),
            sl_pct=p.get("sl_pct", 0.003),
            bb_period=p.get("bb_period", 30),
            bb_std=p.get("bb_std", 3.0),
            rsi_period=p.get("rsi_period", 14),
            rsi_exit_high=p.get("rsi_exit_high", 70),
            allow_reentry=True,
        )
    else:
        raise ValueError(f"Unknown strategy: {strategy_name}")
