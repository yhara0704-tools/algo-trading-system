"""MACD×RCI 最適パラメータストア.

data/macd_rci_params.json を読み書きする。
weekly_optimize.py が更新し、runner.py が参照する。
"""
from __future__ import annotations

import json
import logging
import pathlib

logger = logging.getLogger(__name__)

_PATH = pathlib.Path(__file__).parent.parent.parent / "data" / "macd_rci_params.json"

_DEFAULT_5M = dict(tp_pct=0.003, sl_pct=0.001, rci_min_agree=1, macd_signal=9, max_pyramid=0)
_DEFAULT_1M = dict(tp_pct=0.002, sl_pct=0.001, rci_min_agree=1, macd_signal=9, max_pyramid=0)


def _load() -> dict:
    try:
        if _PATH.exists():
            return json.loads(_PATH.read_text())
    except Exception as e:
        logger.warning("macd_rci_params load failed: %s", e)
    return {}


def get_params_5m(symbol: str) -> dict:
    """5m足用のMACDRCIパラメータを返す。未登録ならデフォルト。"""
    data = _load()
    entry = data.get(symbol, {})
    if not entry:
        return dict(_DEFAULT_5M)
    return dict(
        tp_pct=entry.get("tp_pct", _DEFAULT_5M["tp_pct"]),
        sl_pct=entry.get("sl_pct", _DEFAULT_5M["sl_pct"]),
        rci_min_agree=entry.get("rci_min_agree", _DEFAULT_5M["rci_min_agree"]),
        macd_signal=entry.get("macd_signal", _DEFAULT_5M["macd_signal"]),
        max_pyramid=entry.get("max_pyramid", _DEFAULT_5M["max_pyramid"]),
    )


def get_params_1m(symbol: str) -> dict:
    """1m足用のMACDRCIパラメータを返す（5mと同じ銘柄特性を流用、slのみ少し緩める）。"""
    p = get_params_5m(symbol)
    # 1m足はノイズ多いのでピラミッドは無効・sl少し広め
    return dict(
        tp_pct=p["tp_pct"],
        sl_pct=min(p["sl_pct"] * 1.5, 0.003),
        rci_min_agree=p["rci_min_agree"],
        macd_signal=p["macd_signal"],
        max_pyramid=0,
    )


def is_robust(symbol: str) -> bool:
    """IS+OOS両方プラスのRobust判定済みかどうか。"""
    return _load().get(symbol, {}).get("robust", False)
