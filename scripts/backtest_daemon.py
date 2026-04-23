"""PDCA学習型バックテストデーモン.

毎Generationで異なる実験を実行し、失敗を蓄積して繰り返さない。
成功パラメータの近傍を深掘りし、5Genごとにポートフォリオ合算を計測。

実行:
    nohup .venv/bin/python3 scripts/backtest_daemon.py > /tmp/daemon.log 2>&1 &
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import random
import subprocess
import sys
import time
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pandas as pd

from backend.backtesting.engine import run_backtest
from backend.backtesting.data_quality import check_and_clean_ohlcv
from backend.backtesting.walkforward import (
    multi_window_splits,
    rolling_splits,
    single_split,
    summarize_oos,
    summarize_oos_by_window,
)
from backend.backtesting.strategy_factory import (
    ALL_STRATEGY_NAMES,
    PARAM_RANGES,
    create as create_strategy,
    resolve_jp_ohlcv_interval,
)
from backend.backtesting.param_optimizer import (
    param_hash, neighborhood, random_sample, sensitivity_variants,
    compute_sensitivity,
)
from backend.lab.runner import (
    fetch_ohlcv, JP_CAPITAL_JPY, MARGIN_RATIO, POSITION_PCT, LOT_SIZE,
)
from backend.strategies.jp_stock.pts_screener import PTS_CANDIDATE_POOL
from backend.market_regime import _detect as detect_regime
from backend.storage.db import (
    get_db, save_experiment, get_robust_experiments,
    get_robust_experiments_diversified, get_latest_generation,
    add_to_graveyard, is_in_graveyard, get_graveyard_hashes,
    save_generation_log, save_portfolio_run, get_experiment_count,
    save_backtest_data_quality_events,
    replace_portfolio_latest_daily_agg,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/tmp/daemon_detail.log"),
    ],
)
logger = logging.getLogger(__name__)

# ── 定数 ──────────────────────────────────────────────────────────────────────
FETCH_DAYS = 90
# Phase C4: 日足は 2 年分まで取得し、長期レジーム検証の土台にする
FETCH_DAYS_DAILY = int(os.getenv("BACKTEST_FETCH_DAYS_DAILY", "730"))
MIN_BARS = 120
MIN_TRADES = 15
EXPERIMENTS_PER_GEN = 15
PORTFOLIO_EVERY_N = 1  # 毎Generationポートフォリオシミュレーション
CYCLE_WAIT = 5         # 5秒（OHLCVキャッシュ更新の最小間隔）

# Phase F4: 銘柄多様化と採用候補補強（2026-04-21）
# method_pdca 枠が 3103.T / 6613.T の 2 銘柄に偏って Robust 昇格が止まるのを防ぐため、
# 直近 N 件の採用履歴から占有率が閾値を超えた銘柄を候補プールから外す。
_METHOD_PDCA_HISTORY_MAXLEN = int(os.getenv("BACKTEST_METHOD_PDCA_HISTORY", "30"))
# Phase F5 (2026-04-22): 0.35 → 0.25 に圧縮。F4 実装後も 3103.T 単独 28.9% と top1 集中が
# 残っており、OOS Booster や novel 探索に回すための余地を作るため要件を厳しく。
_METHOD_PDCA_MAX_SHARE = float(os.getenv("BACKTEST_METHOD_PDCA_MAX_SHARE", "0.25"))
# explore 枠で同一 (strategy, symbol) の連投を抑制する直近履歴長と回数上限。
_EXPLORE_HISTORY_MAXLEN = int(os.getenv("BACKTEST_EXPLORE_HISTORY", "40"))
_EXPLORE_MAX_REPEAT = int(os.getenv("BACKTEST_EXPLORE_MAX_REPEAT", "3"))
# OOS booster: is_oos_pass=True だが oos_trades が薄い銘柄の再探索。
# 少数サンプル（例: 8058.T の trades=6）は T1 採用には使えないため、
# 近傍パラメータで継続評価し WF 窓・OOS 件数を積む。
_OOS_BOOSTER_MIN_TRADES = int(os.getenv("BACKTEST_OOS_BOOSTER_MIN_TRADES", "30"))
# Phase F5 (2026-04-22): Robust 判定に「walkforward 窓の過半通過」を必須化。
# ただし旧データ（walkforward 未実施 = wf_window_total==0）は後方互換で通す。
# 9468.T のように wf_total=2, wf_win=0 の時系列分割で崩れるケースを弾くのが目的。
_ROBUST_MIN_WF_PASS_RATIO = float(os.getenv("ROBUST_MIN_WF_PASS_RATIO", "0.5"))
# trending_down のような「有効手法が極端に薄い」レジームで、
# explore 枠から未検証手法が零れ落ちないよう最低保障枠を敷く。
_EXPLORE_NOVEL_FLOOR_WHEN_FEW_EFFECTIVE = int(
    os.getenv("BACKTEST_EXPLORE_NOVEL_FLOOR_WHEN_FEW_EFFECTIVE", "3")
)
# Phase F6 (2026-04-23): DB ベースのクラスタ冷却。直近 N 日で experiments 行の
# (strategy_name, symbol) 占有率が `MAX_SHARE` を超えたクラスタは、method_pdca /
# exploit / validate / sensitivity の候補から除外する。既定 OFF（観測ログのみ）。
# 全クラスタが閾値超過の場合は元プールにフォールバック（探索停止を避ける）。
_CLUSTER_COOLING_ENABLED = os.getenv(
    "BACKTEST_CLUSTER_COOLING_ENABLED", "0"
).strip() not in {"0", "false", "False"}
_CLUSTER_COOLING_DAYS = int(os.getenv("BACKTEST_CLUSTER_COOLING_DAYS", "7"))
_CLUSTER_COOLING_MAX_SHARE = float(os.getenv("BACKTEST_CLUSTER_COOLING_MAX_SHARE", "0.15"))
DATA_QUALITY_POLICY = os.getenv("BACKTEST_DATA_QUALITY_POLICY", "flag_only").strip()
DATA_QUALITY_ENABLED = os.getenv("BACKTEST_DATA_QUALITY_ENABLED", "1").strip() not in {"0", "false", "False"}
WALKFORWARD_ENABLED = os.getenv("BACKTEST_WALKFORWARD_ENABLED", "1").strip() not in {"0", "false", "False"}
WALKFORWARD_MODE = os.getenv("BACKTEST_WALKFORWARD_MODE", "rolling").strip()
WALKFORWARD_TOPK = int(os.getenv("BACKTEST_WALKFORWARD_TOPK", "5"))
_COST_MODEL_PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "backtest_cost_model.json"

ALL_SYMBOLS = [(c["symbol"], c["name"]) for c in PTS_CANDIDATE_POOL]

_OHLCV_KEY_SEP = "::"

# ── OHLCVキャッシュ（Generation内で再利用）─────────────────────────────────────
# キーは ``{symbol}::{interval}``（従来互換で symbol のみの参照は行わない）
_ohlcv_cache: dict[str, pd.DataFrame] = {}
_cache_date: str = ""
_quality_events: list[dict] = []

# ── プロセス永続の採用履歴（Phase F4, 2026-04-21） ───────────────────────────
# method_pdca / explore の銘柄偏りを抑えるため、直近 N 件を FIFO で保持する。
# デーモン再起動で履歴はクリアされる（同一セッション内の偏り抑止が目的）。
from collections import deque  # noqa: E402

_METHOD_PDCA_HISTORY: deque = deque(maxlen=_METHOD_PDCA_HISTORY_MAXLEN)
_EXPLORE_HISTORY: deque = deque(maxlen=_EXPLORE_HISTORY_MAXLEN)


def _load_cost_model() -> dict:
    default = {
        "fee_pct": 0.0,
        "limit_slip_pct": 0.003,
        "short_borrow_fee_annual": 0.0,
        "short_premium_daily_pct": 0.003,
        "long_margin_interest_annual": 0.028,
        "volume_impact_coeff": 0.3,
        "latency_bars": 0,
        "cost_model_enabled": True,
    }
    if not _COST_MODEL_PATH.exists():
        return default
    try:
        raw = json.loads(_COST_MODEL_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            default.update(raw)
    except Exception:
        pass
    return default


def _flush_quality_events() -> None:
    if not _quality_events:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    save_backtest_data_quality_events(today, list(_quality_events))
    total = len(_quality_events)
    flagged = sum(1 for r in _quality_events if r.get("status") == "flagged")
    excluded = sum(1 for r in _quality_events if r.get("status") == "excluded")
    payload = {
        "date": today,
        "policy": DATA_QUALITY_POLICY,
        "total_symbols": total,
        "flagged_symbols": flagged,
        "excluded_symbols": excluded,
        "issue_rate": float((flagged + excluded) / total) if total > 0 else 0.0,
        "events": _quality_events[-200:],
    }
    out = pathlib.Path(__file__).resolve().parent.parent / "data" / "backtest_data_quality_latest.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _quality_events.clear()


def _ohlcv_cache_key(symbol: str, interval: str) -> str:
    return f"{symbol}{_OHLCV_KEY_SEP}{interval}"


async def _get_ohlcv(symbol: str, interval: str = "5m") -> pd.DataFrame:
    global _cache_date
    today = str(date.today())
    if today != _cache_date:
        _ohlcv_cache.clear()
        _cache_date = today

    key = _ohlcv_cache_key(symbol, interval)
    if key not in _ohlcv_cache:
        # Phase C4: 日足は長期（デフォルト 730 日）で取得し、5分足は 90 日のまま
        days = FETCH_DAYS_DAILY if interval in ("1d", "1wk", "1mo") else FETCH_DAYS
        try:
            df = await asyncio.wait_for(
                fetch_ohlcv(symbol, interval, days),
                timeout=60 if days > 365 else 30,
            )
        except asyncio.TimeoutError:
            logger.warning("OHLCV取得タイムアウト %s %s", symbol, interval)
            df = pd.DataFrame()
        except Exception as e:
            logger.warning("OHLCV取得失敗 %s %s: %s", symbol, interval, e)
            df = pd.DataFrame()
        if DATA_QUALITY_ENABLED and not df.empty:
            qr = check_and_clean_ohlcv(symbol, df, policy=DATA_QUALITY_POLICY)
            _quality_events.append(
                {
                    "symbol": symbol,
                    "status": qr.status,
                    "policy": qr.policy,
                    "issue_count": len(qr.issues),
                    "issues": qr.issues,
                    "stats": qr.stats,
                }
            )
            df = qr.cleaned_df
        _ohlcv_cache[key] = df
    return _ohlcv_cache[key]


def _split(df: pd.DataFrame):
    mid = len(df) // 2
    return df.iloc[:mid], df.iloc[mid:]


# 大型株はプレミアム料なし（制度信用で空売り可能な銘柄）
# 小型・新興はプレミアム料1%/日を想定
PREMIUM_FREE_SYMBOLS = {
    "8306.T", "9432.T", "7203.T", "7267.T", "4568.T", "9433.T",
    "6758.T", "6954.T", "9984.T", "8316.T", "8411.T", "8604.T",
    "6098.T", "7201.T", "5401.T", "4502.T", "6752.T", "7269.T",
    "9468.T", "4911.T", "3382.T",  # 大型
}


def _run(strat, df, *, cost_enabled: bool = True):
    sym = strat.meta.symbol
    cm = _load_cost_model()
    fee_pct = float(cm.get("fee_pct", 0.0)) if cost_enabled else 0.0
    slip_pct = float(cm.get("limit_slip_pct", 0.003)) if cost_enabled else 0.0
    borrow_fee = float(cm.get("short_borrow_fee_annual", 0.0)) if cost_enabled else 0.0
    premium_default = 0.0 if sym in PREMIUM_FREE_SYMBOLS else float(cm.get("short_premium_daily_pct", 0.003))
    premium = premium_default if cost_enabled else 0.0
    margin_int = float(cm.get("long_margin_interest_annual", 0.0)) if cost_enabled else 0.0
    latency = int(cm.get("latency_bars", 0)) if cost_enabled else 0
    vol_impact = float(cm.get("volume_impact_coeff", 0.0)) if cost_enabled else 0.0
    return run_backtest(
        strat, df,
        starting_cash=JP_CAPITAL_JPY * MARGIN_RATIO,
        fee_pct=fee_pct, position_pct=POSITION_PCT,
        usd_jpy=1.0, lot_size=LOT_SIZE,
        limit_slip_pct=slip_pct, eod_close_time=(15, 25),
        short_borrow_fee_annual=borrow_fee,
        short_premium_daily_pct=premium,
        long_margin_interest_annual=margin_int,
        latency_bars=latency,
        volume_impact_coeff=vol_impact,
    )


# ── 実験実行 ──────────────────────────────────────────────────────────────────

async def run_experiment(exp: dict, generation: int) -> dict:
    """1つの実験を実行し、結果dictを返す。"""
    strategy_name = exp["strategy_name"]
    symbol = exp["symbol"]
    params = exp.get("params", {})
    exp_type = exp.get("type", "explore")
    hypothesis = exp.get("hypothesis", "")

    ohlcv_iv = resolve_jp_ohlcv_interval(strategy_name, params)
    df = await _get_ohlcv(symbol, ohlcv_iv)
    if df.empty or len(df) < MIN_BARS:
        return {"skip": True, "rci_slope_summary_json": "{}"}

    # 多時間足レジーム検出（1m/5m/15m/1h）
    regime = "unknown"
    regimes_multi = {}
    try:
        regime = detect_regime(symbol, df).regime  # 5分足
        regimes_multi["5m"] = regime
        for tf, rule in [("1m", "1min"), ("15m", "15min"), ("1h", "1h")]:
            df_tf = df.resample(rule).agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum"
            }).dropna()
            if len(df_tf) >= 50:
                regimes_multi[tf] = detect_regime(symbol, df_tf).regime
    except Exception:
        pass

    if WALKFORWARD_ENABLED and exp.get("type") in {"exploit", "validate", "method_pdca"}:
        if WALKFORWARD_MODE == "rolling":
            splits = rolling_splits(df, is_ratio=0.6, oos_ratio=0.2, step_ratio=0.2, min_bars=MIN_BARS)
        elif WALKFORWARD_MODE == "multi":
            # Phase C3: 複数の IS 長を同時に走らせ、短期/中期/長期すべてで
            # 生存する戦略のみ is_oos_pass を通す。5分足 120=1.5日, 240=3日, 480=6日 を基準。
            env_bars = os.getenv("BACKTEST_WALKFORWARD_WINDOW_BARS", "").strip()
            if env_bars:
                try:
                    window_bars = tuple(int(x) for x in env_bars.split(",") if x.strip())
                except ValueError:
                    window_bars = (120, 240, 480)
            else:
                window_bars = (120, 240, 480)
            splits = multi_window_splits(
                df, window_bars=window_bars, oos_ratio=0.3, step_ratio=0.5, min_bars=MIN_BARS,
            )
            if not splits:
                splits = rolling_splits(df, is_ratio=0.6, oos_ratio=0.2, step_ratio=0.2, min_bars=MIN_BARS)
        else:
            splits = single_split(df)
    else:
        splits = single_split(df)
    # rolling が足長不足で空でも、手法PDCAは単一スプリットで記録する（無音スキップ防止）
    if not splits and exp.get("type") == "method_pdca":
        splits = single_split(df)
    if not splits:
        return {"skip": True, "rci_slope_summary_json": "{}"}
    main_split = splits[0]
    df_is, df_oos = main_split.is_df, main_split.oos_df

    try:
        strat = create_strategy(strategy_name, symbol, params=params)
    except Exception as e:
        return {"skip": True, "error": str(e), "rci_slope_summary_json": "{}"}

    # IS実行
    r_is = _run(strat, df_is, cost_enabled=True)
    is_pnl = float(r_is.daily_pnl_jpy)
    is_trades = int(r_is.num_trades) if r_is.num_trades == r_is.num_trades else 0

    # IS不合格なら OOS スキップ（高速化）
    if is_pnl <= 0 or is_trades < MIN_TRADES:
        failure_reasons = []
        if is_pnl <= 0:
            failure_reasons.append("IS損失")
        if is_trades < MIN_TRADES:
            failure_reasons.append(f"取引不足({is_trades})")
        return {
            "generation": generation, "experiment_type": exp_type,
            "strategy_name": strategy_name, "symbol": symbol,
            "params": params, "regime": regime, "regimes_multi": regimes_multi,
            "is_daily_pnl": is_pnl, "is_win_rate": float(r_is.win_rate),
            "is_pf": float(r_is.profit_factor), "is_trades": is_trades,
            "robust": False, "failure_reasons": failure_reasons,
            "hypothesis": hypothesis,
            "parent_exp_id": exp.get("parent_exp_id"),
            "rci_slope_summary_json": "{}",
        }

    # OOS実行
    strat2 = create_strategy(strategy_name, symbol, params=params)
    rci_slope_summary_json = "{}"
    if strategy_name == "MacdRci":
        try:
            from backend.backtesting.macd_rci_slope_metrics import summarize_macd_rci_oos_signals

            sig_oos = strat2.generate_signals(df_oos)
            rci_slope_summary_json = json.dumps(
                summarize_macd_rci_oos_signals(sig_oos, params),
                ensure_ascii=False,
            )
        except Exception as e:
            logger.debug("rci slope summary skipped: %s", e)
            rci_slope_summary_json = json.dumps({"error": str(e)}, ensure_ascii=False)

    r_oos = _run(strat2, df_oos, cost_enabled=True)
    strat3 = create_strategy(strategy_name, symbol, params=params)
    r_oos_no_cost = _run(strat3, df_oos, cost_enabled=False)
    # walkforward summary (OOS)
    # Phase C3: 窓タグ別の勝ち負けも集計する
    main_tag = getattr(main_split, "window_tag", "default")
    wf_oos_values = [float(r_oos.daily_pnl_jpy)]
    wf_oos_pairs: list[tuple[str, float]] = [(main_tag, float(r_oos.daily_pnl_jpy))]
    if len(splits) > 1:
        for sp in splits[1:WALKFORWARD_TOPK]:
            s_is = create_strategy(strategy_name, symbol, params=params)
            rr_is = _run(s_is, sp.is_df, cost_enabled=True)
            if rr_is.daily_pnl_jpy <= 0:
                continue
            s_oos = create_strategy(strategy_name, symbol, params=params)
            rr_oos = _run(s_oos, sp.oos_df, cost_enabled=True)
            wf_oos_values.append(float(rr_oos.daily_pnl_jpy))
            wf_oos_pairs.append((getattr(sp, "window_tag", "default"), float(rr_oos.daily_pnl_jpy)))
    wf_sum = summarize_oos(wf_oos_values)
    wf_by_window = summarize_oos_by_window(wf_oos_pairs)
    oos_pnl = float(r_oos.daily_pnl_jpy)
    oos_is_ratio = oos_pnl / is_pnl if is_pnl > 0 else 0

    # walkforward 窓の評価値（robust / is_oos_pass の両方で参照する）
    wf_evaluated_n = len(wf_oos_values)
    wf_window_total = max(wf_evaluated_n, 1)  # 0 除算回避のため内部計算用は最小 1
    wf_window_win = sum(1 for v in wf_oos_values if v > 0)
    wf_window_pass_ratio = (
        wf_window_win / wf_evaluated_n if wf_evaluated_n > 0 else 0.0
    )
    # Phase F5: wf_window_pass_ratio ゲートを robust に畳み込む。wf_evaluated_n==0 は
    # walkforward 未実施の旧仕様サンプルなので後方互換で通す（判定に使わない）。
    wf_gate_ok = (
        wf_evaluated_n == 0
        or wf_window_pass_ratio >= _ROBUST_MIN_WF_PASS_RATIO
    )
    robust = bool(
        oos_pnl > 0
        and oos_is_ratio >= 0.3
        and wf_sum.get("worst", 0.0) > -3000
        and wf_gate_ok
    )

    # Phase B1: is_oos_pass — 1億到達まで戦略を生存させるための厳格ゲート
    # 条件: OOS勝率>=50%、walkforward窓のうち 2/3 以上が OOS PnL>0、
    #       worst > -1.5 * avg_win_jpy（負けが最大勝ちの1.5倍以内に収まる）
    oos_win_rate_val = float(r_oos.win_rate)
    avg_win_jpy_val = float(r_oos.avg_win_jpy) if r_oos.avg_win_jpy else 0.0
    worst_val = float(wf_sum.get("worst", 0.0))
    worst_threshold = -1.5 * avg_win_jpy_val if avg_win_jpy_val > 0 else -3000.0
    is_oos_pass = bool(
        robust
        and oos_win_rate_val >= 50.0
        and wf_window_pass_ratio >= (2.0 / 3.0)
        and worst_val > worst_threshold
    )

    failure_reasons = []
    if not robust:
        if oos_pnl <= 0:
            failure_reasons.append("OOS損失")
        if oos_is_ratio < 0.3:
            failure_reasons.append(f"OOS/IS比低({oos_is_ratio:.2f})")
    if robust and not is_oos_pass:
        if oos_win_rate_val < 50.0:
            failure_reasons.append(f"OOS勝率低({oos_win_rate_val:.1f}%)")
        if wf_window_pass_ratio < (2.0 / 3.0):
            failure_reasons.append(f"WF窓通過率低({wf_window_win}/{wf_window_total})")
        if worst_val <= worst_threshold:
            failure_reasons.append(f"WF最悪値超過({worst_val:.0f}<={worst_threshold:.0f})")

    return {
        "generation": generation, "experiment_type": exp_type,
        "strategy_name": strategy_name, "symbol": symbol,
        "params": params, "regime": regime, "regimes_multi": regimes_multi,
        "is_daily_pnl": is_pnl, "oos_daily_pnl": oos_pnl,
        "is_win_rate": float(r_is.win_rate),
        "oos_win_rate": float(r_oos.win_rate),
        "is_pf": float(r_is.profit_factor),
        "oos_pf": float(r_oos.profit_factor),
        "is_trades": is_trades,
        "oos_trades": int(r_oos.num_trades) if r_oos.num_trades == r_oos.num_trades else 0,
        "max_dd_pct": float(r_oos.max_drawdown_pct),
        "score": float(r_oos.score) if robust else 0,
        "robust": robust,
        "is_oos_pass": is_oos_pass,
        "wf_window_total": wf_window_total,
        "wf_window_win": wf_window_win,
        "wf_window_pass_ratio": float(wf_window_pass_ratio),
        "oos_is_ratio": oos_is_ratio,
        "oos_pnl_std": float(wf_sum.get("std", 0.0)),
        "oos_pnl_worst": float(wf_sum.get("worst", 0.0)),
        "wf_splits": int(wf_sum.get("count", 1)),
        "wf_by_window": {
            k: {"count": int(v.get("count", 0)), "wins": int(v.get("wins", 0)),
                "avg": float(v.get("avg", 0.0)), "worst": float(v.get("worst", 0.0))}
            for k, v in wf_by_window.items()
        },
        "calmar": float(r_oos.calmar),
        "cagr": float(r_oos.cagr),
        "daily_return_pct_mean": float(r_oos.daily_return_pct_mean),
        "daily_return_pct_std": float(r_oos.daily_return_pct_std),
        "cost_on_oos_daily_pnl": float(r_oos.daily_pnl_jpy),
        "cost_off_oos_daily_pnl": float(r_oos_no_cost.daily_pnl_jpy),
        "cost_drag_pct": float(
            ((float(r_oos_no_cost.daily_pnl_jpy) - float(r_oos.daily_pnl_jpy))
             / max(abs(float(r_oos_no_cost.daily_pnl_jpy)), 1.0)) * 100.0
        ),
        "failure_reasons": failure_reasons,
        "hypothesis": hypothesis,
        "parent_exp_id": exp.get("parent_exp_id"),
        "rci_slope_summary_json": rci_slope_summary_json,
    }


# ── 失敗分析 ──────────────────────────────────────────────────────────────────

def classify_failure(result: dict) -> str:
    """失敗の原因を分類する。"""
    is_pnl = result.get("is_daily_pnl", 0)
    oos_pnl = result.get("oos_daily_pnl", 0)
    is_trades = result.get("is_trades", 0)

    if is_trades < MIN_TRADES:
        return "low_trades"
    if is_pnl > 0 and (oos_pnl is None or oos_pnl <= 0):
        return "overfit"
    if oos_pnl is not None and oos_pnl < 0:
        return "negative_oos"
    return "unknown"


# ── プランナー ────────────────────────────────────────────────────────────────

def _get_regime_strategy_affinity() -> dict[str, dict[str, float]]:
    """蓄積データから レジーム×手法 のRobust率マトリクスを構築する。"""
    import sqlite3
    conn = sqlite3.connect(str(pathlib.Path(__file__).resolve().parent.parent / "data" / "algo_trading.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT regime, strategy_name,
               COUNT(*) as total,
               SUM(CASE WHEN robust=1 THEN 1 ELSE 0 END) as robust_n
        FROM experiments
        WHERE regime != '' AND regime != 'unknown'
        GROUP BY regime, strategy_name
        HAVING total >= 3
    """).fetchall()
    conn.close()

    affinity: dict[str, dict[str, float]] = {}
    for r in rows:
        regime = r["regime"]
        strat = r["strategy_name"]
        rate = r["robust_n"] / r["total"]
        affinity.setdefault(regime, {})[strat] = rate
    return affinity


def _rank_symbols_by_volatility(top_k: int = 8) -> list[tuple[str, str, float]]:
    """ATR% 降順で上位 top_k 銘柄を返す（キャッシュ不足はスキップ）。"""
    ranked: list[tuple[str, str, float]] = []
    for sym, name in ALL_SYMBOLS:
        df = _ohlcv_cache.get(_ohlcv_cache_key(sym, "5m"))
        if df is None or len(df) < 20:
            continue
        close = df["close"]
        high = df["high"]
        low = df["low"]
        tr = (high - low).rolling(14).mean()
        atr_pct = float(tr.iloc[-1] / close.iloc[-1] * 100) if close.iloc[-1] > 0 else 0.0
        if atr_pct > 0:
            ranked.append((sym, name, atr_pct))
    ranked.sort(key=lambda x: -x[2])
    return ranked[: max(1, top_k)]


def _pick_symbol_by_volatility() -> tuple[str, str]:
    """ATR% 最上位を返す（後方互換）。"""
    ranked = _rank_symbols_by_volatility(top_k=1)
    if ranked:
        return ranked[0][0], ranked[0][1]
    return random.choice(ALL_SYMBOLS)


def _pick_explore_symbol(strategy_name: str) -> tuple[str, str]:
    """explore 用銘柄選択（Phase F4）.

    ATR% 上位 8 銘柄を候補とし、直近 `_EXPLORE_HISTORY` で同一 (strat, sym) の
    採用が `_EXPLORE_MAX_REPEAT` を超えた銘柄を除外。全滅時は原プール。
    候補内はランダム（上位偏重を避けて裾野を広げる）。
    """
    ranked = _rank_symbols_by_volatility(top_k=8)
    if not ranked:
        return random.choice(ALL_SYMBOLS)
    hist_counts: dict[tuple[str, str], int] = {}
    for s, y in _EXPLORE_HISTORY:
        hist_counts[(s, y)] = hist_counts.get((s, y), 0) + 1
    filtered = [
        (sym, name)
        for sym, name, _atr in ranked
        if hist_counts.get((strategy_name, sym), 0) < _EXPLORE_MAX_REPEAT
    ]
    pool = filtered or [(s, n) for s, n, _ in ranked]
    return random.choice(pool)


METHOD_PDCA_KEYS: dict[str, list[str]] = {
    "MacdRci": [
        "entry_profile",
        "exit_profile",
        "hist_exit_delay_bars",
        "rci_exit_min_agree",
        "rci_entry_mode",
        "rci_gc_slope_enabled",
        "rci_gc_slope_lookback",
        "rci_gc_slope_min",
        "rci_gc_slope_max",
    ],
    "Breakout": ["lookback", "trend_bars", "vol_confirm_mult"],
    "Scalp": ["atr_period", "atr_min_pct", "vwap_dev_limit", "morning_only", "allow_short"],
    "EnhancedMacdRci": ["vwap_stop", "allow_reentry", "max_pyramid", "rci_min_agree"],
    "EnhancedScalp": ["max_pyramid", "rsi_exit_high", "bb_std"],
    "MaVol": [
        "interval_code",
        "ema_fast",
        "vol_confirm_mult",
        "full_session",
        "allow_short",
        "vwap_entry_margin_pct",
    ],
    "BbShort": ["bb_period", "bb_std", "full_session"],
}


def _sample_pdca_value(strategy_name: str, key: str):
    mn, mx, caster = PARAM_RANGES[strategy_name][key]
    if caster is int:
        return int(random.randint(int(mn), int(mx)))
    return float(round(random.uniform(float(mn), float(mx)), 6))


def _build_method_pdca_experiments(
    robust_list: list[dict],
    n_samples: int,
) -> list[dict]:
    """複数手法で手法内部ロジックを毎世代並行探索する。"""
    if n_samples <= 0:
        return []
    experiments: list[dict] = []
    robust_by_strategy: dict[str, list[dict]] = {}
    for r in robust_list:
        s = r.get("strategy_name")
        if s in METHOD_PDCA_KEYS:
            robust_by_strategy.setdefault(s, []).append(r)
    if not robust_by_strategy:
        return []

    active_strategies = sorted(
        robust_by_strategy.keys(),
        key=lambda s: max(float(x.get("oos_daily_pnl", 0.0)) for x in robust_by_strategy[s]),
        reverse=True,
    )
    # Phase F3: セクター集中度制約 — 同一バッチ内で同一セクターからの method_pdca 採用を制限する。
    # 例: n_samples=5 ならば 1 セクターあたり 2 件まで（40%）。
    try:
        from backend.backtesting.trade_guard import get_sector as _get_sector
    except Exception:  # pragma: no cover
        _get_sector = lambda s: None  # type: ignore[assignment]
    sector_cap = max(1, int((n_samples or 1) * 0.4))

    for i in range(n_samples):
        strat = active_strategies[i % len(active_strategies)]
        ranked = sorted(
            robust_by_strategy[strat],
            key=lambda r: float(r.get("oos_daily_pnl", 0.0)),
            reverse=True,
        )
        # 同一世代内で銘柄が偏らないよう、当バッチでの使用回数が少ない Robust を優先（OOS は副次キー）
        sym_counts: dict[str, int] = {}
        sector_counts: dict[str, int] = {}
        for ex in experiments:
            sym_counts[ex["symbol"]] = sym_counts.get(ex["symbol"], 0) + 1
            sec = _get_sector(ex["symbol"]) or "__unknown__"
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
        pool = ranked[: max(1, min(12, len(ranked)))]
        # セクター採用上限を超えた候補はまず除外、全滅時は元プールにフォールバック
        pool_filtered = [
            r for r in pool
            if sector_counts.get(_get_sector(r["symbol"]) or "__unknown__", 0) < sector_cap
        ] or pool
        # Phase F4 (2026-04-21): 直近 N 件の採用履歴で占有率が閾値を超えた銘柄を除外。
        # 同一銘柄 (3103.T / 6613.T) が method_pdca 枠で 9 割を占める偏りを抑制する。
        hist_counts: dict[str, int] = {}
        if _METHOD_PDCA_HISTORY:
            for s in _METHOD_PDCA_HISTORY:
                hist_counts[s] = hist_counts.get(s, 0) + 1
            share_cap = max(
                1, int(len(_METHOD_PDCA_HISTORY) * _METHOD_PDCA_MAX_SHARE)
            )
            pool_filtered = [
                r for r in pool_filtered
                if hist_counts.get(r["symbol"], 0) < share_cap
            ] or pool_filtered
        # ソートの第一キーを「直近履歴での使用回数」に変更。
        # 履歴が空・短い初期でも、OOS の大きさではなく使われていない銘柄を優先する。
        pool_sorted = sorted(
            pool_filtered,
            key=lambda r: (
                hist_counts.get(r["symbol"], 0),
                sym_counts.get(r["symbol"], 0),
                -float(r.get("oos_daily_pnl", 0.0)),
            ),
        )
        base = pool_sorted[0]
        base_params = (
            json.loads(base["params_json"])
            if isinstance(base["params_json"], str)
            else dict(base["params_json"])
        )

        keys = METHOD_PDCA_KEYS[strat]
        appended = False
        for _attempt in range(48):
            candidate = dict(base_params)
            n_mutate = min(len(keys), max(1, random.randint(1, 2)))
            for k in random.sample(keys, k=n_mutate):
                candidate[k] = _sample_pdca_value(strat, k)
            if candidate == base_params:
                continue
            ph = param_hash(strat, base["symbol"], candidate)
            if is_in_graveyard(strat, base["symbol"], ph):
                continue
            changed = [k for k in keys if candidate.get(k) != base_params.get(k)]
            experiments.append({
                "type": "method_pdca",
                "strategy_name": strat,
                "symbol": base["symbol"],
                "params": candidate,
                "parent_exp_id": base["id"],
                "hypothesis": f"手法PDCA: {strat} logic={','.join(changed[:3])}",
            })
            _METHOD_PDCA_HISTORY.append(base["symbol"])
            appended = True
            break
        if not appended:
            logger.warning(
                "  method_pdca: %s×%s で墓場・同一パラメータにより変異を出せず（スキップ）",
                strat, base["symbol"],
            )
    if n_samples > 0 and len(experiments) == 0:
        logger.warning(
            "  method_pdca: 予定 %d 件すべて生成失敗（Robust×METHOD_PDCA_KEYS の組か墓場を確認）",
            n_samples,
        )
    return experiments


def _overindexed_clusters(
    days: int = _CLUSTER_COOLING_DAYS,
    max_share: float = _CLUSTER_COOLING_MAX_SHARE,
) -> tuple[set[tuple[str, str]], list[dict]]:
    """直近 `days` 日で (strategy_name, symbol) 占有率が `max_share` を超えたクラスタを返す。

    第 2 戻り値は上位 5 件の診断情報（ログ用）。DB アクセス失敗時は空集合を返し、
    既存挙動を温存する。
    """
    try:
        import sqlite3
        from datetime import timedelta as _td
        db_path = pathlib.Path(__file__).resolve().parent.parent / "data" / "algo_trading.db"
        since = (datetime.now() - _td(days=days)).strftime("%Y-%m-%d")
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        try:
            total_row = con.execute(
                "SELECT COUNT(*) AS n FROM experiments WHERE date(created_at) >= ?",
                (since,),
            ).fetchone()
            total = int(total_row["n"] or 0) if total_row else 0
            if total <= 0:
                return set(), []
            rows = con.execute(
                """
                SELECT strategy_name, symbol, COUNT(*) AS cnt
                FROM experiments
                WHERE date(created_at) >= ?
                GROUP BY strategy_name, symbol
                ORDER BY cnt DESC
                LIMIT 20
                """,
                (since,),
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:  # pragma: no cover - DB 障害時は無効化
        logger.warning("cluster cooling: DB 参照失敗のため無効化: %s", exc)
        return set(), []

    over: set[tuple[str, str]] = set()
    diag: list[dict] = []
    for r in rows:
        share = float(r["cnt"] or 0) / float(total)
        diag.append({
            "strategy_name": r["strategy_name"],
            "symbol": r["symbol"],
            "cnt": int(r["cnt"] or 0),
            "share": share,
        })
        if share > max_share:
            over.add((r["strategy_name"], r["symbol"]))
    return over, diag[:5]


def plan_generation(generation: int) -> list[dict]:
    """レジーム×変動率に基づいて最適な手法×銘柄を選択する。"""
    experiments = []
    # Phase F4 (2026-04-21): 銘柄別の最良 Robust 1 行に絞って取得する。
    # `get_robust_experiments` は OOS 降順 LIMIT のため、3103.T / 6613.T の大量
    # 重複行で埋まり、method_pdca / exploit の候補銘柄が実質 2 銘柄に縮退してい
    # た。`..._diversified` は (strategy, symbol) ごとに最良 1 行を返すため、
    # プール内で各銘柄が均等の重みを持つ。
    robust_list = get_robust_experiments_diversified(min_oos=0, limit=100)
    n_robust = len(robust_list)

    # レジーム×手法のアフィニティ
    affinity = _get_regime_strategy_affinity()

    # 現在のレジーム（キャッシュ内の銘柄から代表的に検出）
    current_regime = "unknown"
    for key in list(_ohlcv_cache.keys())[:3]:
        df = _ohlcv_cache.get(key)
        sym = key.split(_OHLCV_KEY_SEP, 1)[0] if _OHLCV_KEY_SEP in key else key
        if df is not None and len(df) >= 50:
            try:
                current_regime = detect_regime(sym, df).regime
                break
            except Exception:
                pass

    # このレジームで有効な手法をランク付け
    regime_strats = affinity.get(current_regime, {})
    # Robust率でソート、0%の手法は除外しない（未検証を探索するため）
    ranked_strats = sorted(ALL_STRATEGY_NAMES,
                           key=lambda s: regime_strats.get(s, 0.5),  # 未検証=0.5で中間
                           reverse=True)

    # Robust手法の偏り分析
    robust_strats = {}
    for r in robust_list:
        s = r["strategy_name"]
        robust_strats[s] = robust_strats.get(s, 0) + 1

    # 予算配分（Phase F4 → F5: oos_booster 枠を拡張。2026-04-22 時点で is_oos_pass=True の
    # 3 銘柄（8058.T / 8306.T / 6752.T）がすべて oos_trades < 20 とサンプル薄で
    # 採用判定にはまだ早い。近傍パラメータ再探索の密度を上げて早期に oos_trades>=30 に
    # 乗せるのが狙い。effective_strats フィルタは掛けない（採用候補そのものを強化する）。
    if n_robust < 10:
        alloc = {"explore": 7, "exploit": 3, "validate": 2, "sensitivity": 1, "method_pdca": 2, "oos_booster": 1}
    elif n_robust < 30:
        alloc = {"explore": 5, "exploit": 3, "validate": 2, "sensitivity": 2, "method_pdca": 2, "oos_booster": 2}
    else:
        alloc = {"explore": 4, "exploit": 3, "validate": 2, "sensitivity": 1, "cross_pollinate": 1, "method_pdca": 3, "oos_booster": 3}

    logger.info("  Plan: regime=%s 手法ランク=%s Robust=%d → alloc=%s",
                 current_regime,
                 [(s, f"{regime_strats.get(s, 0)*100:.0f}%") for s in ranked_strats[:4]],
                 n_robust, alloc)

    # Phase F6: クラスタ冷却。ENABLED=0 でも観測ログは常に残す。
    overindexed, cluster_diag = _overindexed_clusters()
    if cluster_diag:
        logger.info(
            "  cluster_share[%dd] top: %s (over>%.0f%%: %d)",
            _CLUSTER_COOLING_DAYS,
            [f"{d['strategy_name']}×{d['symbol']}={d['share']*100:.1f}%" for d in cluster_diag],
            _CLUSTER_COOLING_MAX_SHARE * 100,
            len(overindexed),
        )
    robust_list_cool = robust_list
    if _CLUSTER_COOLING_ENABLED and overindexed:
        cooled = [
            r for r in robust_list
            if (r.get("strategy_name"), r.get("symbol")) not in overindexed
        ]
        if cooled:
            robust_list_cool = cooled
            logger.info(
                "  cluster_cooling: %d クラスタを冷却 (robust %d → %d)",
                len(overindexed), len(robust_list), len(cooled),
            )
        else:
            logger.info("  cluster_cooling: 全候補超過のためフォールバック（冷却しない）")

    # ── Explore: レジーム×変動率に基づく最適選択 ──
    # フェーズ2: 蓄積データから最適な手法×銘柄を選択
    regime_effective = affinity.get(current_regime, {})
    # Robust率10%以上の手法のみ（このレジームで実績あり）
    effective_strats = [s for s in ALL_STRATEGY_NAMES
                        if regime_effective.get(s, 0) >= 0.10]
    # 実績なければ全手法（探索継続）
    if not effective_strats:
        effective_strats = ALL_STRATEGY_NAMES

    # 「実績がまだない手法」も少なくとも一定割合は explore に回す。
    # effective フィルタは Robust 率 10% 未満を弾くので、新規追加した手法が永遠に
    # 探索されない問題が起きる。novel_strats を作り、explore 枠を交互配分する。
    novel_strats = [s for s in ALL_STRATEGY_NAMES if s not in effective_strats]

    logger.info(
        "  レジーム%s 有効手法: %s / 未検証手法: %s",
        current_regime, effective_strats, novel_strats,
    )

    # 世代ごとに開始位置をずらして、末尾の手法も必ず探索される（固定順序だと
    # explore 枠数より多い手法群の末尾が永久に回らない）。
    eff_start = random.randint(0, max(1, len(effective_strats)) - 1)
    nov_start = random.randint(0, max(1, len(novel_strats)) - 1) if novel_strats else 0

    # Phase F4: 有効手法がごく少数しか無い（例: trending_down で MacdRci のみ）場合、
    # explore 枠の半分以上を novel に回して未検証手法のサンプルを積む。
    explore_n = int(alloc.get("explore", 0))
    if novel_strats and len(effective_strats) < 3:
        novel_floor = min(
            explore_n,
            max(_EXPLORE_NOVEL_FLOOR_WHEN_FEW_EFFECTIVE, explore_n // 2 + 1),
        )
    else:
        novel_floor = 0

    novel_used = 0
    for idx in range(explore_n):
        use_novel = False
        if novel_strats:
            if novel_used < novel_floor:
                use_novel = True
            elif idx % 2 == 1:
                use_novel = True
        if use_novel:
            strat_name = novel_strats[(nov_start + novel_used) % len(novel_strats)]
            novel_used += 1
        else:
            strat_name = effective_strats[(eff_start + idx) % len(effective_strats)]
        # 銘柄選択: (strat, sym) 履歴を考慮して同一ペアの連投を抑制（Phase F4）。
        if _ohlcv_cache:
            sym, name = _pick_explore_symbol(strat_name)
        else:
            sym, name = random.choice(ALL_SYMBOLS)
        graveyard = get_graveyard_hashes(strat_name, sym)
        params_list = random_sample(strat_name, n_samples=1,
                                     graveyard_hashes=graveyard, symbol=sym)
        if params_list:
            experiments.append({
                "type": "explore", "strategy_name": strat_name,
                "symbol": sym, "params": params_list[0],
                "hypothesis": f"未探索: {strat_name}×{name} ランダムパラメータ",
            })
            _EXPLORE_HISTORY.append((strat_name, sym))

    if novel_floor > 0:
        logger.info(
            "  explore: novel floor %d/%d（effective 手法少の補強）novel_used=%d",
            novel_floor, explore_n, novel_used,
        )

    # ── Method-PDCA: 銘柄固定で手法ロジックを比較 ──
    experiments.extend(
        _build_method_pdca_experiments(
            robust_list_cool,
            alloc.get("method_pdca", 0),
        )
    )

    # ── OOS Booster: is_oos_pass=True だが oos_trades が薄い銘柄の裏付け補強 ──
    # Phase F4 (2026-04-21): 例えば 8058.T は WF 通過率 1.0 / OOS PF 6.96 と数字は良いが
    # oos_trades=6 と極端にサンプル不足。T1 採用ゲートに乗せる前に、近傍パラメータで
    # OOS 件数・WF 窓数を積み増す専用枠。effective_strats フィルタは掛けない（採用候補
    # そのものを強化するため）。
    booster_n = int(alloc.get("oos_booster", 0))
    if booster_n > 0 and robust_list:
        underpowered = [
            r for r in robust_list
            if int(r.get("is_oos_pass", 0) or 0) == 1
            and int(r.get("oos_trades", 0) or 0) < _OOS_BOOSTER_MIN_TRADES
        ]
        if underpowered:
            # Phase F5: oos_trades が少ない候補ほど優先度を上げる重み付き選択に変更。
            # 例) 8058.T(oos_tr=6) は 8306.T(oos_tr=14) より 24/12 = 2 倍選ばれやすくする。
            # 重みは (MIN_TRADES - oos_trades) を下限 1 でクランプ。
            weights = [
                max(1, _OOS_BOOSTER_MIN_TRADES - int(r.get("oos_trades", 0) or 0))
                for r in underpowered
            ]
            logger.info(
                "  oos_booster: 候補 %d 銘柄 (例 %s, 重み=%s)",
                len(underpowered),
                ",".join(
                    f"{r['symbol']}(tr={int(r.get('oos_trades') or 0)})"
                    for r in underpowered[:5]
                ),
                weights[:5],
            )
        # 同一銘柄に booster 枠が集中しすぎないよう、連続重複を軽くシャッフルで避ける
        seen_symbols_this_round: list[str] = []
        for _ in range(booster_n):
            if not underpowered:
                break
            # 直近 2 回連続で同じ銘柄を引かないよう、1 回だけリサンプル許容
            base = random.choices(underpowered, weights=weights, k=1)[0]
            if (
                seen_symbols_this_round[-2:] == [base["symbol"], base["symbol"]]
                and len(underpowered) > 1
            ):
                base = random.choices(underpowered, weights=weights, k=1)[0]
            seen_symbols_this_round.append(base["symbol"])

            base_params = (
                json.loads(base["params_json"])
                if isinstance(base["params_json"], str)
                else dict(base["params_json"])
            )
            # magnitude 0.05 → 0.08 に緩めて探索半径を広げる。ただし近傍なので勾配が
            # 残っていれば逸脱しすぎない。0.05 だと同じハッシュに衝突しやすく、
            # graveyard で弾かれて booster が空振りするケースがあった。
            variants = neighborhood(base_params, base["strategy_name"], magnitude=0.08)
            if not variants:
                continue
            chosen = random.choice(variants)
            ph = param_hash(base["strategy_name"], base["symbol"], chosen)
            if is_in_graveyard(base["strategy_name"], base["symbol"], ph):
                continue
            experiments.append({
                "type": "oos_booster",
                "strategy_name": base["strategy_name"],
                "symbol": base["symbol"],
                "params": chosen,
                "parent_exp_id": base["id"],
                "hypothesis": (
                    f"OOS強化: {base['symbol']} {base['strategy_name']} "
                    f"(trades={base.get('oos_trades')}) 近傍再探索"
                ),
            })

    # ── Exploit: このレジームで有効な手法のRobustを深掘り ──
    if robust_list_cool:
        # 現レジームで有効な手法のRobustだけフィルタ
        regime_robust = [r for r in robust_list_cool
                         if r["strategy_name"] in effective_strats]
        if not regime_robust:
            regime_robust = robust_list_cool  # フォールバック
        for _ in range(alloc.get("exploit", 0)):
            base = random.choice(regime_robust)
            base_params = json.loads(base["params_json"]) if isinstance(base["params_json"], str) else base["params_json"]
            variants = neighborhood(base_params, base["strategy_name"], magnitude=0.15)
            if variants:
                chosen = random.choice(variants)
                ph = param_hash(base["strategy_name"], base["symbol"], chosen)
                if not is_in_graveyard(base["strategy_name"], base["symbol"], ph):
                    experiments.append({
                        "type": "exploit", "strategy_name": base["strategy_name"],
                        "symbol": base["symbol"], "params": chosen,
                        "parent_exp_id": base["id"],
                        "hypothesis": f"Robust近傍探索: OOS{base['oos_daily_pnl']:+.0f}円の改善狙い",
                    })

    # ── Validate: 古いRobustの再検証 ──
    if robust_list_cool:
        stale = [r for r in robust_list_cool if r.get("sensitivity") is None]
        for r in stale[:alloc.get("validate", 0)]:
            base_params = json.loads(r["params_json"]) if isinstance(r["params_json"], str) else r["params_json"]
            experiments.append({
                "type": "validate", "strategy_name": r["strategy_name"],
                "symbol": r["symbol"], "params": base_params,
                "parent_exp_id": r["id"],
                "hypothesis": "Robust再検証",
            })

    # ── Sensitivity: パラメータ安定性チェック ──
    if robust_list_cool:
        for r in robust_list_cool[:alloc.get("sensitivity", 0)]:
            if r.get("sensitivity") is not None:
                continue
            base_params = json.loads(r["params_json"]) if isinstance(r["params_json"], str) else r["params_json"]
            experiments.append({
                "type": "sensitivity", "strategy_name": r["strategy_name"],
                "symbol": r["symbol"], "params": base_params,
                "parent_exp_id": r["id"],
                "hypothesis": "感度分析",
            })

    # ── Cross-Pollinate: 銘柄Aのパラメータを銘柄Bに ──
    if robust_list and alloc.get("cross_pollinate", 0) > 0:
        source = random.choice(robust_list)
        source_params = json.loads(source["params_json"]) if isinstance(source["params_json"], str) else source["params_json"]
        target_sym, target_name = random.choice(ALL_SYMBOLS)
        if target_sym != source["symbol"]:
            experiments.append({
                "type": "cross_pollinate",
                "strategy_name": source["strategy_name"],
                "symbol": target_sym, "params": source_params,
                "parent_exp_id": source["id"],
                "hypothesis": f"交差受粉: {source['symbol']}→{target_sym}",
            })

    return experiments[:EXPERIMENTS_PER_GEN]


# ── 感度分析 ──────────────────────────────────────────────────────────────────

async def run_sensitivity_check(result: dict, generation: int) -> float | None:
    """Robust結果の感度を計測し、DBに記録する。"""
    variants = sensitivity_variants(
        result["params"], result["strategy_name"], perturbation=0.10
    )
    if not variants:
        return None

    scores = []
    for vp in variants[:6]:
        r = await run_experiment({
            "type": "sensitivity", "strategy_name": result["strategy_name"],
            "symbol": result["symbol"], "params": vp,
            "hypothesis": "感度チェック用バリアント",
        }, generation)
        if not r.get("skip"):
            scores.append(r.get("score", 0))

    base_score = result.get("score", 0)
    return compute_sensitivity(base_score, scores) if scores else None


# ── インジケーターブースト分析 ─────────────────────────────────────────────

async def _run_boost_analysis(result: dict) -> None:
    """Robustトレードに追加インジケーターを重ねて、ブースト条件を探す。
    レジーム別のインジケーター設定も探索する。"""
    try:
        from backend.backtesting.indicator_boost import (
            analyze_trades_with_indicators, analyze_best_config,
        )
        sym = result["symbol"]
        iv = resolve_jp_ohlcv_interval(result["strategy_name"], result.get("params") or {})
        df = await _get_ohlcv(sym, iv)
        if df.empty:
            return
        params = result.get("params", {})
        regime = result.get("regime", "")

        # 1. レジーム対応ブースト分析
        boosts, meta = analyze_trades_with_indicators(
            result["strategy_name"], sym, params, df, regime=regime,
        )

        notable = [b for b in boosts if b.win_rate >= 0.65 and b.total >= 5]
        if notable:
            cfg_str = json.dumps(meta.get("indicator_config", {}), ensure_ascii=False)
            logger.info("    ブースト分析 %s×%s [%s] config=%s:",
                         result["strategy_name"], sym, regime, cfg_str)
            for b in notable[:5]:
                logger.info("      %s: 勝率%.0f%% (%d/%d) 平均PnL%+.0f円",
                             b.name, b.win_rate * 100, b.wins, b.total, b.avg_pnl)

        # 2. 最良インジケーター設定の探索（20Genに1回）
        gen = result.get("generation", 0)
        if gen % 20 == 0:
            best = analyze_best_config(
                result["strategy_name"], sym, params, df, regime=regime,
            )
            if best.get("best_config"):
                logger.info("    最良設定 [%s]: %s (スコア%.0f)",
                             regime, best["best_config"], best["best_score"])

    except Exception as e:
        logger.debug("ブースト分析エラー: %s", e)


# ── ポートフォリオシミュレーション ──────────────────────────────────────────

async def run_portfolio_sim(generation: int) -> float | None:
    """上位Robust銘柄でポートフォリオシミュレーションを実行する。"""
    from backend.backtesting.portfolio_sim import MIN_OOS_BARS_PORTFOLIO, simulate

    robust_list = get_robust_experiments(min_oos=50, limit=50)
    if len(robust_list) < 2:
        logger.info("  Portfolio: Robust不足（%d件）、スキップ", len(robust_list))
        return None

    # 変動率×OOSの積でランク付け（変動率が高い銘柄ほど回転で稼げる）
    scored = []
    for r in robust_list:
        pj = json.loads(r["params_json"]) if isinstance(r["params_json"], str) else dict(r["params_json"])
        iv = resolve_jp_ohlcv_interval(r["strategy_name"], pj)
        df = _ohlcv_cache.get(_ohlcv_cache_key(r["symbol"], iv))
        if df is not None and len(df) >= 20:
            close = df["close"]
            tr = (df["high"] - df["low"]).rolling(14).mean()
            atr_pct = float(tr.iloc[-1] / close.iloc[-1] * 100) if close.iloc[-1] > 0 else 0
        else:
            atr_pct = 0.3  # デフォルト
        combo_score = r["oos_daily_pnl"] * (1 + atr_pct)
        scored.append((combo_score, r))
    scored.sort(key=lambda x: -x[0])

    # 同一銘柄に複数手法を許可（エントリータイミングが違うので干渉しない）
    # ただし同一銘柄×同一手法は除外
    selected = []
    used_combos = set()  # (strategy, symbol)
    for _, r in scored:
        combo = (r["strategy_name"], r["symbol"])
        if combo in used_combos:
            continue
        params = json.loads(r["params_json"]) if isinstance(r["params_json"], str) else r["params_json"]
        selected.append({
            "strategy_name": r["strategy_name"],
            "symbol": r["symbol"],
            "params": params,
        })
        used_combos.add(combo)
        if len(selected) >= 5:  # 最大5戦略（同一銘柄の異手法含む）
            break
    # フォールバック
    if len(selected) < 2:
        for _, r in scored:
            combo = (r["strategy_name"], r["symbol"])
            if combo not in used_combos:
                params = json.loads(r["params_json"]) if isinstance(r["params_json"], str) else r["params_json"]
                selected.append({
                    "strategy_name": r["strategy_name"],
                    "symbol": r["symbol"],
                    "params": params,
                })
                used_combos.add(combo)
            if len(selected) >= 3:
                break

    if len(selected) < 2:
        return None

    # OHLCVキャッシュ準備（銘柄×実効時間足）
    df_cache: dict[str, pd.DataFrame] = {}
    for cfg in selected:
        iv = resolve_jp_ohlcv_interval(cfg["strategy_name"], cfg.get("params") or {})
        ck = _ohlcv_cache_key(cfg["symbol"], iv)
        if ck not in df_cache:
            df_cache[ck] = await _get_ohlcv(cfg["symbol"], iv)

    result = simulate(selected, df_cache)
    if not result.daily_pnl_by_date and selected:
        logger.warning(
            "  Portfolio: 暦日PnLなし（OOS が %d 本未満で除外された銘柄、または成份が 0 トレード）成分=%s",
            MIN_OOS_BARS_PORTFOLIO,
            [(c["strategy_name"], c["symbol"]) for c in selected],
        )
    dpnl = result.daily_pnls or []
    mx = max(dpnl) if dpnl else None
    mn = min(dpnl) if dpnl else None
    nd = len(dpnl)
    extra = ""
    if mx is not None and mn is not None:
        extra = f" 暦日max{mx:+,.0f}/min{mn:+,.0f}円({nd}日)"
    logger.info(
        f"  Portfolio: {len(selected)}戦略 → 平均日次{result.daily_pnl_jpy:+.0f}円{extra} "
        f"Sharpe={result.sharpe:.2f} DD={result.max_drawdown_pct:.1f}%"
    )

    save_portfolio_run(
        generation,
        selected,
        result.daily_pnl_jpy,
        result.sharpe,
        result.max_drawdown_pct,
        result.margin_util_pct,
        max_daily_pnl=mx,
        min_daily_pnl=mn,
        num_trading_days=nd or None,
    )
    try:
        nagg = replace_portfolio_latest_daily_agg(
            result.daily_pnl_by_date,
            generation=generation,
            total_trades=result.total_trades,
        )
        logger.info("  Portfolio → backtest_daily_agg: %d 暦日（strategy_id=portfolio_latest_curve）", nagg)
    except Exception as e:
        logger.warning("  replace_portfolio_latest_daily_agg failed: %s", e)
    return result.daily_pnl_jpy


# ── 進捗チェック＆通知 ─────────────────────────────────────────────────────────

_milestone_notified: set[str] = set()  # 通知済みマイルストーン
_portfolio_history: list[float] = []   # 直近ポートフォリオPnL履歴


def _refresh_backtest_quality_json() -> None:
    """data/backtest_quality_gate_latest.json と data/backtest_data_quality_latest.json を更新。"""
    root = pathlib.Path(__file__).resolve().parent.parent
    py = root / ".venv" / "bin" / "python3"
    if not py.exists():
        py = pathlib.Path(sys.executable)
    for name in ("evaluate_backtest_quality_gate.py", "backtest_data_quality_report.py"):
        script = root / "scripts" / name
        if not script.exists():
            continue
        try:
            subprocess.run(
                [str(py), str(script)],
                cwd=str(root),
                capture_output=True,
                timeout=180,
                check=False,
            )
        except Exception as e:
            logging.getLogger(__name__).warning("quality json %s: %s", name, e)


async def _check_milestone(generation: int, portfolio_pnl: float | None) -> None:
    """連続テスト移行の準備が整った時点でPushover通知。"""
    from backend.notify import push
    from backend.storage.db import get_robust_experiments
    import sqlite3

    robust_list = get_robust_experiments(min_oos=0, limit=200)
    stable_robust = [r for r in robust_list
                     if r.get("sensitivity") is not None and r["sensitivity"] >= 0.8]

    # ポートフォリオ履歴を蓄積（安定性判定用）
    if portfolio_pnl is not None:
        _portfolio_history.append(portfolio_pnl)

    # ── 判定基準 ──
    # 1) 合算1,000円/日が安定（直近10回のポートフォリオシムで8回以上1,000超え）
    recent = _portfolio_history[-10:] if len(_portfolio_history) >= 10 else []
    pf_stable = len(recent) >= 10 and sum(1 for p in recent if p >= 1000) >= 8

    # 2) 複数レジームでRobustな手法がある（3手法以上が異なるレジームで安定）
    conn = sqlite3.connect(str(pathlib.Path(__file__).resolve().parent.parent / "data" / "algo_trading.db"))
    conn.row_factory = sqlite3.Row
    regime_coverage = set()
    for r in stable_robust:
        if r.get("regime") and r["regime"] != "unknown":
            regime_coverage.add(r["regime"])
    conn.close()
    regime_diverse = len(regime_coverage) >= 3

    # 3) 手法の多様性（2種類以上の異なる手法でRobust）
    strategy_types = set(r["strategy_name"] for r in stable_robust)
    method_diverse = len(strategy_types) >= 2

    # 4) 安定Robust数が十分（感度0.8以上が5件以上）
    enough_robust = len(stable_robust) >= 5

    # ── 連続テスト移行判定 ──
    ready = pf_stable and regime_diverse and method_diverse and enough_robust

    if ready:
        key = "ready_for_continuous_test"
        if key not in _milestone_notified:
            _milestone_notified.add(key)

            # 詳細レポート
            avg_pf = sum(recent) / len(recent)
            lines = [
                "連続テスト移行の準備が整いました",
                "",
                f"ポートフォリオ合算: 直近10回平均 {avg_pf:+,.0f}円/日（{sum(1 for p in recent if p >= 1000)}/10回が1,000超え）",
                f"安定Robust: {len(stable_robust)}件（感度0.8以上）",
                f"手法: {', '.join(sorted(strategy_types))}",
                f"レジーム対応: {', '.join(sorted(regime_coverage))}",
                "",
                "TOP 5:",
            ]
            for r in stable_robust[:5]:
                lines.append(f"  {r['strategy_name']}×{r['symbol']} OOS{r['oos_daily_pnl']:+.0f} 感度{r['sensitivity']:.2f}")

            msg = "\n".join(lines)
            await push(
                "連続テスト移行OK",
                msg,
                priority=1,
                category="backtest",
                source="backtest_daemon._check_milestone",
            )
            logger.info("★ 連続テスト移行準備完了!")
            return

    # ── 中間進捗（100Genごと、7:00-20:00のみ）──
    now_hour = datetime.now().hour
    quiet_hours = now_hour >= 20 or now_hour < 7
    if generation % 100 == 0 and not quiet_hours:
        status = []
        if recent:
            avg = sum(recent) / len(recent)
            over1k = sum(1 for p in recent if p >= 1000)
            status.append(f"合算: 平均{avg:+,.0f}円/日 ({over1k}/{len(recent)}回1K超)")
        else:
            status.append(f"合算: データ蓄積中({len(_portfolio_history)}回)")
        status.append(f"安定Robust: {len(stable_robust)}件")
        status.append(f"手法: {len(strategy_types)}種 レジーム: {len(regime_coverage)}種")
        from backend.storage.db import get_experiment_count
        status.append(f"実験総数: {get_experiment_count()}件")

        key = f"progress_{generation}"
        if key not in _milestone_notified:
            _milestone_notified.add(key)
            msg = f"Gen{generation} 中間報告\n" + "\n".join(status)
            await push(
                "進捗",
                msg,
                priority=0,
                category="backtest",
                source="backtest_daemon._check_milestone",
            )
            logger.info("  中間報告送信")


# ── メインループ ──────────────────────────────────────────────────────────────

async def main():
    get_db()
    generation = get_latest_generation()

    logger.info("=" * 60)
    logger.info("PDCA学習型バックテストデーモン起動")
    logger.info("直前Generation: %d", generation)
    logger.info("=" * 60)

    while True:
        generation += 1
        t0 = time.time()
        logger.info("\n[Generation %d] 開始", generation)

        # ── Plan ──
        experiments = plan_generation(generation)
        logger.info("  計画: %d件の実験", len(experiments))

        # ── Do ──
        results = []
        robust_count = 0
        best_pnl = 0.0

        for i, exp in enumerate(experiments):
            try:
                result = await asyncio.wait_for(
                    run_experiment(exp, generation),
                    timeout=120,  # 1実験あたり2分でタイムアウト
                )
            except asyncio.TimeoutError:
                logger.warning("  [%d/%d] タイムアウト %s×%s", i+1, len(experiments),
                               exp.get("strategy_name","?"), exp.get("symbol","?"))
                continue
            if result.get("skip"):
                continue

            results.append(result)

            # ログ出力
            is_pnl = result.get("is_daily_pnl", 0)
            oos_pnl = result.get("oos_daily_pnl")
            tag = "★Robust" if result.get("robust") else "  NG"
            oos_str = f"OOS{oos_pnl:+.0f}" if oos_pnl is not None else "OOS skip"
            logger.info(
                f"  [{i+1}/{len(experiments)}] {tag} {result['strategy_name']}×{result['symbol']} "
                f"IS{is_pnl:+.0f} {oos_str} [{exp.get('type', '')}]"
            )

            # ── Check & Act ──
            if result.get("robust"):
                robust_count += 1
                oos = result.get("oos_daily_pnl", 0)
                if oos > best_pnl:
                    best_pnl = oos

                # Robust: 感度分析を実行
                if exp.get("type") != "sensitivity":
                    sens = await run_sensitivity_check(result, generation)
                    result["sensitivity"] = sens
                    if sens is not None:
                        logger.info("    感度: %.2f %s", sens,
                                     "（安定）" if sens > 0.6 else "（脆弱）")

                # Robust: インジケーターブースト分析（10Genに1回）
                if generation % 10 == 0 and exp.get("type") in ("explore", "exploit"):
                    await _run_boost_analysis(result)
            else:
                # 失敗分析 → 墓場
                failure_type = classify_failure(result)
                if failure_type in ("overfit", "negative_oos"):
                    ph = param_hash(result["strategy_name"], result["symbol"],
                                     result.get("params", {}))
                    detail = ", ".join(result.get("failure_reasons", []))
                    add_to_graveyard(result["strategy_name"], result["symbol"],
                                      ph, failure_type, detail)

            # DB保存（多時間足レジームをhypothesisに追記）
            rm = result.get("regimes_multi", {})
            if rm:
                tag = " ".join(f"[{tf}:{r}]" for tf, r in sorted(rm.items()) if r != "unknown")
                if tag:
                    h = result.get("hypothesis", "")
                    result["hypothesis"] = f"{h} {tag}".strip() if h else tag
            save_experiment(result)

        # ── ポートフォリオシミュレーション（5Genに1回）──
        portfolio_pnl = None
        if generation % PORTFOLIO_EVERY_N == 0:
            logger.info("  ポートフォリオシミュレーション実行...")
            try:
                portfolio_pnl = await asyncio.wait_for(
                    run_portfolio_sim(generation),
                    timeout=180,  # 3分でタイムアウト
                )
            except asyncio.TimeoutError:
                logger.warning("  ポートフォリオシミュレーション タイムアウト")
                portfolio_pnl = None

        # ── Generationサマリー ──
        duration = time.time() - t0
        summary = (
            f"Gen{generation}: {len(results)}実験 Robust{robust_count} "
            f"Best{best_pnl:+,.0f}円/日 "
            f"{f'Portfolio{portfolio_pnl:+,.0f}円/日' if portfolio_pnl else ''} "
            f"({duration:.0f}秒)"
        )
        logger.info("  %s", summary)

        save_generation_log(
            generation,
            json.dumps([{"type": e.get("type"), "strategy": e.get("strategy_name"),
                          "symbol": e.get("symbol")} for e in experiments],
                        ensure_ascii=False),
            summary, len(results), robust_count,
            best_pnl, portfolio_pnl, duration,
        )
        _flush_quality_events()

        try:
            from backend.storage.research_canonical_sync import sync_after_generation

            sync_after_generation(generation, summary, robust_found=robust_count)
        except Exception as e:
            logger.warning("research canonical sync failed: %s", e)

        try:
            await asyncio.to_thread(_refresh_backtest_quality_json)
        except Exception as e:
            logger.warning("backtest quality json refresh failed: %s", e)

        # ── 進捗チェック＆通知（条件達成時のみ） ──
        await _check_milestone(generation, portfolio_pnl)

        # ── 保有時間分析（50Genごと） ──
        if generation % 50 == 0:
            try:
                from backend.backtesting.holding_time import measure_holding_time, get_all_stats
                for r in results[:3]:  # 上位3件のRobustのみ
                    if r.get("robust"):
                        iv_ht = resolve_jp_ohlcv_interval(
                            r["strategy_name"], r.get("params") or {},
                        )
                        df_ht = await _get_ohlcv(r["symbol"], iv_ht)
                        if not df_ht.empty:
                            stats = measure_holding_time(
                                r["strategy_name"], r["symbol"],
                                r.get("params", {}), df_ht,
                            )
                            if stats:
                                logger.info(
                                    f"  保有時間 {r['strategy_name']}×{r['symbol']}: "
                                    f"中央値{stats['median_min']:.0f}分 "
                                    f"95pct{stats['p95_min']:.0f}分 "
                                    f"→ 締切{stats['entry_cutoff']}"
                                )
            except Exception as e:
                logger.debug("保有時間分析エラー: %s", e)

        # ── 自己診断（20Genごと） ──
        if generation % 20 == 0:
            issues = []
            if portfolio_pnl is not None and portfolio_pnl == 0:
                issues.append("ポートフォリオ0円")
            if robust_count == 0 and len(experiments) >= 5:
                issues.append("Robust0件")
            if duration > 600:
                issues.append(f"処理{duration:.0f}秒（遅延）")
            # 収束チェック
            from backend.storage.db import get_robust_experiments as _gr
            recent = _gr(min_oos=0, limit=10)
            if recent and all(r.get("strategy_name") == recent[0].get("strategy_name") for r in recent):
                issues.append(f"手法偏重({recent[0]['strategy_name']}のみ)")

            if issues:
                logger.warning("  ⚠ 自己診断: %s", ", ".join(issues))
                from backend.notify import push
                asyncio.ensure_future(push(
                    "警告",
                    f"Gen{generation}\n" + "\n".join(issues),
                    priority=0,
                    category="alert",
                    source="backtest_daemon.main",
                ))

        # ── 待機 ──
        await asyncio.sleep(CYCLE_WAIT)


if __name__ == "__main__":
    asyncio.run(main())
