"""PortfolioSimulator v2 — 動的余力配分 × 日中複数エントリー × 銘柄間ローテーション.

各戦略を独立にバックテストしてトレードリストを取得し、
時系列で統合して余力制約のもと、最も期待値の高い銘柄に動的配分する。

エグジット後は余力が空くので、次に期待値の高いシグナルにすぐ再配分。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from backend.backtesting.engine import run_backtest
from backend.backtesting.strategy_factory import create as create_strategy
from backend.lab.runner import JP_CAPITAL_JPY, MARGIN_RATIO, POSITION_PCT, LOT_SIZE

logger = logging.getLogger(__name__)

MAX_POSITIONS = 3
CAPITAL = JP_CAPITAL_JPY
BUYING_POWER = CAPITAL * MARGIN_RATIO


@dataclass
class PortfolioResult:
    strategies: list[dict]
    daily_pnl_jpy: float = 0.0
    total_pnl_jpy: float = 0.0
    sharpe: float = 0.0
    max_drawdown_pct: float = 0.0
    max_positions_used: int = 0
    margin_util_pct: float = 0.0
    daily_pnls: list[float] = field(default_factory=list)
    total_trades: int = 0
    reentry_count: int = 0  # 日中再エントリー回数


def simulate(configs: list[dict], df_cache: dict[str, pd.DataFrame],
             days: int = 45) -> PortfolioResult:
    """動的余力配分ポートフォリオシミュレーション。"""

    # Step 1: 各戦略を独立にバックテスト → 全トレードリスト取得
    all_trades = []

    for i, cfg in enumerate(configs):
        sym = cfg["symbol"]
        df = df_cache.get(sym)
        if df is None or df.empty:
            continue

        split = len(df) // 2
        df_oos = df.iloc[split:]
        if len(df_oos) < 60:
            continue

        try:
            strat = create_strategy(
                cfg["strategy_name"], sym,
                params=cfg.get("params", {}),
            )
            result = run_backtest(
                strat, df_oos,
                starting_cash=BUYING_POWER,
                fee_pct=0.0,
                position_pct=POSITION_PCT,
                usd_jpy=1.0,
                lot_size=LOT_SIZE,
                limit_slip_pct=0.003,
                eod_close_time=(15, 25),
            )

            label = f"{cfg['strategy_name']}_{sym}"
            for trade in result.trades:
                all_trades.append({
                    "label": label,
                    "config_idx": i,
                    "symbol": sym,
                    "strategy": cfg["strategy_name"],
                    "entry_time": trade.entry_time,
                    "exit_time": trade.exit_time,
                    "pnl": trade.pnl,
                    "pnl_pct": trade.pnl_pct,
                    "entry_price": trade.entry_price,
                    "exit_price": trade.exit_price,
                    "qty": trade.qty if hasattr(trade, "qty") else 0,
                    "exit_reason": trade.exit_reason,
                })
        except Exception as e:
            logger.warning("Portfolio sim error %s %s: %s", cfg["strategy_name"], sym, e)

    if not all_trades:
        return PortfolioResult(strategies=configs)

    # Step 2: 全イベント（エントリー+エグジット）を時系列で統合
    events = []
    for t in all_trades:
        events.append({"time": t["entry_time"], "type": "entry", "trade": t})
        events.append({"time": t["exit_time"], "type": "exit", "trade": t})
    events.sort(key=lambda e: e["time"])

    # Step 3: 動的余力配分シミュレーション
    equity = BUYING_POWER
    open_positions: dict[str, dict] = {}  # label -> trade
    realized_pnl = 0.0
    peak_positions = 0
    peak_margin = 0.0
    daily_pnl_map: dict[str, float] = {}
    total_trades = 0
    reentry_count = 0
    seen_labels_today: dict[str, set] = {}  # date -> set of labels

    # エントリー待ちキュー（余力不足時に保持）
    pending_entries: list[dict] = []

    for event in events:
        t = event["trade"]
        day = str(event["time"])[:10]

        if event["type"] == "exit":
            label = t["label"]
            if label not in open_positions:
                continue

            # ポジションクローズ
            pos = open_positions.pop(label)
            # 実際のポジションサイズに基づくPnL再計算
            pos_value = equity * POSITION_PCT
            if t["entry_price"] > 0 and t["qty"] > 0:
                actual_qty = int(pos_value / t["entry_price"] / 100) * 100
                if actual_qty >= 100:
                    pnl = (t["exit_price"] - t["entry_price"]) * actual_qty
                else:
                    pnl = t["pnl"]
            else:
                pnl = t["pnl"]

            realized_pnl += pnl
            equity += pnl
            daily_pnl_map[day] = daily_pnl_map.get(day, 0) + pnl
            total_trades += 1

            # エグジット後: 待ちキューからエントリーを試行
            _try_pending_entries(
                pending_entries, open_positions, equity,
                peak_positions, daily_pnl_map, seen_labels_today,
            )

        elif event["type"] == "entry":
            label = t["label"]

            # 同じポジションが既にオープンならスキップ
            if label in open_positions:
                continue

            # 日中再エントリー判定
            today_labels = seen_labels_today.setdefault(day, set())
            is_reentry = label in today_labels

            # ポジション上限チェック
            if len(open_positions) >= MAX_POSITIONS:
                # キューに入れて余力が空いたら実行
                pending_entries.append(event)
                continue

            # 余力チェック（ロット倍率を考慮）
            pos_value = equity * POSITION_PCT
            margin_in_use = sum(
                p["entry_price"] * p.get("qty", 100)
                for p in open_positions.values()
            )
            available = equity - margin_in_use
            if t["entry_price"] > 0:
                qty = int(pos_value / t["entry_price"] / 100) * 100
                if qty < 100:
                    continue
                # 余力オーバーチェック
                required = t["entry_price"] * qty
                if required > available:
                    # 余力不足なら100株に減らして再チェック
                    qty = 100
                    required = t["entry_price"] * qty
                    if required > available:
                        continue

            t["actual_qty"] = qty
            open_positions[label] = t
            today_labels.add(label)
            peak_positions = max(peak_positions, len(open_positions))
            margin_used = len(open_positions) * pos_value
            peak_margin = max(peak_margin, margin_used)

            if is_reentry:
                reentry_count += 1

    # 残りのオープンポジションを閉じる
    for label, t in open_positions.items():
        if t.get("exit_price") and t.get("entry_price"):
            pos_value = equity * POSITION_PCT
            actual_qty = int(pos_value / t["entry_price"] / 100) * 100
            if actual_qty >= 100:
                pnl = (t["exit_price"] - t["entry_price"]) * actual_qty
            else:
                pnl = t["pnl"]
            realized_pnl += pnl
            equity += pnl
            day = str(t["exit_time"])[:10]
            daily_pnl_map[day] = daily_pnl_map.get(day, 0) + pnl
            total_trades += 1

    # Step 4: メトリクス計算
    daily_pnls = list(daily_pnl_map.values())
    n_days = max(len(daily_pnls), 1)
    avg_daily = realized_pnl / n_days

    if len(daily_pnls) > 1:
        std = np.std(daily_pnls)
        sharpe = (np.mean(daily_pnls) / std * np.sqrt(252)) if std > 0 else 0
    else:
        sharpe = 0

    cumulative = np.cumsum([0] + daily_pnls)
    peak = np.maximum.accumulate(cumulative)
    dd = cumulative - peak
    max_dd_pct = (float(np.min(dd)) / CAPITAL * 100) if CAPITAL > 0 else 0
    margin_util = (peak_margin / BUYING_POWER * 100) if BUYING_POWER > 0 else 0

    return PortfolioResult(
        strategies=configs,
        daily_pnl_jpy=avg_daily,
        total_pnl_jpy=realized_pnl,
        sharpe=sharpe,
        max_drawdown_pct=max_dd_pct,
        max_positions_used=peak_positions,
        margin_util_pct=margin_util,
        daily_pnls=daily_pnls,
        total_trades=total_trades,
        reentry_count=reentry_count,
    )


def _try_pending_entries(
    pending: list[dict],
    open_positions: dict,
    equity: float,
    peak_positions: int,
    daily_pnl_map: dict,
    seen_labels: dict,
) -> None:
    """待ちキューからエントリーを試行する。"""
    remaining = []
    for event in pending:
        t = event["trade"]
        label = t["label"]
        if label in open_positions:
            continue
        if len(open_positions) >= MAX_POSITIONS:
            remaining.append(event)
            continue
        open_positions[label] = t
        day = str(event["time"])[:10]
        seen_labels.setdefault(day, set()).add(label)
    pending.clear()
    pending.extend(remaining)
