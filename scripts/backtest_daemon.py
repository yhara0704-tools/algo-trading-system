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
import pathlib
import random
import sys
import time
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pandas as pd

from backend.backtesting.engine import run_backtest
from backend.backtesting.strategy_factory import (
    create as create_strategy, ALL_STRATEGY_NAMES, PARAM_RANGES,
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
    get_db, save_experiment, get_robust_experiments, get_latest_generation,
    add_to_graveyard, is_in_graveyard, get_graveyard_hashes,
    save_generation_log, save_portfolio_run, get_experiment_count,
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
MIN_BARS = 120
MIN_TRADES = 15
EXPERIMENTS_PER_GEN = 15
PORTFOLIO_EVERY_N = 1  # 毎Generationポートフォリオシミュレーション
CYCLE_WAIT = 5         # 5秒（OHLCVキャッシュ更新の最小間隔）

ALL_SYMBOLS = [(c["symbol"], c["name"]) for c in PTS_CANDIDATE_POOL]

# ── OHLCVキャッシュ（Generation内で再利用）─────────────────────────────────────
_ohlcv_cache: dict[str, pd.DataFrame] = {}
_cache_date: str = ""


async def _get_ohlcv(symbol: str) -> pd.DataFrame:
    global _cache_date
    today = str(date.today())
    if today != _cache_date:
        _ohlcv_cache.clear()
        _cache_date = today

    if symbol not in _ohlcv_cache:
        try:
            df = await asyncio.wait_for(
                fetch_ohlcv(symbol, "5m", FETCH_DAYS),
                timeout=30,  # 30秒でタイムアウト
            )
        except asyncio.TimeoutError:
            logger.warning("OHLCV取得タイムアウト %s", symbol)
            df = pd.DataFrame()
        except Exception as e:
            logger.warning("OHLCV取得失敗 %s: %s", symbol, e)
            df = pd.DataFrame()
        _ohlcv_cache[symbol] = df
    return _ohlcv_cache[symbol]


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


def _run(strat, df):
    sym = strat.meta.symbol
    premium = 0.0 if sym in PREMIUM_FREE_SYMBOLS else 0.003  # 0.3%/日（現実的想定）
    return run_backtest(
        strat, df,
        starting_cash=JP_CAPITAL_JPY * MARGIN_RATIO,
        fee_pct=0.0, position_pct=POSITION_PCT,
        usd_jpy=1.0, lot_size=LOT_SIZE,
        limit_slip_pct=0.003, eod_close_time=(15, 25),
        short_borrow_fee_annual=0.0,
        short_premium_daily_pct=premium,
    )


# ── 実験実行 ──────────────────────────────────────────────────────────────────

async def run_experiment(exp: dict, generation: int) -> dict:
    """1つの実験を実行し、結果dictを返す。"""
    strategy_name = exp["strategy_name"]
    symbol = exp["symbol"]
    params = exp.get("params", {})
    exp_type = exp.get("type", "explore")
    hypothesis = exp.get("hypothesis", "")

    df = await _get_ohlcv(symbol)
    if df.empty or len(df) < MIN_BARS:
        return {"skip": True}

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

    df_is, df_oos = _split(df)

    try:
        strat = create_strategy(strategy_name, symbol, params=params)
    except Exception as e:
        return {"skip": True, "error": str(e)}

    # IS実行
    r_is = _run(strat, df_is)
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
        }

    # OOS実行
    strat2 = create_strategy(strategy_name, symbol, params=params)
    r_oos = _run(strat2, df_oos)
    oos_pnl = float(r_oos.daily_pnl_jpy)
    oos_is_ratio = oos_pnl / is_pnl if is_pnl > 0 else 0
    robust = bool(oos_pnl > 0 and oos_is_ratio >= 0.3)

    failure_reasons = []
    if not robust:
        if oos_pnl <= 0:
            failure_reasons.append("OOS損失")
        if oos_is_ratio < 0.3:
            failure_reasons.append(f"OOS/IS比低({oos_is_ratio:.2f})")

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
        "oos_is_ratio": oos_is_ratio,
        "failure_reasons": failure_reasons,
        "hypothesis": hypothesis,
        "parent_exp_id": exp.get("parent_exp_id"),
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


def _pick_symbol_by_volatility() -> tuple[str, str]:
    """株価変動率が高い銘柄を優先選択する。OHLCVキャッシュから直近ATR%を計算。"""
    best_atr = 0.0
    best = None
    for sym, name in ALL_SYMBOLS:
        df = _ohlcv_cache.get(sym)
        if df is None or len(df) < 20:
            continue
        close = df["close"]
        high = df["high"]
        low = df["low"]
        tr = (high - low).rolling(14).mean()
        atr_pct = float(tr.iloc[-1] / close.iloc[-1] * 100) if close.iloc[-1] > 0 else 0
        if atr_pct > best_atr:
            best_atr = atr_pct
            best = (sym, name, atr_pct)

    if best:
        return best[0], best[1]
    return random.choice(ALL_SYMBOLS)


def plan_generation(generation: int) -> list[dict]:
    """レジーム×変動率に基づいて最適な手法×銘柄を選択する。"""
    experiments = []
    robust_list = get_robust_experiments(min_oos=0, limit=100)
    n_robust = len(robust_list)

    # レジーム×手法のアフィニティ
    affinity = _get_regime_strategy_affinity()

    # 現在のレジーム（キャッシュ内の銘柄から代表的に検出）
    current_regime = "unknown"
    for sym in list(_ohlcv_cache.keys())[:3]:
        df = _ohlcv_cache.get(sym)
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

    # 予算配分
    if n_robust < 10:
        alloc = {"explore": 8, "exploit": 4, "validate": 2, "sensitivity": 1}
    elif n_robust < 30:
        alloc = {"explore": 6, "exploit": 5, "validate": 2, "sensitivity": 2}
    else:
        alloc = {"explore": 7, "exploit": 4, "validate": 2, "sensitivity": 1, "cross_pollinate": 1}

    logger.info("  Plan: regime=%s 手法ランク=%s Robust=%d → alloc=%s",
                 current_regime,
                 [(s, f"{regime_strats.get(s, 0)*100:.0f}%") for s in ranked_strats[:4]],
                 n_robust, alloc)

    # ── Explore: レジーム×変動率に基づく最適選択 ──
    # フェーズ2: 蓄積データから最適な手法×銘柄を選択
    regime_effective = affinity.get(current_regime, {})
    # Robust率10%以上の手法のみ（このレジームで実績あり）
    effective_strats = [s for s in ALL_STRATEGY_NAMES
                        if regime_effective.get(s, 0) >= 0.10]
    # 実績なければ全手法（探索継続）
    if not effective_strats:
        effective_strats = ALL_STRATEGY_NAMES

    logger.info("  レジーム%s有効手法: %s", current_regime, effective_strats)

    for idx in range(alloc.get("explore", 0)):
        # 手法: このレジームで有効な手法をRobust率順に回す
        strat_name = effective_strats[idx % len(effective_strats)]
        # 銘柄: 変動率が高い銘柄を優先
        if _ohlcv_cache:
            sym, name = _pick_symbol_by_volatility()
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

    # ── Exploit: このレジームで有効な手法のRobustを深掘り ──
    if robust_list:
        # 現レジームで有効な手法のRobustだけフィルタ
        regime_robust = [r for r in robust_list
                         if r["strategy_name"] in effective_strats]
        if not regime_robust:
            regime_robust = robust_list  # フォールバック
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
    if robust_list:
        stale = [r for r in robust_list if r.get("sensitivity") is None]
        for r in stale[:alloc.get("validate", 0)]:
            base_params = json.loads(r["params_json"]) if isinstance(r["params_json"], str) else r["params_json"]
            experiments.append({
                "type": "validate", "strategy_name": r["strategy_name"],
                "symbol": r["symbol"], "params": base_params,
                "parent_exp_id": r["id"],
                "hypothesis": "Robust再検証",
            })

    # ── Sensitivity: パラメータ安定性チェック ──
    if robust_list:
        for r in robust_list[:alloc.get("sensitivity", 0)]:
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
        df = await _get_ohlcv(sym)
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
    from backend.backtesting.portfolio_sim import simulate

    robust_list = get_robust_experiments(min_oos=50, limit=50)
    if len(robust_list) < 2:
        logger.info("  Portfolio: Robust不足（%d件）、スキップ", len(robust_list))
        return None

    # 変動率×OOSの積でランク付け（変動率が高い銘柄ほど回転で稼げる）
    scored = []
    for r in robust_list:
        df = _ohlcv_cache.get(r["symbol"])
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
            if (r["strategy_name"], r["symbol"]) not in used_combos:
                params = json.loads(r["params_json"]) if isinstance(r["params_json"], str) else r["params_json"]
                selected.append({
                    "strategy_name": r["strategy_name"],
                    "symbol": r["symbol"],
                    "params": params,
            })
            used_symbols.add(r["symbol"])
            if len(selected) >= 3:
                break

    if len(selected) < 2:
        return None

    # OHLCVキャッシュ準備
    df_cache = {}
    for cfg in selected:
        df_cache[cfg["symbol"]] = await _get_ohlcv(cfg["symbol"])

    result = simulate(selected, df_cache)
    logger.info(
        f"  Portfolio: {len(selected)}戦略 → 日次{result.daily_pnl_jpy:+.0f}円 "
        f"Sharpe={result.sharpe:.2f} DD={result.max_drawdown_pct:.1f}%"
    )

    save_portfolio_run(
        generation, selected, result.daily_pnl_jpy,
        result.sharpe, result.max_drawdown_pct, result.margin_util_pct
    )
    return result.daily_pnl_jpy


# ── 進捗チェック＆通知 ─────────────────────────────────────────────────────────

_milestone_notified: set[str] = set()  # 通知済みマイルストーン
_portfolio_history: list[float] = []   # 直近ポートフォリオPnL履歴


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
            await push("バックテスト: 連続テスト移行OK", msg, priority=1)
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
            await push("バックテスト進捗", msg, priority=0)
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

        # ── 進捗チェック＆通知（条件達成時のみ） ──
        await _check_milestone(generation, portfolio_pnl)

        # ── 保有時間分析（50Genごと） ──
        if generation % 50 == 0:
            try:
                from backend.backtesting.holding_time import measure_holding_time, get_all_stats
                for r in results[:3]:  # 上位3件のRobustのみ
                    if r.get("robust"):
                        df_ht = await _get_ohlcv(r["symbol"])
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
                    "バックテスト警告",
                    f"Gen{generation}\n" + "\n".join(issues),
                    priority=0,
                ))

        # ── 待機 ──
        await asyncio.sleep(CYCLE_WAIT)


if __name__ == "__main__":
    asyncio.run(main())
