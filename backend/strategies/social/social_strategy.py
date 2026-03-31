"""
social/social_strategy.py — 抽出された手法をバックテスト可能な戦略クラスに変換。

ExtractedStrategyのparamsを読んで適切な基底戦略（ORB/VWAP/EMA等）を選び、
「○○手法」として命名した戦略インスタンスを返す。
"""
from __future__ import annotations

import logging
from typing import Any

from backend.strategies.base import StrategyBase
from backend.strategies.jp_stock.jp_orb import JPOpeningRangeBreakout
from backend.strategies.jp_stock.jp_vwap import JPVwapReversion
from backend.strategies.btc.ema_cross import BTCEmaCross
from backend.strategies.btc.vwap_reversion import BTCVwapReversion
from .extractor import ExtractedStrategy

logger = logging.getLogger(__name__)


def build_strategy(es: ExtractedStrategy) -> StrategyBase | None:
    """ExtractedStrategyからバックテスト可能な戦略インスタンスを生成する。"""
    params     = es.params or {}
    entry_type = params.get("entry_type", "breakout")
    market     = es.market
    avoid_slots    = params.get("avoid_slots", [])
    preferred_slots = params.get("preferred_slots", [])

    # JP株戦略
    if "JP" in market.upper() or market in ("JP_stock",):
        # 銘柄は実行時に差し込む — デフォルトはプレースホルダ
        symbol = params.get("symbol", "7203.T")
        name   = es.name

        if entry_type in ("breakout", "orb"):
            strat = JPOpeningRangeBreakout(
                symbol=symbol,
                name=name,
                range_minutes=int(params.get("range_minutes", 15)),
                tp_ratio=float(params.get("tp_ratio", 1.5)),
                sl_ratio=float(params.get("sl_ratio", 1.0)),
                avoid_opening_minutes=int(params.get("avoid_opening_minutes", 0)),
                avoid_slots=avoid_slots,
            )

        elif entry_type in ("reversal", "vwap_reversion"):
            strat = JPVwapReversion(
                symbol=symbol,
                name=name,
                dev_pct=float(params.get("dev_pct", 0.006)),
                stop_pct=float(params.get("sl_ratio", 1.0)) * 0.008,
                avoid_slots=avoid_slots,
                only_slots=preferred_slots or None,
            )

        else:
            # デフォルト: ORB
            strat = JPOpeningRangeBreakout(
                symbol=symbol, name=name,
                range_minutes=15, tp_ratio=1.5, sl_ratio=1.0,
            )

        strat.meta.id          = es.id
        strat.meta.name        = es.name
        strat.meta.description = es.description
        return strat

    # BTC戦略
    elif "BTC" in market.upper():
        if entry_type in ("reversal", "vwap_reversion"):
            strat = BTCVwapReversion(
                dev_pct=float(params.get("dev_pct", 0.005)),
                stop_pct=float(params.get("sl_ratio", 1.0)) * 0.005,
            )
        else:
            strat = BTCEmaCross(
                ema_fast=int(params.get("ema_fast", 9)),
                ema_slow=int(params.get("ema_slow", 21)),
            )

        strat.meta.id          = es.id
        strat.meta.name        = es.name
        strat.meta.description = es.description
        return strat

    else:
        logger.warning("Unknown market type: %s for %s", market, es.id)
        return None


def build_strategies_for_symbol(
    es: ExtractedStrategy,
    symbols: list[tuple[str, str]],  # [(symbol, name), ...]
) -> list[StrategyBase]:
    """
    銘柄リストに対してそれぞれの手法インスタンスを生成する。
    例: [("7203.T", "Toyota"), ("6758.T", "Sony")] × 二階堂手法
    """
    result = []
    params = es.params or {}
    avoid_slots     = params.get("avoid_slots", [])
    preferred_slots = params.get("preferred_slots", [])
    entry_type = params.get("entry_type", "breakout")

    for sym, nm in symbols:
        label = f"{es.name}[{nm}]"
        sid   = f"{es.id}_{sym.replace('.', '_')}"

        if entry_type in ("breakout", "orb"):
            strat = JPOpeningRangeBreakout(
                symbol=sym, name=label,
                range_minutes=int(params.get("range_minutes", 15)),
                tp_ratio=float(params.get("tp_ratio", 1.5)),
                sl_ratio=float(params.get("sl_ratio", 1.0)),
                avoid_opening_minutes=int(params.get("avoid_opening_minutes", 0)),
                avoid_slots=avoid_slots,
            )
        else:
            strat = JPVwapReversion(
                symbol=sym, name=label,
                dev_pct=float(params.get("dev_pct", 0.006)),
                stop_pct=float(params.get("sl_ratio", 1.0)) * 0.008,
                avoid_slots=avoid_slots,
                only_slots=preferred_slots or None,
            )

        strat.meta.id          = sid
        strat.meta.name        = label
        strat.meta.description = es.description
        result.append(strat)

    return result
