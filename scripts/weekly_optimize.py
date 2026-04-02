"""週次最適化スクリプト — MACD×RCI IS/OOS グリッドサーチ + 自動パラメータ更新.

毎週月曜8:30にcronで実行（Mac側）:
    30 8 * * 1 cd /Users/himanosuke/algo-trading-system && .venv/bin/python scripts/weekly_optimize.py

処理フロー:
  1. push_ohlcv_cache.py を実行してデータ更新
  2. 対象銘柄をスクリーナーでランキング（価格フィルター後 上位15）
  3. IS/OOS グリッドサーチを全銘柄に対して実行
  4. Robust（IS+OOS両方プラス）なベストパラメータを data/macd_rci_params.json に保存
  5. VPS に rsync で転送
  6. Pushover で結果通知
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import pathlib
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pandas as pd

import numpy as np
from backend.backtesting.engine import run_backtest
from backend.lab.runner import (
    JP_CAPITAL_JPY, MARGIN_RATIO, POSITION_PCT, LOT_SIZE, MAX_STOCK_PRICE,
    fetch_ohlcv,
)
from backend.strategies.jp_stock.jp_macd_rci import JPMacdRci
from backend.strategies.jp_stock.pts_screener import rank_by_historical_activity

# 相場環境の定義（日足ATR/終値と20日トレンドで分類）
REGIMES = ["uptrend", "downtrend", "sideways", "volatile", "calm"]
REGIMES_JP = {
    "uptrend":   "上昇トレンド",
    "downtrend": "下降トレンド",
    "sideways":  "レンジ",
    "volatile":  "高ボラ",
    "calm":      "低ボラ（凪）",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── 設定 ─────────────────────────────────────────────────────────────────────
PARAMS_FILE   = pathlib.Path(__file__).parent.parent / "data" / "macd_rci_params.json"
VPS_HOST      = "bullvps"
VPS_DEST      = "/root/algo-trading-system/data/macd_rci_params.json"
UNIVERSE_TOP  = 15    # 価格フィルター後ランキング上位N銘柄
FETCH_DAYS    = 60    # 60日取得してIS/OOS各30日
MIN_TRADES    = 15    # IS期間の最低トレード数

GRID = {
    "tp_pct":        [0.002, 0.003, 0.004, 0.005, 0.006],
    "sl_pct":        [0.001, 0.0015, 0.002, 0.003],
    "rci_min_agree": [1, 2, 3],
    "macd_signal":   [7, 9, 11],
}


@dataclass
class BestParam:
    symbol:        str
    name:          str
    tp_pct:        float
    sl_pct:        float
    rci_min_agree: int
    macd_signal:   int
    max_pyramid:   int
    is_daily:      float
    is_pf:         float
    is_win_rate:   float
    oos_daily:     float
    oos_pf:        float
    oos_win_rate:  float
    robust:        bool
    last_updated:  str
    # 相場環境適性スコア (0〜100)
    regime_suitability: dict = None   # {"uptrend": 82, "downtrend": 35, ...}
    best_regime:        str  = ""
    worst_regime:       str  = ""

    def __post_init__(self):
        if self.regime_suitability is None:
            self.regime_suitability = {}


def _classify_day(row: pd.Series) -> str:
    """日足1行から相場環境を分類する。"""
    trend = row.get("trend_20d", 0.0)
    atr   = row.get("atr_pct",   0.0)
    if atr > 0.025:
        return "volatile"
    if atr < 0.008:
        return "calm"
    if trend > 5:
        return "uptrend"
    if trend < -5:
        return "downtrend"
    return "sideways"


def _add_regime_labels(df_1d: pd.DataFrame) -> pd.DataFrame:
    """日足DataFrameに相場環境ラベルを追加する。"""
    d = df_1d.copy()
    d["atr_pct"]   = (d["high"] - d["low"]) / d["close"]
    d["trend_20d"] = d["close"].pct_change(20) * 100
    d["regime"]    = d.apply(_classify_day, axis=1)
    return d


def _suitability_score(daily_pnl: float, pf: float, win_rate: float,
                        dd_pct: float, n_trades: int) -> int:
    """相場環境適性スコアを0〜100で計算する。

    トレード数が少なすぎる場合は信頼度が低いためペナルティ。
    """
    if n_trades < 5:
        return 0
    raw = (
        min(daily_pnl / 500, 40)          +  # 日次損益 (上限40点)
        min(max((pf - 1) * 20, -20), 30)  +  # PF     (上限30点)
        (win_rate - 40) * 0.5             +  # 勝率   (基準40%で0点)
        max(dd_pct * 2, -20)               +  # DD     (上限-20点ペナルティ)
        min(n_trades / 5, 10)               # トレード数 (上限10点)
    )
    return max(0, min(100, int(raw)))


async def compute_regime_suitability(
    sym: str, name: str, params: dict, df_5m: pd.DataFrame, df_1d: pd.DataFrame
) -> dict:
    """5m足バックテストを相場環境別に分割して適性スコアを計算する。

    Returns:
        {"uptrend": 82, "downtrend": 35, ...}
    """
    if df_1d.empty or df_5m.empty:
        return {}

    df_1d_labeled = _add_regime_labels(df_1d)

    scores: dict[str, int] = {}
    for regime in REGIMES:
        # レジームに分類された日付を抽出
        regime_dates = set(
            df_1d_labeled[df_1d_labeled["regime"] == regime].index.date
        )
        if len(regime_dates) < 3:
            continue

        # 5m足をそのレジーム日だけにフィルター
        df_regime = df_5m[
            pd.Series(df_5m.index.date, index=df_5m.index).isin(regime_dates)
        ]
        if len(df_regime) < 50:
            continue

        strat = JPMacdRci(sym, name, interval="5m",
                          macd_signal=params["macd_signal"],
                          rci_min_agree=params["rci_min_agree"],
                          tp_pct=params["tp_pct"],
                          sl_pct=params["sl_pct"],
                          max_pyramid=params.get("max_pyramid", 0))
        r = run_backtest(
            strat, df_regime,
            starting_cash=JP_CAPITAL_JPY * MARGIN_RATIO,
            fee_pct=0.0, position_pct=POSITION_PCT,
            usd_jpy=1.0, lot_size=LOT_SIZE,
            limit_slip_pct=0.003, eod_close_time=(15, 20),
        )
        scores[regime] = _suitability_score(
            r.daily_pnl_jpy, r.profit_factor, r.win_rate,
            r.max_drawdown_pct, r.num_trades,
        )

    return scores


def _run(strat, df) -> object:
    return run_backtest(
        strat, df,
        starting_cash=JP_CAPITAL_JPY * MARGIN_RATIO,
        fee_pct=0.0,
        position_pct=POSITION_PCT,
        usd_jpy=1.0,
        lot_size=LOT_SIZE,
        limit_slip_pct=0.003,
        eod_close_time=(15, 20),
    )


async def optimize_symbol(sym: str, name: str,
                          df_1d: pd.DataFrame | None = None) -> BestParam | None:
    df = await fetch_ohlcv(sym, "5m", FETCH_DAYS)
    if df.empty or len(df) < 200:
        logger.warning("%s: データ不足 (%d bars)", sym, len(df))
        return None

    split   = len(df) // 2
    df_is   = df.iloc[:split]
    df_oos  = df.iloc[split:]

    keys   = list(GRID.keys())
    combos = list(itertools.product(*GRID.values()))

    best_robust:  BestParam | None = None
    best_is_only: BestParam | None = None

    for combo in combos:
        params = dict(zip(keys, combo))
        if params["sl_pct"] >= params["tp_pct"]:
            continue

        strat = JPMacdRci(sym, name, interval="5m",
                          macd_signal=params["macd_signal"],
                          rci_min_agree=params["rci_min_agree"],
                          tp_pct=params["tp_pct"],
                          sl_pct=params["sl_pct"])

        r_is  = _run(strat, df_is)
        if r_is.num_trades < MIN_TRADES:
            continue

        r_oos = _run(strat, df_oos)
        robust = r_is.daily_pnl_jpy > 0 and r_oos.daily_pnl_jpy > 0

        # pyramid: IS+OOS両方プラスかつ IS PF > 1.2 なら有効
        pyramid = 2 if (robust and r_is.profit_factor >= 1.2) else 0

        bp = BestParam(
            symbol=sym, name=name,
            tp_pct=params["tp_pct"], sl_pct=params["sl_pct"],
            rci_min_agree=params["rci_min_agree"], macd_signal=params["macd_signal"],
            max_pyramid=pyramid,
            is_daily=round(r_is.daily_pnl_jpy, 1),
            is_pf=round(r_is.profit_factor, 3),
            is_win_rate=round(r_is.win_rate, 1),
            oos_daily=round(r_oos.daily_pnl_jpy, 1),
            oos_pf=round(r_oos.profit_factor, 3),
            oos_win_rate=round(r_oos.win_rate, 1),
            robust=robust,
            last_updated=str(date.today()),
        )

        if robust:
            if best_robust is None or r_is.score > _run(
                JPMacdRci(sym, name, interval="5m",
                          macd_signal=best_robust.macd_signal,
                          rci_min_agree=best_robust.rci_min_agree,
                          tp_pct=best_robust.tp_pct, sl_pct=best_robust.sl_pct),
                df_is
            ).score:
                best_robust = bp
        else:
            if best_is_only is None or r_is.score > _run(
                JPMacdRci(sym, name, interval="5m",
                          macd_signal=best_is_only.macd_signal,
                          rci_min_agree=best_is_only.rci_min_agree,
                          tp_pct=best_is_only.tp_pct, sl_pct=best_is_only.sl_pct),
                df_is
            ).score:
                best_is_only = bp

    result = best_robust or best_is_only
    if result is None:
        return None

    tag = "Robust" if result.robust else "IS-only"
    logger.info(
        "%s [%s]: tp=%.3f sl=%.3f rci=%d sig=%d → IS %+,.0f / OOS %+,.0f JPY/day",
        name, tag, result.tp_pct, result.sl_pct,
        result.rci_min_agree, result.macd_signal,
        result.is_daily, result.oos_daily,
    )

    # 相場環境別適性スコアを計算（df_1d が渡された場合のみ）
    if df_1d is not None and not df_1d.empty:
        best_params_dict = dict(
            tp_pct=result.tp_pct, sl_pct=result.sl_pct,
            rci_min_agree=result.rci_min_agree, macd_signal=result.macd_signal,
            max_pyramid=result.max_pyramid,
        )
        scores = await compute_regime_suitability(sym, name, best_params_dict, df, df_1d)
        result.regime_suitability = scores
        if scores:
            result.best_regime  = max(scores, key=scores.get)
            result.worst_regime = min(scores, key=scores.get)
            logger.info(
                "%s 相場適性: %s",
                name,
                " / ".join(f"{REGIMES_JP.get(r,r)}={s}" for r, s in sorted(scores.items(), key=lambda x: -x[1])),
            )

    return result


def load_params() -> dict:
    if PARAMS_FILE.exists():
        return json.loads(PARAMS_FILE.read_text())
    return {}


def save_params(params: dict) -> None:
    PARAMS_FILE.write_text(json.dumps(params, ensure_ascii=False, indent=2))
    logger.info("保存: %s", PARAMS_FILE)


def push_to_vps() -> None:
    cmd = ["rsync", "-av", str(PARAMS_FILE), f"{VPS_HOST}:{VPS_DEST}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        logger.info("VPS転送完了")
    else:
        logger.error("VPS転送失敗: %s", result.stderr)


def notify(summary: str) -> None:
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
        from backend.notify import push
        push(title="週次最適化完了", message=summary, priority=0)
    except Exception as e:
        logger.warning("通知失敗: %s", e)


async def main() -> None:
    logger.info("=== 週次最適化 開始 ===")

    # 1. OHLVキャッシュ更新
    logger.info("OHLCVキャッシュ更新中...")
    subprocess.run(
        [sys.executable, str(pathlib.Path(__file__).parent / "push_ohlcv_cache.py")],
        check=False,
    )

    # 2. スクリーナーで対象銘柄を動的選択（価格フィルター先）
    logger.info("銘柄スクリーニング（価格フィルター後 上位%d）...", UNIVERSE_TOP)
    ranked = rank_by_historical_activity(
        max_price=MAX_STOCK_PRICE,
        lookback_days=30,
        top_n=UNIVERSE_TOP,
    )
    targets = [(c["symbol"], c["name"]) for c in ranked]
    logger.info("対象銘柄: %s", [t[0] for t in targets])

    # 3. 各銘柄を最適化
    current_params = load_params()
    new_params = dict(current_params)
    changed: list[str] = []

    for sym, name in targets:
        logger.info("最適化中: %s (%s)", name, sym)
        # 相場環境分析用に1d足も取得
        df_1d = await fetch_ohlcv(sym, "1d", 90)
        result = await optimize_symbol(sym, name, df_1d=df_1d)
        if result is None:
            continue

        new_entry = {
            "tp_pct":        result.tp_pct,
            "sl_pct":        result.sl_pct,
            "rci_min_agree": result.rci_min_agree,
            "macd_signal":   result.macd_signal,
            "max_pyramid":   result.max_pyramid,
            "is_daily":      result.is_daily,
            "is_pf":         result.is_pf,
            "oos_daily":     result.oos_daily,
            "oos_pf":              result.oos_pf,
            "robust":              result.robust,
            "regime_suitability":  result.regime_suitability,
            "best_regime":         result.best_regime,
            "worst_regime":        result.worst_regime,
            "last_updated":        result.last_updated,
        }

        old = current_params.get(sym, {})
        if old.get("tp_pct") != result.tp_pct or old.get("sl_pct") != result.sl_pct:
            changed.append(
                f"{name}: tp={result.tp_pct} sl={result.sl_pct} "
                f"[{'Robust' if result.robust else 'IS-only'}] "
                f"OOS {result.oos_daily:+,.0f}JPY/day"
            )
        new_params[sym] = new_entry

    # 4. 保存 → VPS転送
    save_params(new_params)
    push_to_vps()

    # 5. サマリー通知
    robust_count = sum(1 for v in new_params.values() if v.get("robust"))
    summary_lines = [
        f"対象{len(targets)}銘柄 / Robust={robust_count}件",
        "",
    ]
    if changed:
        summary_lines.append("【パラメータ更新】")
        summary_lines.extend(changed)
    else:
        summary_lines.append("パラメータ変更なし")

    summary = "\n".join(summary_lines)
    logger.info("\n%s", summary)
    notify(summary)
    logger.info("=== 週次最適化 完了 ===")


if __name__ == "__main__":
    asyncio.run(main())
