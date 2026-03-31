"""Lab REST endpoints — backtesting & PDCA."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel

from backend.lab.runner import LabRunner, get_btc_strategies, get_jp_strategies
from backend.analysis.time_pattern import get_store as get_pattern_store

router = APIRouter(prefix="/api/lab")
_runner: LabRunner | None = None
_jp_live_runner = None   # set by inject()


def inject(runner: LabRunner, jp_live=None) -> None:
    global _runner, _jp_live_runner
    _runner = runner
    _jp_live_runner = jp_live


@router.get("/strategies")
def list_strategies():
    """All available strategies."""
    all_strats = get_btc_strategies() + get_jp_strategies([])
    results = _runner._results if _runner else {}
    return {"strategies": [
        {**s.meta.__dict__, "has_result": s.meta.id in results}
        for s in all_strats
    ]}


@router.post("/run/{strategy_id}")
async def run_one(strategy_id: str, days: int = 30, usd_jpy: float = 150.0):
    """Run a single backtest."""
    if not _runner:
        raise HTTPException(503, "Lab not ready")
    result = await _runner.run_backtest(strategy_id, days=days, usd_jpy=usd_jpy)
    return result


@router.post("/run-all")
async def run_all(days: int = 30, usd_jpy: float = 150.0):
    """Run all strategies in parallel."""
    if not _runner:
        raise HTTPException(503, "Lab not ready")
    results = await _runner.run_all(days=days, usd_jpy=usd_jpy)
    return {"results": results}


@router.get("/results")
def get_results():
    """All completed backtest results."""
    if not _runner:
        raise HTTPException(503, "Lab not ready")
    return {"results": _runner.get_results(), "running": _runner.get_running()}


@router.get("/results/{strategy_id}")
def get_result(strategy_id: str):
    if not _runner:
        raise HTTPException(503, "Lab not ready")
    r = _runner.get_result(strategy_id)
    if not r:
        raise HTTPException(404, "No result yet")
    return r


@router.get("/pdca")
def get_pdca():
    if not _runner:
        raise HTTPException(503, "Lab not ready")
    return _runner.get_pdca()


@router.get("/regime")
def get_regime():
    """現在の地合い判定結果。"""
    if not _runner:
        raise HTTPException(503, "Lab not ready")
    return {"regime": _runner.get_regime()}


@router.get("/screen")
def get_screen():
    """最新スクリーニング結果（銘柄選定）。"""
    if not _runner:
        raise HTTPException(503, "Lab not ready")
    pdca = _runner.get_pdca()
    return {"screen_results": pdca.get("screen_results", [])}


@router.get("/time-patterns/{symbol}")
def get_time_patterns(symbol: str):
    """銘柄の時間帯別パターン統計を返す。"""
    store = get_pattern_store()
    report = store.get_report(symbol)
    danger = store.get_danger_zones(symbol)
    return {"symbol": symbol, "slots": report, "zones": danger}


@router.get("/time-patterns")
def list_time_patterns():
    """時間帯パターンが蓄積済みの銘柄一覧。"""
    store = get_pattern_store()
    return {"symbols": store.get_all_symbols()}


# ── 戦略知識ベース ────────────────────────────────────────────────────────────

@router.get("/knowledge")
def get_knowledge():
    """全戦略の地合い別実績・洞察メモ一覧。"""
    if not _runner:
        raise HTTPException(503, "Lab not ready")
    return {"knowledge": _runner.get_knowledge()}


@router.get("/knowledge/{strategy_id}")
def get_strategy_knowledge(strategy_id: str):
    """特定戦略の知識（地合い別実績・失敗理由・洞察）。"""
    from backend.analysis.strategy_knowledge import get_kb
    k = get_kb().get_knowledge(strategy_id)
    if not k:
        raise HTTPException(404, "No knowledge yet for this strategy")
    return {
        "strategy_id":    k.strategy_id,
        "strategy_name":  k.strategy_name,
        "total_runs":     k.total_runs,
        "best_regime":    k.best_regime,
        "worst_regime":   k.worst_regime,
        "regime_summary": k.get_regime_summary(),
        "insights":       k.get_insights(),
        "recent_records": k.get_recent_records(20),
    }


@router.get("/regime-map")
def get_regime_map():
    """地合い別に推奨戦略をランキングして返す。"""
    if not _runner:
        raise HTTPException(503, "Lab not ready")
    return {"regime_map": _runner.get_regime_map()}


@router.get("/regime-analysis")
def get_regime_analysis():
    """全銘柄のレジーム別バックテスト結果を返す。"""
    if not _runner:
        raise HTTPException(503, "Lab not ready")
    return {"regime_analysis": _runner.get_regime_analysis()}


@router.post("/regime-analysis/{symbol}")
async def run_regime_analysis(symbol: str, strategy_id: str = ""):
    """指定銘柄のレジーム別バックテストを実行する。"""
    if not _runner:
        raise HTTPException(503, "Lab not ready")
    try:
        result = await _runner.run_regime_analysis(
            symbol, strategy_id=strategy_id or None
        )
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/jp-live/session")
def get_jp_live_session():
    """JP株リアルタイム・ペーパートレードの当日セッションサマリー。"""
    if not _jp_live_runner:
        raise HTTPException(503, "JP live runner not ready")
    return _jp_live_runner.get_session()


@router.get("/history/strategies")
def get_strategy_history(strategy_id: str = "", days: int = 90):
    """戦略の日次パフォーマンス履歴（SQLite）。strategy_id未指定で全戦略最新日。"""
    from backend.storage.db import get_strategy_history, get_all_strategies_latest
    if strategy_id:
        return {"history": get_strategy_history(strategy_id, days)}
    return {"latest": get_all_strategies_latest()}


@router.get("/history/summaries")
def get_daily_summaries(days: int = 30):
    """過去N日分の夜間サマリー履歴（SQLite）。"""
    from backend.storage.db import get_daily_summaries
    return {"summaries": get_daily_summaries(days)}


@router.get("/history/subsessions")
def get_subsessions(date: str = ""):
    """JP株サブセッション履歴（SQLite）。dateなら当日のみ。"""
    from backend.storage.db import get_jp_subsessions
    return {"subsessions": get_jp_subsessions(date or None)}


@router.get("/history/milestone-curve")
def get_milestone_curve():
    """日付別最良PnLの推移（マイルストーンチャート用）。"""
    from backend.storage.db import get_milestone_progress
    return get_milestone_progress()


@router.get("/jquants/status")
async def get_jquants_status():
    """J-Quants API接続状態を確認する。"""
    from backend.feeds.jquants_client import is_available, ping
    if not is_available():
        return {"status": "not_configured", "message": ".envにJQUANTS_API_KEYが未設定"}
    try:
        ok = await ping()
        if ok:
            return {"status": "ok", "message": "J-Quants API接続成功（日足データ利用可能）"}
        return {"status": "error", "message": "接続失敗（APIキーを確認してください）"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/live-readiness")
def get_live_readiness():
    """本番移行チェックリストを返す。"""
    if not _runner:
        raise HTTPException(503, "Lab not ready")
    return _runner.check_live_readiness()


@router.get("/best-params")
def get_best_params():
    """銘柄別最適パラメータ一覧を返す。"""
    if not _runner:
        raise HTTPException(503, "Lab not ready")
    return {"best_params": _runner.get_best_params()}


@router.put("/best-params/{symbol}")
def set_best_params(symbol: str, body: dict):
    """銘柄のパラメータを手動上書きする。
    body: {"ema_fast": 3, "ema_slow": 5, "tp_pct": 0.005, "sl_pct": 0.003,
           "atr_min_pct": 0.001, "allow_short": true}
    """
    from backend.storage.best_params import manual_set
    manual_set(symbol, body)
    return {"status": "ok", "symbol": symbol, "params": body}


@router.get("/capital-plan")
def get_capital_plan():
    """資金計画・マイルストーン一覧を返す。"""
    from backend.lab.runner import (
        INITIAL_CAPITAL_JPY, BTC_CAPITAL_JPY, JP_CAPITAL_JPY,
        CAPITAL_MILESTONES, POSITION_PCT,
    )
    from backend.lab.jp_live_runner import JPLiveRunner
    return {
        "initial_capital_jpy": INITIAL_CAPITAL_JPY,
        "btc_capital_jpy":     BTC_CAPITAL_JPY,
        "jp_capital_jpy":      JP_CAPITAL_JPY,
        "position_pct":        POSITION_PCT,
        "daily_loss_limit_jpy":   JPLiveRunner.DAILY_LOSS_LIMIT_JPY,
        "daily_profit_target_jpy": JPLiveRunner.DAILY_PROFIT_TARGET_JPY,
        "milestones": [
            {
                "name":          m.name,
                "target_jpy":    m.target_jpy,
                "daily_pnl_jpy": m.daily_pnl_jpy,
                "description":   m.description,
                "gain_from_start_pct": (m.target_jpy / INITIAL_CAPITAL_JPY - 1) * 100,
            }
            for m in CAPITAL_MILESTONES
        ],
    }
