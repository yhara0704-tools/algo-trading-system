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
from backend.backtesting.strategy_factory import resolve_jp_ohlcv_interval
from backend.backtesting.trade_guard import get_sector
from backend.lab.runner import (
    JP_CAPITAL_JPY,
    JP_TRADING_DAYS_PER_YEAR,
    MARGIN_RATIO,
    POSITION_PCT,
    LOT_SIZE,
)

logger = logging.getLogger(__name__)

# OOS 半区間の最低本数。60 だと取得日数が短い銘柄が全スキップされ、暦日 PnL が常に空になりやすい。
MIN_OOS_BARS_PORTFOLIO = 40

MAX_POSITIONS = 3
CAPITAL = JP_CAPITAL_JPY
BUYING_POWER = CAPITAL * MARGIN_RATIO

# Phase D3: セクター集中度ガード — 同一セクター建玉比率がこの値を超えるならエントリー保留
SECTOR_CONCENTRATION_CAP = 0.40

# N5 (2026-05-03): 1 銘柄あたりの最大ポジション比率 (lot_multiplier 適用後の cap)
# POSITION_PCT * lot_multiplier がこの値を超えないよう制限する
# = 1 銘柄に余力 (BUYING_POWER) の何 % まで投入できるか
MAX_POSITION_PCT_PER_SYMBOL = 0.90


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
    """暦日順の損益（daily_pnl_by_date の値列と同じ順）。"""
    daily_pnl_by_date: dict[str, float] = field(default_factory=dict)
    """YYYY-MM-DD → その日の実現損益（擬似PF日次集計用）。"""
    total_trades: int = 0
    reentry_count: int = 0  # 日中再エントリー回数


def simulate(configs: list[dict], df_cache: dict[str, pd.DataFrame],
             days: int = 45) -> PortfolioResult:
    """動的余力配分ポートフォリオシミュレーション。

    ``df_cache`` のキーは ``symbol`` または ``{symbol}::{interval}``（``resolve_jp_ohlcv_interval`` と一致）。
    """

    # Step 1: 各戦略を独立にバックテスト → 全トレードリスト取得
    all_trades = []

    for i, cfg in enumerate(configs):
        sym = cfg["symbol"]
        iv = resolve_jp_ohlcv_interval(cfg["strategy_name"], cfg.get("params") or {})
        ck = f"{sym}::{iv}"
        # DataFrame を `or` で評価すると ambiguous になるので明示的にフォールバック
        df = df_cache.get(ck)
        if df is None:
            df = df_cache.get(sym)
        if df is None or df.empty:
            continue

        split = len(df) // 2
        df_oos = df.iloc[split:]
        if len(df_oos) < MIN_OOS_BARS_PORTFOLIO:
            logger.debug(
                "Portfolio skip short OOS %s %s: bars=%d (min=%d)",
                cfg["strategy_name"], sym, len(df_oos), MIN_OOS_BARS_PORTFOLIO,
            )
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
            # N5: 各 trade に lot_multiplier を伝搬 (動的余力配分で position_pct に乗じる)
            lot_mult = float(cfg.get("lot_multiplier", 1.0) or 1.0)
            for trade in result.trades:
                all_trades.append({
                    "label": label,
                    "config_idx": i,
                    "symbol": sym,
                    "strategy": cfg["strategy_name"],
                    "lot_multiplier": lot_mult,
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
        return PortfolioResult(strategies=configs, daily_pnl_by_date={}, daily_pnls=[])

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
            # N5: lot_multiplier 適用 + 1 銘柄上限 cap
            lot_mult = float(t.get("lot_multiplier", 1.0))
            pos_pct = min(POSITION_PCT * lot_mult, MAX_POSITION_PCT_PER_SYMBOL)
            # 実際のポジションサイズに基づくPnL再計算
            pos_value = equity * pos_pct
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

            # Phase D3: セクター集中度ガード
            # 同一セクターの保有銘柄数が SECTOR_CONCENTRATION_CAP を超えそうならスキップ。
            # 小口フェーズ（MAX_POSITIONS=3）で 1:2 の偏りが出る程度は許容し、
            # MAX_POSITIONS>=5 から実効的に効く閾値（40%）にしている。
            new_sector = get_sector(t.get("symbol", ""))
            if new_sector and len(open_positions) >= 2:
                same_sector = sum(
                    1 for p in open_positions.values()
                    if get_sector(p.get("symbol", "")) == new_sector
                )
                projected_ratio = (same_sector + 1) / (len(open_positions) + 1)
                if projected_ratio > SECTOR_CONCENTRATION_CAP:
                    # ガードで落とした分は pending に積み直さない（翌バーの再トリガー前提）
                    continue

            # 余力チェック (N5: lot_multiplier 適用 + 1 銘柄上限 cap)
            lot_mult = float(t.get("lot_multiplier", 1.0))
            pos_pct = min(POSITION_PCT * lot_mult, MAX_POSITION_PCT_PER_SYMBOL)
            pos_value = equity * pos_pct
            margin_in_use = sum(
                p["entry_price"] * p.get("actual_qty", p.get("qty", 100))
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
                    # 余力不足なら 100 株単位で減らす (lot_multiplier 高でも余力枯渇を回避)
                    qty = max(100, (int(available / t["entry_price"]) // 100) * 100)
                    required = t["entry_price"] * qty
                    if required > available or qty < 100:
                        continue

            t["actual_qty"] = qty
            open_positions[label] = t
            today_labels.add(label)
            peak_positions = max(peak_positions, len(open_positions))
            margin_used = sum(
                p["entry_price"] * p.get("actual_qty", p.get("qty", 100))
                for p in open_positions.values()
            )
            peak_margin = max(peak_margin, margin_used)

            if is_reentry:
                reentry_count += 1

    # 残りのオープンポジションを閉じる
    for label, t in open_positions.items():
        if t.get("exit_price") and t.get("entry_price"):
            # N5: actual_qty が決定済みならそれを使う、無ければ lot_mult 適用で再算出
            actual_qty = t.get("actual_qty")
            if not actual_qty:
                lot_mult = float(t.get("lot_multiplier", 1.0))
                pos_pct = min(POSITION_PCT * lot_mult, MAX_POSITION_PCT_PER_SYMBOL)
                pos_value = equity * pos_pct
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
    daily_pnl_by_date = dict(sorted(daily_pnl_map.items()))
    daily_pnls = list(daily_pnl_by_date.values())
    n_days = max(len(daily_pnls), 1)
    avg_daily = realized_pnl / n_days

    if len(daily_pnls) > 1:
        std = np.std(daily_pnls)
        sharpe = (np.mean(daily_pnls) / std * np.sqrt(252)) if std > 0 else 0
    else:
        sharpe = 0

    # Phase A2: equity 対比の DD に変更（複利で伸びた口座でも正しい MDD を得るため）
    # 従来は cum_pnl - peak を固定 CAPITAL で割っていたので、資産が増えた後の
    # 大きな円ベース DD が過小評価され、複利前提の promotion 判定に使えなかった。
    equity_series = np.array([CAPITAL] + [CAPITAL + cum for cum in np.cumsum(daily_pnls)])
    equity_peak = np.maximum.accumulate(equity_series)
    equity_peak_safe = np.where(equity_peak > 0, equity_peak, 1.0)
    dd_ratio = (equity_series - equity_peak) / equity_peak_safe
    max_dd_pct = float(np.min(dd_ratio) * 100) if len(dd_ratio) else 0.0
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
        daily_pnl_by_date=daily_pnl_by_date,
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


# ─────────────────────────────────────────────────────────────────────
# Phase D2: Tier Sweep — 30万→1億までの複利到達日数を推定する
# ─────────────────────────────────────────────────────────────────────

def sweep_tiers(
    base_result: PortfolioResult,
    *,
    trading_days_per_month: int = 20,
    target_goal_jpy: float = 100_000_000,
) -> dict:
    """`simulate()` の結果から、各 Tier での月次複利%と到達日数を推定する。

    仮定:
      - base_result は T1（現行 CAPITAL=30万円相当）で取得された daily_pnls を持つ
      - 各ティアの daily_return_pct は、基礎%/日から slippage_bps 分を減じて近似する
      - 月20営業日で複利計算

    戻り値:
      {
        "base_daily_return_pct": float,
        "base_capital_jpy": float,
        "tiers": [{"tier": str, "capital_start": float, "capital_end": float,
                    "daily_return_pct": float, "monthly_return_pct": float,
                    "days_in_tier": int, "cum_days": int}],
        "goal_days": int,      # 1億円到達までの合算日数
        "goal_months": float,  # 月次換算
      }
    """
    from backend.capital_tier import TIERS

    if not base_result.daily_pnls:
        return {
            "base_daily_return_pct": 0.0,
            "base_capital_jpy": float(CAPITAL),
            "tiers": [],
            "goal_days": 0,
            "goal_months": 0.0,
            "note": "daily_pnls empty",
        }

    base_daily_mean_jpy = float(np.mean(base_result.daily_pnls))
    base_daily_mean_pct = base_daily_mean_jpy / max(CAPITAL, 1)
    if base_daily_mean_pct <= 0:
        return {
            "base_daily_return_pct": base_daily_mean_pct * 100,
            "base_capital_jpy": float(CAPITAL),
            "tiers": [],
            "goal_days": 0,
            "goal_months": 0.0,
            "note": "base daily return <= 0 — 1億到達不能",
        }

    out_tiers: list[dict] = []
    cum_days = 0
    current_cap = float(TIERS[0].capital_min)

    for tier in TIERS:
        tier_max = float(tier.capital_max if tier.capital_max != float("inf") else target_goal_jpy)
        if current_cap >= tier_max:
            continue
        target = min(tier_max, target_goal_jpy)

        # slippage 増分で基礎%/日を減じる（bps → 率）
        slip_pct = float(tier.slippage_bps) / 10000.0
        # position_pct の差で収益率もスケーリング（T1 の position_pct を基準に比率補正）
        pp_ratio = float(tier.position_pct) / float(TIERS[0].position_pct or 0.5)
        tier_daily_pct = max(base_daily_mean_pct * pp_ratio - slip_pct, 1e-5)

        growth_needed = target / current_cap
        if growth_needed <= 1.0:
            days_in_tier = 0
        else:
            import math
            days_in_tier = int(math.ceil(math.log(growth_needed) / math.log(1.0 + tier_daily_pct)))
        cum_days += days_in_tier
        monthly_pct = ((1.0 + tier_daily_pct) ** trading_days_per_month - 1.0) * 100.0

        out_tiers.append({
            "tier": tier.name,
            "capital_start": round(current_cap, 0),
            "capital_end": round(target, 0),
            "position_pct": tier.position_pct,
            "max_concurrent": tier.max_concurrent,
            "slippage_bps": tier.slippage_bps,
            "daily_return_pct": round(tier_daily_pct * 100, 4),
            "monthly_return_pct": round(monthly_pct, 3),
            "days_in_tier": days_in_tier,
            "cum_days": cum_days,
        })
        current_cap = target
        if current_cap >= target_goal_jpy:
            break

    return {
        "base_daily_return_pct": round(base_daily_mean_pct * 100, 4),
        "base_capital_jpy": float(CAPITAL),
        "tiers": out_tiers,
        "goal_days": cum_days,
        "goal_months": round(cum_days / trading_days_per_month, 2),
        "goal_years": round(cum_days / JP_TRADING_DAYS_PER_YEAR, 2),
        "time_basis": "tse_trading_days",
        "trading_days_per_year": JP_TRADING_DAYS_PER_YEAR,
    }
