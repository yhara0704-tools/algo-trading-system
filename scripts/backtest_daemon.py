"""バックテスト無停止ループデーモン — 「検証することがない」まで完走.

完了条件:
  全58銘柄が以下をすべて完了したとき
  1. MACD×RCI クイックスキャン (IS/OOS)
  2. 6手法横断比較 (IS/OOS)
  3. IS日次プラス銘柄はMACD×RCIフルグリッドサーチ

完了後は翌日の市場データ更新まで待機し、データが新しくなったら再スタート。

実行:
    nohup .venv/bin/python3 scripts/backtest_daemon.py > /tmp/daemon.log 2>&1 &
ログ確認:
    tail -f /tmp/daemon.log
進捗確認:
    cat /root/algo-trading-system/data/daemon_state.json
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import pathlib
import sys
import time
from datetime import date, datetime

sys.path.insert(0, "/root/algo-trading-system")

import pandas as pd

from backend.backtesting.engine import run_backtest
from backend.lab.runner import (
    fetch_ohlcv, JP_CAPITAL_JPY, MARGIN_RATIO, POSITION_PCT, LOT_SIZE, MAX_STOCK_PRICE,
)
from backend.strategies.jp_stock.jp_macd_rci import JPMacdRci
from backend.strategies.jp_stock.jp_breakout import JPBreakout
from backend.strategies.jp_stock.jp_scalp import JPScalp
from backend.strategies.jp_stock.jp_momentum_5min import JPMomentum5Min
from backend.strategies.jp_stock.jp_orb import JPOpeningRangeBreakout
from backend.strategies.jp_stock.jp_vwap import JPVwapReversion
from backend.strategies.jp_stock.pts_screener import PTS_CANDIDATE_POOL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/tmp/daemon_detail.log"),
    ],
)
logger = logging.getLogger(__name__)

# ── ゲート除外銘柄 (v2設定 2026-04-02確定) ──────────────────────────────────
# QD Laser(6613.T): 全設定でWORSE。ノイジーな値動きでSMAフィルターが機能しない。
GATE_EXCLUDE_SYMBOLS: set[str] = {"6613.T"}

# ── 定数 ──────────────────────────────────────────────────────────────────────
DATA_DIR     = pathlib.Path("/root/algo-trading-system/data")
PARAMS_FILE  = DATA_DIR / "macd_rci_params.json"
STRATEGY_FIT = DATA_DIR / "strategy_fit_map.json"
STATE_FILE   = DATA_DIR / "daemon_state.json"
FETCH_DAYS   = 60
MIN_BARS     = 80
MIN_TRADES   = 15

GRID = {
    "tp_pct":        [0.002, 0.003, 0.004, 0.005, 0.006],
    "sl_pct":        [0.001, 0.0015, 0.002, 0.003],
    "rci_min_agree": [1, 2, 3],
    "macd_signal":   [7, 9, 11],
}

ALL_SYMBOLS = [(c["symbol"], c["name"]) for c in PTS_CANDIDATE_POOL]


# ── 状態管理 ───────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _to_python(obj):
    """numpy型など JSON非対応の型を Python標準型に再帰変換する。"""
    import numpy as np
    if isinstance(obj, dict):
        return {k: _to_python(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_python(v) for v in obj]
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    return obj


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(_to_python(state), ensure_ascii=False, indent=2))


def _load_params() -> dict:
    if PARAMS_FILE.exists():
        return json.loads(PARAMS_FILE.read_text())
    return {}


def _save_params(params: dict) -> None:
    PARAMS_FILE.write_text(json.dumps(_to_python(params), ensure_ascii=False, indent=2))


def _load_fit() -> dict:
    if STRATEGY_FIT.exists():
        try:
            return json.loads(STRATEGY_FIT.read_text())
        except Exception:
            pass
    return {}


def _save_fit(fit: dict) -> None:
    STRATEGY_FIT.write_text(json.dumps(_to_python(fit), ensure_ascii=False, indent=2))


# ── バックテスト共通 ───────────────────────────────────────────────────────────

def _run(strat, df):
    return run_backtest(
        strat, df,
        starting_cash=JP_CAPITAL_JPY * MARGIN_RATIO,
        fee_pct=0.0, position_pct=POSITION_PCT,
        usd_jpy=1.0, lot_size=LOT_SIZE,
        limit_slip_pct=0.003, eod_close_time=(15, 20),
    )


def _split(df: pd.DataFrame):
    split = len(df) // 2
    return df.iloc[split:], df.iloc[:split]  # IS=新しい方, OOS=古い方


# ── タスク: クイックスキャン ───────────────────────────────────────────────────

async def task_quick_scan(sym: str, name: str) -> dict | None:
    try:
        df = await fetch_ohlcv(sym, "5m", FETCH_DAYS)
        if df.empty or len(df) < MIN_BARS:
            return {"sym": sym, "skip": "データ不足"}
        price = float(df["close"].iloc[-1])
        if price > MAX_STOCK_PRICE:
            return {"sym": sym, "skip": f"価格超過 {price:.0f}円"}

        df_is, df_oos = _split(df)
        strat = JPMacdRci(sym, name, interval="5m")
        r_is  = _run(strat, df_is)
        r_oos = _run(strat, df_oos)
        is_pos  = float(r_is.daily_pnl_jpy)
        oos_pos = float(r_oos.daily_pnl_jpy)
        robust  = bool(is_pos > 0 and oos_pos > 0)
        verdict = "Robust" if robust else ("IS-ok" if is_pos > 0 else "NG")
        logger.info(f"  [QS] {name}: {verdict}  IS {is_pos:+,.0f} / OOS {oos_pos:+,.0f}  trades={int(r_is.num_trades)}")
        return {
            "sym": sym, "name": name, "price": round(price, 0),
            "is_daily": round(is_pos, 1),
            "is_pf": round(float(r_is.profit_factor), 3),
            "is_trades": int(r_is.num_trades),
            "oos_daily": round(oos_pos, 1),
            "robust": robust,
            "is_positive": bool(is_pos > 0),
        }
    except Exception as e:
        logger.warning("  [QS] %s: エラー %s", sym, e)
        return {"sym": sym, "skip": str(e)}


# ── タスク: 6手法横断比較 ─────────────────────────────────────────────────────

async def task_multi_strategy(sym: str, name: str) -> None:
    try:
        df = await fetch_ohlcv(sym, "5m", FETCH_DAYS)
        if df.empty or len(df) < MIN_BARS:
            return

        df_is, df_oos = _split(df)
        strategies = [
            ("MacdRci",       JPMacdRci(sym, name, interval="5m")),
            ("Breakout",      JPBreakout(sym, name, interval="5m")),
            ("Scalp",         JPScalp(sym, name, interval="5m")),
            ("Momentum5Min",  JPMomentum5Min(sym, name)),
            ("ORB",           JPOpeningRangeBreakout(sym, name)),
            ("VwapReversion", JPVwapReversion(sym, name)),
        ]

        fit = _load_fit()
        fit.setdefault(sym, {"name": name, "updated": str(date.today()), "strategies": {}})

        for strat_name, strat in strategies:
            try:
                r_is  = _run(strat, df_is)
                r_oos = _run(strat, df_oos)
                robust = bool(r_is.daily_pnl_jpy > 0 and r_oos.daily_pnl_jpy > 0)
                fit[sym]["strategies"][strat_name] = {
                    "is_daily":  round(float(r_is.daily_pnl_jpy), 1),
                    "is_pf":     round(float(r_is.profit_factor), 3),
                    "is_trades": int(r_is.num_trades),
                    "oos_daily": round(float(r_oos.daily_pnl_jpy), 1),
                    "robust":    robust,
                }
                _is = float(r_is.daily_pnl_jpy); _oos = float(r_oos.daily_pnl_jpy)
                _v = "Robust" if robust else ("IS-ok" if _is > 0 else "NG")
                logger.info(f"  [MS] {name} x {strat_name}: {_v}  IS {_is:+,.0f} / OOS {_oos:+,.0f}")
            except Exception as e:
                logger.warning("  [MS] %s × %s: エラー %s", name, strat_name, e)

        # ベスト手法を記録
        strats = fit[sym]["strategies"]
        if strats:
            best = max(strats.items(), key=lambda x: x[1]["is_daily"])
            fit[sym]["best_strategy"] = best[0]
            fit[sym]["best_is_daily"] = best[1]["is_daily"]
            fit[sym]["best_robust"] = best[1]["robust"]

        _save_fit(fit)
    except Exception as e:
        logger.warning("  [MS] %s: 全体エラー %s", sym, e)


# ── タスク: グリッドサーチ ────────────────────────────────────────────────────

async def task_grid_search(sym: str, name: str) -> None:
    logger.info("  [GS] %s (%s) グリッドサーチ開始", name, sym)
    try:
        df = await fetch_ohlcv(sym, "5m", FETCH_DAYS)
        if df.empty or len(df) < MIN_BARS:
            return

        df_is, df_oos = _split(df)
        keys   = list(GRID.keys())
        combos = list(itertools.product(*GRID.values()))

        best_robust  = None
        best_is_only = None

        for combo in combos:
            p = dict(zip(keys, combo))
            if p["sl_pct"] >= p["tp_pct"]:
                continue
            strat = JPMacdRci(sym, name, interval="5m",
                              macd_signal=p["macd_signal"],
                              rci_min_agree=p["rci_min_agree"],
                              tp_pct=p["tp_pct"], sl_pct=p["sl_pct"])
            r_is = _run(strat, df_is)
            if r_is.num_trades < MIN_TRADES:
                continue
            r_oos  = _run(strat, df_oos)
            robust = bool(r_is.daily_pnl_jpy > 0 and r_oos.daily_pnl_jpy > 0)
            pyramid = 2 if (robust and r_is.profit_factor >= 1.2) else 0

            entry = dict(
                tp_pct=p["tp_pct"], sl_pct=p["sl_pct"],
                rci_min_agree=p["rci_min_agree"], macd_signal=p["macd_signal"],
                max_pyramid=pyramid,
                is_daily=round(float(r_is.daily_pnl_jpy), 1),
                is_pf=round(float(r_is.profit_factor), 3),
                is_win_rate=round(float(r_is.win_rate), 1),
                oos_daily=round(float(r_oos.daily_pnl_jpy), 1),
                oos_pf=round(float(r_oos.profit_factor), 3),
                oos_win_rate=round(float(r_oos.win_rate), 1),
                robust=robust,
                last_updated=str(date.today()),
            )

            if robust:
                if best_robust is None or r_is.score > _score_of(best_robust, sym, name, df_is):
                    best_robust = entry
            else:
                if best_is_only is None or r_is.score > _score_of(best_is_only or entry, sym, name, df_is):
                    best_is_only = entry

        best = best_robust or best_is_only
        if best:
            tag = "Robust" if best["robust"] else "IS-only"
            logger.info(
                f"  [GS] {name} [{tag}]: tp={best['tp_pct']:.3f} sl={best['sl_pct']:.3f} "
                f"rci={best['rci_min_agree']} sig={best['macd_signal']} "
                f"IS {best['is_daily']:+,.0f} / OOS {best['oos_daily']:+,.0f}"
            )
            params = _load_params()
            params[sym] = best
            _save_params(params)
    except Exception as e:
        logger.warning("  [GS] %s: エラー %s", sym, e)


def _score_of(entry: dict, sym: str, name: str, df_is: pd.DataFrame) -> float:
    try:
        strat = JPMacdRci(sym, name, interval="5m",
                          macd_signal=entry["macd_signal"],
                          rci_min_agree=entry["rci_min_agree"],
                          tp_pct=entry["tp_pct"], sl_pct=entry["sl_pct"])
        return _run(strat, df_is).score
    except Exception:
        return 0.0


# ── 仮説管理 ─────────────────────────────────────────────────────────────────

HYPOTHESES_FILE = DATA_DIR / "lab_hypotheses.json"


def _load_pending_hypotheses() -> list[dict]:
    if not HYPOTHESES_FILE.exists():
        return []
    try:
        hyps = json.loads(HYPOTHESES_FILE.read_text())
        return [h for h in hyps if h.get("status") == "pending"]
    except Exception:
        return []


def _mark_hypothesis(hyp_id: str, status: str, result_summary: str = "") -> None:
    if not HYPOTHESES_FILE.exists():
        return
    try:
        hyps = json.loads(HYPOTHESES_FILE.read_text())
        for h in hyps:
            if h["hypothesis_id"] == hyp_id:
                h["status"] = status
                h["result_summary"] = result_summary
                h["executed_at"] = datetime.now().isoformat()
        HYPOTHESES_FILE.write_text(json.dumps(hyps, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning("仮説ステータス更新失敗: %s", e)


async def execute_hypothesis(hyp: dict) -> None:
    """仮説を実行してステータスを更新する。"""
    hyp_id   = hyp["hypothesis_id"]
    hyp_type = hyp.get("type", "")
    desc     = hyp.get("description", "")
    logger.info(f"  [HYP] {hyp_id}: {desc}")

    try:
        if hyp_type == "strategy_on_symbol":
            # 特定銘柄×特定手法をIS/OOSで検証
            # 過学習防止: IS取引回数が MIN_TRADES 未満なら「サンプル不足」として記録
            symbols = hyp.get("symbols") or ([hyp["symbol"]] if hyp.get("symbol") else [])
            strat_name = hyp.get("strategy", "MacdRci")
            params = hyp.get("params", {})
            results = []
            for sym in symbols:
                name = next((c["name"] for c in PTS_CANDIDATE_POOL if c["symbol"] == sym), sym)
                df = await fetch_ohlcv(sym, "5m", FETCH_DAYS)
                if df.empty or len(df) < MIN_BARS:
                    continue
                df_is, df_oos = _split(df)
                strat = _make_strat(strat_name, sym, name, params)
                if strat is None:
                    continue
                r_is  = _run(strat, df_is)
                r_oos = _run(strat, df_oos)
                is_d = float(r_is.daily_pnl_jpy); oos_d = float(r_oos.daily_pnl_jpy)
                # 過学習防止: IS取引回数が少ない場合は信頼度低として記録
                if r_is.num_trades < MIN_TRADES:
                    verdict = f"LOW_SAMPLE({r_is.num_trades}trades)"
                    logger.info(f"    {name}×{strat_name}: {verdict} — 取引回数不足、結果を信頼しない")
                else:
                    robust = bool(r_is.daily_pnl_jpy > 0 and r_oos.daily_pnl_jpy > 0)
                    verdict = "Robust" if robust else ("IS-ok" if is_d > 0 else "NG")
                    logger.info(f"    {name}×{strat_name}: {verdict}  IS {is_d:+,.0f} / OOS {oos_d:+,.0f}  trades={r_is.num_trades}")
                results.append(f"{name}:{verdict} IS{is_d:+,.0f}/OOS{oos_d:+,.0f}")
            summary = " | ".join(results) if results else "データなし"
            _mark_hypothesis(hyp_id, "done", summary)

        elif hyp_type == "macd_rci_grid":
            # 特定銘柄のグリッドサーチをカスタム範囲で実施
            symbols = hyp.get("symbols") or ([hyp["symbol"]] if hyp.get("symbol") else [])
            for sym in symbols:
                name = next((c["name"] for c in PTS_CANDIDATE_POOL if c["symbol"] == sym), sym)
                await task_grid_search(sym, name)
            _mark_hypothesis(hyp_id, "done", f"{symbols} グリッドサーチ完了")

        elif hyp_type == "multi_symbol_group":
            # 銘柄グループ × 全手法
            symbols = hyp.get("symbols", [])
            for sym in symbols:
                name = next((c["name"] for c in PTS_CANDIDATE_POOL if c["symbol"] == sym), sym)
                await task_multi_strategy(sym, name)
            _mark_hypothesis(hyp_id, "done", f"{symbols} 全手法比較完了")

        else:
            # 未対応タイプは skip
            _mark_hypothesis(hyp_id, "skipped", f"未対応タイプ: {hyp_type}")

    except Exception as e:
        logger.warning(f"  [HYP] {hyp_id} 実行エラー: {e}")
        _mark_hypothesis(hyp_id, "error", str(e))


def _make_strat(strat_name: str, sym: str, name: str, params: dict):
    """手法名と params から戦略インスタンスを生成する。"""
    try:
        if strat_name == "MacdRci":
            return JPMacdRci(sym, name, interval="5m",
                             tp_pct=params.get("tp_pct", 0.003),
                             sl_pct=params.get("sl_pct", 0.001),
                             rci_min_agree=params.get("rci_min_agree", 1),
                             macd_signal=params.get("macd_signal", 9))
        elif strat_name == "Breakout":
            return JPBreakout(sym, name, interval="5m")
        elif strat_name == "Scalp":
            return JPScalp(sym, name, interval="5m",
                           only_slots=params.get("only_slots"),
                           avoid_slots=params.get("avoid_slots"))
        elif strat_name == "Momentum5Min":
            return JPMomentum5Min(sym, name)
        elif strat_name == "ORB":
            return JPOpeningRangeBreakout(sym, name)
        elif strat_name == "VwapReversion":
            return JPVwapReversion(sym, name)
    except Exception as e:
        logger.warning("戦略生成失敗 %s: %s", strat_name, e)
    return None


# ── メインループ ───────────────────────────────────────────────────────────────

async def main():
    DATA_DIR.mkdir(exist_ok=True)
    logger.info("=" * 60)
    logger.info("バックテストデーモン起動 — 全検証完了まで無停止稼働")
    logger.info("=" * 60)

    generation = 0  # データが更新されるたびにインクリメント

    while True:
        generation += 1
        today = str(date.today())
        logger.info("\n[Generation %d] %s データで全検証スタート", generation, today)

        # 進捗トラッキング
        state = _load_state()
        done_qs = set(state.get("done_quick_scan", []))
        done_ms = set(state.get("done_multi_strategy", []))
        done_gs = set(state.get("done_grid_search", []))
        qs_results: dict[str, dict] = state.get("qs_results", {})

        # ── Step 1: クイックスキャン（未完了銘柄のみ）──
        qs_pending = [s for s in ALL_SYMBOLS if s[0] not in done_qs]
        if qs_pending:
            logger.info("[Step1] クイックスキャン残り%d銘柄", len(qs_pending))
        for sym, name in qs_pending:
            result = await task_quick_scan(sym, name)
            if result and not result.get("skip"):
                qs_results[sym] = result
            done_qs.add(sym)
            state.update({"done_quick_scan": list(done_qs), "qs_results": qs_results})
            _save_state(state)

        # ── Step 2: 全銘柄 × 6手法横断比較（未完了のみ）──
        ms_pending = [s for s in ALL_SYMBOLS if s[0] not in done_ms]
        gs_pending = [
            s for s in ALL_SYMBOLS
            if s[0] not in done_gs
            and qs_results.get(s[0], {}).get("is_positive", False)
        ]

        logger.info(
            "[Step2] 手法比較残り%d銘柄 / グリッドサーチ残り%d銘柄",
            len(ms_pending), len(gs_pending)
        )

        # CPU バウンドタスクはasyncio内でも直列実行 → タスクごとにstate保存
        # MS と GS を交互に1件ずつ実行（アイドルタイムゼロ）
        ms_iter = iter(ms_pending)
        gs_iter = iter(gs_pending)
        ms_done_flag = False
        gs_done_flag = False

        while not (ms_done_flag and gs_done_flag):
            # 手法比較1件
            if not ms_done_flag:
                item = next(ms_iter, None)
                if item:
                    sym, name = item
                    await task_multi_strategy(sym, name)
                    done_ms.add(sym)
                    state["done_multi_strategy"] = list(done_ms)
                    _save_state(state)
                else:
                    ms_done_flag = True

            # グリッドサーチ1件
            if not gs_done_flag:
                item = next(gs_iter, None)
                if item:
                    sym, name = item
                    await task_grid_search(sym, name)
                    done_gs.add(sym)
                    state["done_grid_search"] = list(done_gs)
                    _save_state(state)
                else:
                    gs_done_flag = True

        # ── 全検証完了 ──
        total = len(ALL_SYMBOLS)
        params = _load_params()
        robust_count = sum(1 for v in params.values() if v.get("robust"))
        fit = _load_fit()
        fit_count = len(fit)

        logger.info("\n" + "=" * 60)
        logger.info("Generation %d 全検証完了!", generation)
        logger.info("  スキャン済: %d/%d銘柄", len(done_qs), total)
        logger.info("  手法比較済: %d/%d銘柄", len(done_ms), total)
        logger.info("  グリッドサーチ済: %d銘柄", len(done_gs))
        logger.info("  Robust確定パラメータ: %d銘柄", robust_count)
        logger.info("  手法適性マップ: %d銘柄", fit_count)
        logger.info("=" * 60)

        # 完了ログ保存
        state["completed_generations"] = state.get("completed_generations", [])
        state["completed_generations"].append({
            "generation": generation,
            "date": today,
            "completed_at": datetime.now().isoformat(),
            "robust_count": robust_count,
            "fit_count": fit_count,
        })

        # ── Step 3: 手法研究室 — Claude が仮説を生成 ──
        logger.info("[Step3] 手法研究室 起動中 (Claude API)...")
        try:
            from scripts.strategy_lab import run_lab
            new_hypotheses = run_lab()
            logger.info("[Step3] 新仮説 %d件 生成", len(new_hypotheses))
        except Exception as e:
            logger.warning("[Step3] 手法研究室エラー: %s", e)
            new_hypotheses = []

        # ── Step 4: 仮説を実行 ──
        pending_hyps = _load_pending_hypotheses()
        if pending_hyps:
            logger.info("[Step4] 仮説検証 %d件 開始", len(pending_hyps))
            for hyp in pending_hyps:
                await execute_hypothesis(hyp)

        # 翌日データ更新まで待機（状態リセットして再スタート）
        logger.info("手法研究室サイクル完了。30分待機後、新データで再スタート...")
        await asyncio.sleep(1800)

        # 状態をリセット（新データで最初から）
        state["done_quick_scan"]     = []
        state["done_multi_strategy"] = []
        state["done_grid_search"]    = []
        state["qs_results"]          = {}
        _save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
