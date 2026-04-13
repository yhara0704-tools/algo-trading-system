"""毎朝のペーパートレード準備 — デーモンの学習結果から最適手法を選定.

朝8:50に実行し、以下を行う:
1. 全監視銘柄のレジーム検出
2. experiments DBからレジーム適性のある安定Robust上位を選定
3. 余力制約でポートフォリオを構築
4. jp_live_runner にセット
5. 当日の戦略選定理由をログに記録（大引け後の振り返り用）

使い方:
    # main.py の startup に組み込み済み
    # または手動実行:
    from backend.lab.daily_prep import prepare_daily_strategies
    strategies, report = await prepare_daily_strategies()
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

from backend.backtesting.strategy_factory import create as create_strategy
from backend.lab.runner import fetch_ohlcv
from backend.market_regime import _detect as detect_regime
from backend.storage.db import get_robust_experiments

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))

MAX_STRATEGIES_PER_SYMBOL = 2
MAX_TOTAL_STRATEGIES = 10  # jp_live_runner に渡す最大数
MAX_SYMBOLS = 5            # 監視銘柄数


async def prepare_daily_strategies() -> tuple[list, dict]:
    """当日のペーパートレード用戦略を選定する。

    Returns:
        (strategies, report)
        strategies: StrategyBase のリスト（jp_live_runner.set_strategies() に渡す）
        report: 選定理由の詳細（振り返り用に保存）
    """
    today = datetime.now(JST).strftime("%Y-%m-%d")
    logger.info("=== 当日戦略準備 (%s) ===", today)

    # 1. 安定Robust一覧を取得（感度0.8以上）
    all_robust = get_robust_experiments(min_oos=0, limit=200)
    stable = [r for r in all_robust
              if r.get("sensitivity") is not None and r["sensitivity"] >= 0.8
              and r.get("oos_daily_pnl", 0) > 0]

    if not stable:
        logger.warning("安定Robustが0件。戦略選定をスキップ。")
        return [], {"date": today, "error": "no stable robust", "strategies": []}

    # 銘柄×手法で最良OOSのみ残す（重複排除）
    best_per_combo: dict[tuple[str, str], dict] = {}
    for r in stable:
        key = (r["strategy_name"], r["symbol"])
        if key not in best_per_combo or r["oos_daily_pnl"] > best_per_combo[key]["oos_daily_pnl"]:
            best_per_combo[key] = r
    candidates = list(best_per_combo.values())
    candidates.sort(key=lambda x: -x["oos_daily_pnl"])

    # 2. 各銘柄のレジーム検出
    symbols = list(set(r["symbol"] for r in candidates))
    regime_map: dict[str, str] = {}
    for sym in symbols[:20]:  # 上位20銘柄のみ
        try:
            df = await fetch_ohlcv(sym, "5m", 30)
            if not df.empty:
                result = detect_regime(sym, df)
                regime_map[sym] = result.regime
        except Exception as e:
            logger.warning("レジーム検出失敗 %s: %s", sym, e)
    logger.info("レジーム: %s", regime_map)

    # 3. レジーム適性でフィルタ＆ランキング
    #    同じレジームで実績のある手法を優先
    scored = []
    for r in candidates:
        sym = r["symbol"]
        regime = regime_map.get(sym, "unknown")
        # レジーム一致ボーナス（実験時のレジームと一致すれば+50%）
        regime_bonus = 1.5 if r.get("regime") == regime and regime != "unknown" else 1.0
        adjusted_score = r["oos_daily_pnl"] * regime_bonus
        scored.append({**r, "current_regime": regime, "adjusted_score": adjusted_score})

    scored.sort(key=lambda x: -x["adjusted_score"])

    # 4. 余力制約でポートフォリオ構築
    selected = []
    used_symbols = set()
    symbol_count: dict[str, int] = {}

    for r in scored:
        sym = r["symbol"]
        if len(used_symbols) >= MAX_SYMBOLS and sym not in used_symbols:
            continue
        if symbol_count.get(sym, 0) >= MAX_STRATEGIES_PER_SYMBOL:
            continue
        if len(selected) >= MAX_TOTAL_STRATEGIES:
            break

        selected.append(r)
        used_symbols.add(sym)
        symbol_count[sym] = symbol_count.get(sym, 0) + 1

    # 5. 戦略インスタンス生成
    strategies = []
    report_entries = []
    for r in selected:
        params = json.loads(r["params_json"]) if isinstance(r["params_json"], str) else r.get("params", {})
        try:
            strat = create_strategy(r["strategy_name"], r["symbol"], params=params)
            strategies.append(strat)
            report_entries.append({
                "strategy_name": r["strategy_name"],
                "symbol": r["symbol"],
                "params": params,
                "oos_daily_pnl": r["oos_daily_pnl"],
                "sensitivity": r.get("sensitivity"),
                "current_regime": r.get("current_regime", "unknown"),
                "experiment_regime": r.get("regime", "unknown"),
                "adjusted_score": r.get("adjusted_score", 0),
                "reason": _build_reason(r),
            })
            logger.info("  選定: %s × %s OOS%+.0f 感度%.2f [%s]",
                         r["strategy_name"], r["symbol"],
                         r["oos_daily_pnl"], r.get("sensitivity", 0),
                         r.get("current_regime", "?"))
        except Exception as e:
            logger.warning("戦略生成失敗: %s × %s: %s", r["strategy_name"], r["symbol"], e)

    report = {
        "date": today,
        "regime_map": regime_map,
        "total_robust": len(stable),
        "selected_count": len(strategies),
        "symbols": list(used_symbols),
        "strategies": report_entries,
        "expected_daily_pnl": sum(r["oos_daily_pnl"] for r in selected),
    }

    logger.info("戦略準備完了: %d戦略 / %d銘柄 / 期待損益 %+,.0f円/日",
                 len(strategies), len(used_symbols), report["expected_daily_pnl"])

    return strategies, report


# ── 場中レジームチェック＆戦略切り替え ─────────────────────────────────────

_last_regime_map: dict[str, str] = {}
_daily_report: dict = {}


async def midday_regime_check(jp_live_runner, jp_feed) -> dict | None:
    """場中に15分ごとに呼ばれ、レジーム変化時に戦略を入れ替える。

    Returns:
        変更があった場合は変更レポート、なければNone
    """
    global _last_regime_map, _daily_report
    from backend.feeds.jp_realtime_feed import is_market_open

    if not is_market_open():
        return None

    # 現在の監視銘柄のレジーム検出
    current_regimes: dict[str, str] = {}
    for sym in jp_feed._symbols:
        try:
            df = jp_feed.get_bars(sym)
            if df is not None and len(df) >= 30:
                result = detect_regime(sym, df)
                current_regimes[sym] = result.regime
        except Exception:
            pass

    if not current_regimes:
        return None

    # レジーム変化があるか
    changes = {}
    for sym, new_regime in current_regimes.items():
        old_regime = _last_regime_map.get(sym, "unknown")
        if old_regime != new_regime and old_regime != "unknown":
            changes[sym] = {"from": old_regime, "to": new_regime}

    _last_regime_map = current_regimes

    if not changes:
        return None

    logger.info("レジーム変化検出: %s", changes)

    # 戦略を再選定
    strategies, report = await prepare_daily_strategies()
    if strategies:
        jp_live_runner.set_strategies(strategies)
        symbols = list(set(s.meta.symbol for s in strategies))
        jp_feed.set_symbols(symbols)
        _daily_report = report

        logger.info("場中戦略切替: %d戦略 / 変化=%s", len(strategies), changes)

    return {"regime_changes": changes, "new_strategies": len(strategies)}


async def run_midday_check_loop(jp_live_runner, jp_feed) -> None:
    """場中15分ごとにレジームチェック＆戦略切替を行うループ。"""
    import asyncio
    from backend.feeds.jp_realtime_feed import is_market_open, seconds_to_market_open

    while True:
        if not is_market_open():
            wait = seconds_to_market_open()
            await asyncio.sleep(min(wait, 300))
            continue

        try:
            result = await midday_regime_check(jp_live_runner, jp_feed)
            if result:
                logger.info("場中チェック: %s", result)
        except Exception as e:
            logger.warning("場中チェックエラー: %s", e)

        await asyncio.sleep(15 * 60)  # 15分ごと


def get_daily_report() -> dict:
    """当日の戦略選定レポートを返す（振り返り用）。"""
    return _daily_report


def _build_reason(r: dict) -> str:
    """選定理由を人間が読める形で生成する。"""
    parts = [f"OOS{r['oos_daily_pnl']:+.0f}円"]
    if r.get("sensitivity"):
        parts.append(f"感度{r['sensitivity']:.2f}")
    regime = r.get("current_regime", "unknown")
    if regime != "unknown":
        if r.get("regime") == regime:
            parts.append(f"レジーム一致({regime})")
        else:
            parts.append(f"現在{regime}")
    return " / ".join(parts)
