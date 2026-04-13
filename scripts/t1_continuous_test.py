"""T1→T2 連続テスト — 30万円スタートで100万円到達までのシミュレーション.

安定Robust上位の手法をポートフォリオで運用し、
日次損益を累積して何営業日でT2(100万円)に到達するかを計測する。
未検証レジームの日は待機（トレードしない）。

実行:
    .venv/bin/python3 scripts/t1_continuous_test.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")

import numpy as np
import pandas as pd

from backend.backtesting.engine import run_backtest
from backend.backtesting.strategy_factory import create as create_strategy
from backend.lab.runner import fetch_ohlcv, JP_CAPITAL_JPY, MARGIN_RATIO, POSITION_PCT, LOT_SIZE
from backend.market_regime import _detect as detect_regime
from backend.storage.db import get_db, get_robust_experiments

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── 定数 ──────────────────────────────────────────────────────────────────────
T1_CAPITAL = 300_000.0
T2_TARGET = 1_000_000.0
MARGIN_RATIO_VAL = 3.3
MAX_POSITIONS = 3
POSITION_PCT_VAL = 0.33
EOD_CLOSE = (15, 25)

# 検証済みレジーム（これ以外の日は待機）
TRADEABLE_REGIMES = {"low_vol", "trending_down", "high_vol"}

OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "continuous_test"
OUT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class DayResult:
    date: str
    regime: str
    traded: bool
    pnl: float
    cumulative: float
    trades: int
    strategies_used: list[str]
    waited: bool = False  # 待機した日


@dataclass
class ContinuousTestResult:
    start_capital: float
    target_capital: float
    days: list[DayResult]
    reached_target: bool = False
    days_to_target: int = 0
    max_drawdown_pct: float = 0.0
    max_drawdown_jpy: float = 0.0
    bankrupt: bool = False
    final_capital: float = 0.0
    win_days: int = 0
    loss_days: int = 0
    wait_days: int = 0
    total_days: int = 0


def get_top_strategies(min_oos: float = 50, max_count: int = 10) -> list[dict]:
    """安定Robust上位を取得し、銘柄重複を排除。"""
    all_robust = get_robust_experiments(min_oos=0, limit=200)
    stable = [r for r in all_robust
              if r.get("sensitivity") is not None and r["sensitivity"] >= 0.8
              and r.get("oos_daily_pnl", 0) >= min_oos]

    # 銘柄×手法で最良OOSのみ残す
    best_per_combo: dict[tuple[str, str], dict] = {}
    for r in stable:
        key = (r["strategy_name"], r["symbol"])
        if key not in best_per_combo or r["oos_daily_pnl"] > best_per_combo[key]["oos_daily_pnl"]:
            best_per_combo[key] = r

    candidates = sorted(best_per_combo.values(), key=lambda x: -x["oos_daily_pnl"])

    # 銘柄重複排除（異なる銘柄を優先）
    selected = []
    used_symbols = set()
    for r in candidates:
        if r["symbol"] in used_symbols:
            continue
        selected.append(r)
        used_symbols.add(r["symbol"])
        if len(selected) >= max_count:
            break

    return selected


async def run_continuous_test() -> ContinuousTestResult:
    """連続テストを実行する。"""
    get_db()

    # 1. 戦略取得
    strategies = get_top_strategies()
    if not strategies:
        logger.error("安定Robust戦略が0件。テスト不可。")
        return ContinuousTestResult(T1_CAPITAL, T2_TARGET, [])

    logger.info("=== T1→T2 連続テスト ===")
    logger.info("開始資金: %s円 → 目標: %s円", f"{T1_CAPITAL:,.0f}", f"{T2_TARGET:,.0f}")
    logger.info("戦略:")
    for s in strategies:
        params = json.loads(s["params_json"]) if isinstance(s["params_json"], str) else {}
        logger.info("  %s × %s  OOS%+.0f円 感度%.2f [%s]",
                     s["strategy_name"], s["symbol"], s["oos_daily_pnl"],
                     s.get("sensitivity", 0), s.get("regime", "?"))

    # 2. 全銘柄のOHLCVを取得
    symbols = list(set(s["symbol"] for s in strategies))
    df_cache: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = await fetch_ohlcv(sym, "5m", 90)
            if not df.empty:
                df_cache[sym] = df
                logger.info("  OHLCV %s: %d本 (%s 〜 %s)",
                             sym, len(df), str(df.index[0])[:10], str(df.index[-1])[:10])
        except Exception as e:
            logger.warning("  OHLCV取得失敗 %s: %s", sym, e)

    if not df_cache:
        logger.error("OHLCVデータ取得不可。")
        return ContinuousTestResult(T1_CAPITAL, T2_TARGET, [])

    # 3. 各戦略のトレードリストを事前計算
    all_trades_by_day: dict[str, list[dict]] = defaultdict(list)

    for s in strategies:
        sym = s["symbol"]
        df = df_cache.get(sym)
        if df is None or df.empty:
            continue

        params = json.loads(s["params_json"]) if isinstance(s["params_json"], str) else {}
        try:
            strat = create_strategy(s["strategy_name"], sym, params=params)
            result = run_backtest(
                strat, df,
                starting_cash=T1_CAPITAL * MARGIN_RATIO_VAL,
                fee_pct=0.0,
                position_pct=POSITION_PCT_VAL,
                usd_jpy=1.0,
                lot_size=LOT_SIZE,
                limit_slip_pct=0.003,
                eod_close_time=EOD_CLOSE,
            )
            label = f"{s['strategy_name']}×{sym}"
            for trade in result.trades:
                day = str(trade.exit_time)[:10]
                all_trades_by_day[day].append({
                    "label": label,
                    "symbol": sym,
                    "pnl": trade.pnl,
                    "entry_price": trade.entry_price,
                    "qty": trade.qty,
                    "entry_time": trade.entry_time,
                    "exit_time": trade.exit_time,
                })
        except Exception as e:
            logger.warning("バックテストエラー %s %s: %s", s["strategy_name"], sym, e)

    # 4. 日別レジーム検出（代表銘柄の日足から）
    # 5分足の各日末尾のデータでレジーム判定
    day_regimes: dict[str, str] = {}
    representative_sym = symbols[0]
    rep_df = df_cache.get(representative_sym, pd.DataFrame())
    if not rep_df.empty:
        for day_str, group in rep_df.groupby(rep_df.index.date):
            if len(group) >= 20:
                try:
                    regime = detect_regime(representative_sym, group).regime
                    day_regimes[str(day_str)] = regime
                except Exception:
                    day_regimes[str(day_str)] = "unknown"

    # 5. 連続シミュレーション
    capital = T1_CAPITAL
    peak_capital = capital
    max_dd_jpy = 0.0
    days_result: list[DayResult] = []

    sorted_days = sorted(all_trades_by_day.keys())
    if day_regimes:
        # レジーム情報がある日も含める
        all_days = sorted(set(sorted_days) | set(day_regimes.keys()))
    else:
        all_days = sorted_days

    logger.info("\n=== 日別シミュレーション ===")
    logger.info(f"{'日付':12s} {'レジーム':15s} {'判定':4s} {'損益':>8s} {'累積':>10s} {'取引':>4s}")
    logger.info("-" * 60)

    for day in all_days:
        regime = day_regimes.get(day, "unknown")
        trades = all_trades_by_day.get(day, [])

        # 未検証レジームは待機
        if regime not in TRADEABLE_REGIMES and regime != "unknown":
            days_result.append(DayResult(
                date=day, regime=regime, traded=False, pnl=0,
                cumulative=capital, trades=0, strategies_used=[], waited=True,
            ))
            logger.info(f"{day:12s} {regime:15s} 待機     {0:>+8.0f}  {capital:>10,.0f}円")
            continue

        if not trades:
            continue

        # 余力制約付きで取引（複利: 現在資金ベースで計算）
        buying_power = capital * MARGIN_RATIO_VAL
        position_size = buying_power * POSITION_PCT_VAL
        logger.debug("  資金%.0f → 余力%.0f → 1ポジ%.0f", capital, buying_power, position_size)
        open_count = 0
        day_pnl = 0.0
        used_symbols = set()
        used_strats = []

        # 時系列順にソート
        trades.sort(key=lambda t: t["entry_time"])
        for t in trades:
            if open_count >= MAX_POSITIONS:
                break
            if t["symbol"] in used_symbols:
                continue
            # 実際の株数を資金に応じて再計算
            if t["entry_price"] > 0:
                qty = int(position_size / t["entry_price"] / 100) * 100
                if qty < 100:
                    continue
                pnl = (t["pnl"] / max(t["qty"], 1)) * qty if t["qty"] > 0 else t["pnl"]
            else:
                pnl = t["pnl"]

            day_pnl += pnl
            open_count += 1
            used_symbols.add(t["symbol"])
            used_strats.append(t["label"])

        capital += day_pnl
        peak_capital = max(peak_capital, capital)
        drawdown = peak_capital - capital
        max_dd_jpy = max(max_dd_jpy, drawdown)

        tag = "◎" if day_pnl > 0 else "✗" if day_pnl < 0 else "─"
        logger.info(f"{day:12s} {regime:15s} {tag:4s} {day_pnl:>+8,.0f}  {capital:>10,.0f}円  {open_count}件")

        days_result.append(DayResult(
            date=day, regime=regime, traded=True, pnl=day_pnl,
            cumulative=capital, trades=open_count, strategies_used=used_strats,
        ))

        # T2到達チェック
        if capital >= T2_TARGET:
            logger.info("\n★★★ T2到達! %s (%d営業日) ★★★", day, len([d for d in days_result if d.traded]))
            break

        # 破産チェック（元本の50%を割ったら撤退）
        if capital < T1_CAPITAL * 0.5:
            logger.info("\n✗✗✗ 破産ライン到達: %s (%,.0f円) ✗✗✗", day, capital)
            break

    # 6. 結果集計
    traded_days = [d for d in days_result if d.traded]
    win_days = sum(1 for d in traded_days if d.pnl > 0)
    loss_days = sum(1 for d in traded_days if d.pnl < 0)
    wait_days = sum(1 for d in days_result if d.waited)

    result = ContinuousTestResult(
        start_capital=T1_CAPITAL,
        target_capital=T2_TARGET,
        days=days_result,
        reached_target=capital >= T2_TARGET,
        days_to_target=len(traded_days) if capital >= T2_TARGET else 0,
        max_drawdown_pct=max_dd_jpy / peak_capital * 100 if peak_capital > 0 else 0,
        max_drawdown_jpy=max_dd_jpy,
        bankrupt=capital < T1_CAPITAL * 0.5,
        final_capital=capital,
        win_days=win_days,
        loss_days=loss_days,
        wait_days=wait_days,
        total_days=len(days_result),
    )

    # 7. レポート出力
    # DD復元速度
    from backend.backtesting.trade_guard import compute_recovery_stats, is_high_risk_day
    traded_pnls = [d.pnl for d in days_result if d.traded]
    recovery = compute_recovery_stats(traded_pnls, T1_CAPITAL)

    # イベント日の勝率
    event_wins = sum(1 for d in days_result if d.traded and d.pnl > 0 and is_high_risk_day(d.date))
    event_losses = sum(1 for d in days_result if d.traded and d.pnl < 0 and is_high_risk_day(d.date))
    event_total = event_wins + event_losses

    logger.info("\n" + "=" * 60)
    logger.info("T1→T2 連続テスト結果")
    logger.info("=" * 60)
    logger.info(f"開始資金:   {T1_CAPITAL:>10,.0f}円")
    logger.info(f"最終資金:   {capital:>10,.0f}円")
    logger.info(f"目標:       {T2_TARGET:>10,.0f}円 {'★達成!' if result.reached_target else '未達'}")
    logger.info(f"取引日数:   {len(traded_days)}日 (待機{wait_days}日)")
    logger.info(f"勝ち日:     {win_days}日 / 負け日: {loss_days}日 (勝率{win_days/(win_days+loss_days)*100:.0f}%)" if (win_days + loss_days) > 0 else "取引なし")
    if result.reached_target:
        logger.info(f"T2到達:     {result.days_to_target}営業日")
    logger.info(f"最大DD:     {max_dd_jpy:>10,.0f}円 ({result.max_drawdown_pct:.1f}%)")
    if result.bankrupt:
        logger.info("★ 破産ライン(50%)に到達")

    # 日次平均
    if traded_days:
        avg = sum(d.pnl for d in traded_days) / len(traded_days)
        logger.info(f"日次平均:   {avg:>+10,.0f}円")

    # DD復元
    if recovery:
        logger.info(f"DD復元:     {recovery.get('max_dd_jpy',0):>10,}円 → {'%d日で回復' % recovery['recovery_days'] if recovery.get('recovered') else '未回復'}")

    # イベント日
    if event_total > 0:
        logger.info(f"イベント日: {event_wins}勝{event_losses}敗 (勝率{event_wins/event_total*100:.0f}%)")

    # JSON保存
    out = {
        "test_date": str(date.today()),
        "start_capital": T1_CAPITAL,
        "target_capital": T2_TARGET,
        "final_capital": capital,
        "reached_target": bool(result.reached_target),
        "days_to_target": result.days_to_target,
        "max_drawdown_pct": result.max_drawdown_pct,
        "max_drawdown_jpy": max_dd_jpy,
        "bankrupt": bool(result.bankrupt),
        "win_days": win_days,
        "loss_days": loss_days,
        "wait_days": wait_days,
        "strategies": [
            {"name": s["strategy_name"], "symbol": s["symbol"],
             "oos_pnl": s["oos_daily_pnl"], "sensitivity": s.get("sensitivity")}
            for s in strategies
        ],
        "daily": [
            {"date": d.date, "regime": d.regime, "traded": bool(d.traded),
             "pnl": round(d.pnl), "cumulative": round(d.cumulative),
             "trades": d.trades}
            for d in days_result
        ],
    }
    out_path = OUT_DIR / f"t1_t2_test_{date.today()}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    logger.info(f"\n結果保存: {out_path}")

    return result


if __name__ == "__main__":
    asyncio.run(run_continuous_test())
