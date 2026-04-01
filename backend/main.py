"""Algo Trading Terminal — FastAPI backend.

Run:
    uvicorn backend.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import dataclasses

from backend.analysis.spread_analyzer import SpreadAnalyzer
from backend.brokers.paper_broker import PaperBroker
from backend.feeds.coinbase_feed import CoinbaseFeed
from backend.lab.runner import LabRunner
from backend.feeds.multi_asset_feed import MultiAssetFeed
from backend.feeds.polymarket_feed import PolymarketFeed
from backend.feeds.jp_realtime_feed import JPRealtimeFeed, JST
from backend.storage.db import get_db, save_daily_summary, save_pts_screening, prune_old_data, upsert_backtest_agg
from backend.lab.jp_live_runner import JPLiveRunner
from backend.analysis.time_pattern import get_store as get_pattern_store
from backend.prompt_lab.optimizer import PromptOptimizer
from backend.routers import api as api_router
from backend.routers import lab as lab_router
from backend.routers import trading as trading_router
from backend.routers import prompt_lab as prompt_lab_router
from backend.routers import social as social_router
from backend.ws_manager import ConnectionManager

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Algo Trading Terminal", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Feeds & analyzer ──────────────────────────────────────────────────────────
coinbase = CoinbaseFeed(symbols=["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"])
polymarket = PolymarketFeed()
multi_asset = MultiAssetFeed()
spread = SpreadAnalyzer()
manager = ConnectionManager()
from backend.lab.runner import BTC_CAPITAL_USD, JP_CAPITAL_JPY
paper_broker    = PaperBroker(starting_cash=BTC_CAPITAL_USD)   # BTC用 ~667 USD (10万円)
jp_paper_broker = PaperBroker(starting_cash=JP_CAPITAL_JPY)    # JP株用 10万円
jp_feed         = JPRealtimeFeed(poll_interval=60)
jp_live_runner  = JPLiveRunner(broker=jp_paper_broker)
lab_runner      = LabRunner()
prompt_optimizer = PromptOptimizer()

# ── Static files ───────────────────────────────────────────────────────────────
_FRONTEND = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(_FRONTEND / "static")), name="static")


@app.get("/")
async def root():
    return FileResponse(str(_FRONTEND / "index.html"))


@app.get("/lab")
async def lab():
    return FileResponse(str(_FRONTEND / "lab.html"))


@app.get("/prompt-lab")
async def prompt_lab_page():
    return FileResponse(str(_FRONTEND / "prompt_lab.html"))


@app.get("/milestones")
async def milestones_page():
    return FileResponse(str(_FRONTEND / "milestones.html"))


# ── WebSocket hub ──────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        # Send initial state
        await ws.send_json({
            "type": "init",
            "coinbase": coinbase.get_latest(),
            "multi": multi_asset.get_latest(),
            "spread": spread.get_latest(),
            "polymarket": polymarket.get_latest(),
            "ts": time.time(),
        })
        # Keep alive — receive ping/subscribe messages from client
        while True:
            try:
                data = await asyncio.wait_for(ws.receive_json(), timeout=30)
                if data.get("type") == "subscribe":
                    # Client subscribing to specific symbols — acknowledged
                    await ws.send_json({"type": "subscribed", "symbols": data.get("symbols", [])})
                elif data.get("type") == "ohlcv_request":
                    sym = data.get("symbol", "BTC-USD")
                    candles = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: multi_asset.get_ohlcv(sym)
                    )
                    await ws.send_json({"type": "ohlcv", "symbol": sym, "candles": candles})
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping", "ts": time.time()})
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)


# ── Feed callbacks → broadcast ────────────────────────────────────────────────
async def _on_coinbase_tick(data: dict) -> None:
    if data.get("symbol") == "BTC-USD":
        spread.update_coinbase(data["price"])
    paper_broker.mark_to_market({data["symbol"]: data["price"]})
    await manager.broadcast({"type": "tick", "feed": "coinbase", **data})


async def _on_polymarket_tick(data: dict) -> None:
    implied = data.get("implied_btc")
    if implied:
        spread.update_polymarket(implied)
    await manager.broadcast({"type": "polymarket", **data})


async def _on_multi_tick(data: dict) -> None:
    await manager.broadcast({"type": "tick", "feed": "multi", **data})


async def _spread_broadcast_loop() -> None:
    """Broadcast spread snapshot every 5 seconds."""
    while True:
        await asyncio.sleep(5)
        snap = spread.get_latest()
        if snap:
            await manager.broadcast({"type": "spread", **snap})


# ── Router ─────────────────────────────────────────────────────────────────────
api_router.inject(coinbase, polymarket, multi_asset, spread)
app.include_router(api_router.router)

def _get_price(symbol: str) -> float | None:
    data = coinbase.get_latest(symbol)
    if data:
        return data.get("price")
    data = multi_asset.get_latest(symbol)
    return data.get("price") if data else None

trading_router.inject(paper_broker, _get_price)
app.include_router(trading_router.router)

lab_router.inject(lab_runner, jp_live_runner)
app.include_router(lab_router.router)

prompt_lab_router.inject(prompt_optimizer)
app.include_router(prompt_lab_router.router)
app.include_router(social_router.router)


# ── Lifecycle ──────────────────────────────────────────────────────────────────
async def _on_order_fill(order, account) -> None:
    await manager.broadcast({
        "type": "trade_update",
        "order":   dataclasses.asdict(order),
        "account": dataclasses.asdict(account),
    })


@app.on_event("startup")
async def startup():
    coinbase.on_tick(_on_coinbase_tick)
    polymarket.on_tick(_on_polymarket_tick)
    multi_asset.on_tick(_on_multi_tick)
    paper_broker.on_fill(_on_order_fill)
    lab_runner._on_strategy_done = manager.broadcast

    # DB初期化 + 古いRunRecordをSQLiteへ移行
    get_db()
    try:
        from backend.storage.db import migrate_knowledge_base_records
        n = migrate_knowledge_base_records()
        if n:
            logger.info("Migrated %d old RunRecords to SQLite", n)
    except Exception as e:
        logger.warning("DB migration skipped: %s", e)

    # JP株リアルタイム設定
    from backend.notify import push as _push
    jp_live_runner._notify = _push
    jp_live_runner.set_feed(jp_feed)
    asyncio.create_task(jp_feed.run())
    asyncio.create_task(jp_live_runner.run())
    asyncio.create_task(jp_live_runner.run_scalp_loop())   # 1分足スキャルピング
    asyncio.create_task(_jp_live_symbol_sync_loop())

    asyncio.create_task(coinbase.run())
    asyncio.create_task(polymarket.run())
    asyncio.create_task(multi_asset.run())
    asyncio.create_task(_spread_broadcast_loop())
    asyncio.create_task(_btc_fast_loop())
    asyncio.create_task(_jp_slow_loop())
    asyncio.create_task(_pts_nightly_loop())
    asyncio.create_task(_prompt_opt_loop())
    asyncio.create_task(_pattern_save_loop())
    asyncio.create_task(_evening_summary_loop())
    asyncio.create_task(_weekly_prune_loop())
    asyncio.create_task(_regime_analysis_loop())
    logger.info("All feeds started.")


async def _btc_fast_loop() -> None:
    """BTC戦略専用の高速サイクル — 起動8秒後に開始、以降10分ごと。
    期間ローテーション: 7→14→30→14→7→30 日
    """
    await asyncio.sleep(8)
    day_schedule = [7, 14, 30, 14, 7, 30]
    cycle = 0
    while True:
        days  = day_schedule[cycle % len(day_schedule)]
        cycle += 1
        try:
            results = await lab_runner.run_btc_only(days=days, usd_jpy=150.0)
            pdca    = lab_runner.get_pdca()
            summary = _build_lab_summary(results, pdca)
            await manager.broadcast({"type": "lab_report", **summary})
            logger.info("BTC cycle #%d done. Stage=%d Best=%.0f円/日 [%s]",
                        cycle, pdca["current_stage"],
                        summary["best_daily_jpy"], summary["best_strategy"])
        except Exception as exc:
            logger.error("BTC loop error: %s", exc)
        await asyncio.sleep(15 * 60)  # 15分ごと（JP優先のため緩和）


async def _jp_slow_loop() -> None:
    """JP株スクリーニング + バックテストのサイクル — 起動2分後に開始、以降3分ごと。
    J-Quants有料プラン（5分足）+ OHLCVキャッシュにより高頻度化。
    期間ローテーション: 14→30→60 日
    """
    await asyncio.sleep(2 * 60)  # 2分後に開始（BTCが先に動いてから）
    day_schedule = [14, 30, 60]
    cycle = 0
    while True:
        days  = day_schedule[cycle % len(day_schedule)]
        cycle += 1
        logger.info("JP cycle #%d start (days=%d)", cycle, days)
        try:
            results = await lab_runner.run_all(days=days, usd_jpy=150.0)
            pdca    = lab_runner.get_pdca()
            summary = _build_lab_summary(results, pdca)
            await manager.broadcast({"type": "lab_report", **summary})
            logger.info("JP cycle #%d done. Stage=%d Best=%.0f円/日 [%s]",
                        cycle, pdca["current_stage"],
                        summary["best_daily_jpy"], summary["best_strategy"])
            # 自動分析をブロードキャスト
            analysis = lab_runner.generate_analysis(results)
            await manager.broadcast({"type": "analysis_update", "text": analysis})
            # 実験ステータスをブロードキャスト
            exp_status = lab_runner.get_experiment_status()
            await manager.broadcast({"type": "experiment_update", **exp_status})
            # 知識ベース保存
            from backend.analysis.strategy_knowledge import get_kb
            get_kb().save()
        except Exception as exc:
            logger.error("JP loop error: %s", exc)
        await asyncio.sleep(3 * 60)  # 3分ごと（J-Quants+OHLCVキャッシュで高頻度化）


def _build_lab_summary(results: list[dict], pdca: dict) -> dict:
    valid = [r for r in results if r.get("status") == "done" and r.get("num_trades", 0) > 0]
    if not valid:
        return {"best_strategy": "—", "best_daily_jpy": 0,
                "pdca_stage": pdca.get("current_stage", 1),
                "next_action": pdca.get("next_action", ""),
                "strategies": []}
    best = max(valid, key=lambda r: r.get("score", 0))
    return {
        "best_strategy":  best["strategy_name"],
        "best_daily_jpy": best["daily_pnl_jpy"],
        "best_win_rate":  best["win_rate"],
        "pdca_stage":     pdca.get("current_stage", 1),
        "next_action":    pdca.get("next_action", ""),
        "strategies": [
            {"name": r["strategy_name"], "daily_pnl_jpy": r["daily_pnl_jpy"],
             "win_rate": r["win_rate"], "score": r["score"], "status": r.get("status")}
            for r in results
        ],
    }


async def _pattern_save_loop() -> None:
    """1時間ごとに時間帯パターンデータをディスクに保存。"""
    while True:
        await asyncio.sleep(60 * 60)
        try:
            get_pattern_store().save_all()
            logger.info("Time patterns saved.")
        except Exception as e:
            logger.warning("Pattern save error: %s", e)


async def _pts_nightly_loop() -> None:
    """毎営業日16:00 JST（大引け30分後）にPTSスクリーニングを実行して翌日監視銘柄を準備する。
    起動直後にも1回実行して即座に銘柄リストを補充する。
    """
    from backend.strategies.jp_stock.pts_screener import pts_screen
    from backend.notify import push as _push

    # 起動後まずすぐに1回実行（前日データを使って初期リストを作る）
    await asyncio.sleep(30)
    try:
        pts = await pts_screen(top_n=3)
        candidates = [r for r in pts if r.selected]
        logger.info("Initial PTS screen: %s", [r.symbol for r in candidates])
    except Exception as e:
        logger.warning("Initial PTS screen failed: %s", e)

    while True:
        # 次の16:00 JSTまで待機
        now = datetime.now(JST)
        target = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        # 土日はスキップ
        while target.weekday() >= 5:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        logger.info("PTS nightly: next run in %.0fm (at %s JST)",
                    wait / 60, target.strftime("%m/%d %H:%M"))
        await asyncio.sleep(wait)

        try:
            pts = await pts_screen(top_n=3)
            candidates = [r for r in pts if r.selected]
            syms = [r.symbol for r in candidates]

            # PTS結果をDBに保存
            try:
                save_pts_screening(target.strftime("%Y-%m-%d"), [
                    {"symbol": r.symbol, "name": r.name, "sector": r.sector,
                     "prev_volume_ratio": r.prev_volume_ratio, "prev_range_pct": r.prev_range_pct,
                     "signal": r.signal, "pts_score": r.pts_score, "selected": r.selected}
                    for r in pts
                ])
            except Exception as e:
                logger.warning("PTS DB save error: %s", e)

            # 既存の監視銘柄リストにPTS銘柄を追加
            existing = set(jp_feed._symbols)
            new_syms = [s for s in syms if s not in existing]
            if new_syms:
                jp_feed.set_symbols(list(existing | set(new_syms)))

            msg_lines = [f"📡 翌日PTS候補 ({target.strftime('%m/%d')})"]
            for r in candidates:
                msg_lines.append(
                    f"  {r.name}（{r.symbol}）"
                    f" 出来高x{r.prev_volume_ratio:.1f} 値幅{r.prev_range_pct:.1f}%"
                    f" [{r.signal}]"
                )
            msg = "\n".join(msg_lines)
            logger.info(msg)
            await _push("PTS翌日候補", msg)

            # WebSocket でフロントにも通知
            await manager.broadcast({
                "type": "pts_candidates",
                "date": target.strftime("%Y-%m-%d"),
                "candidates": [
                    {"symbol": r.symbol, "name": r.name, "signal": r.signal,
                     "vol_ratio": r.prev_volume_ratio, "range_pct": r.prev_range_pct}
                    for r in candidates
                ],
            })
        except Exception as e:
            logger.error("PTS nightly loop error: %s", e)


async def _jp_live_symbol_sync_loop() -> None:
    """JP株スクリーナー結果をリアルタイムフィードに反映する。
    JPスロー・サイクルが走るたびにスクリーニング済み銘柄と戦略が更新されるので
    5分ごとに lab_runner からピックアップして jp_feed / jp_live_runner に渡す。
    """
    await asyncio.sleep(90)  # lab_runner が初回スクリーニングを終えるまで待つ
    while True:
        try:
            # LabRunner が保持している JP 戦略リストを取得
            jp_strategies = lab_runner.get_live_jp_strategies()
            if jp_strategies:
                symbols = list({s.meta.symbol for s in jp_strategies})
                jp_feed.set_symbols(symbols)
                jp_live_runner.set_strategies(jp_strategies)
                logger.info("JP live: synced %d symbols / %d strategies",
                            len(symbols), len(jp_strategies))
        except Exception as e:
            logger.warning("JP live symbol sync error: %s", e)
        await asyncio.sleep(5 * 60)  # 5分ごと


async def _weekly_prune_loop() -> None:
    """毎週月曜0時にSQLiteの古いデータを削除してファイルサイズを圧縮する。
    起動直後に1回 migrate も実行する。
    """
    while True:
        now = datetime.now(JST)
        # 次の月曜0:00まで待機
        days_until_mon = (7 - now.weekday()) % 7 or 7
        next_mon = (now + timedelta(days=days_until_mon)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        wait = (next_mon - now).total_seconds()
        logger.info("Weekly prune scheduled in %.0fh", wait / 3600)
        await asyncio.sleep(wait)
        try:
            deleted = prune_old_data()
            logger.info("Weekly prune done: %s", deleted)
        except Exception as e:
            logger.error("Weekly prune error: %s", e)


def _write_bii_daily(today: str, phase: str,
                     pnl_today: float, pnl_cumulative: float,
                     trade_count: int, win_count: int) -> None:
    """BII連携用日次JSONを /root/algo_shared/daily/YYYY-MM-DD.json に書き込む。
    書き込み前に検閲モジュールでホワイトリスト適用・バリデーション実施。
    """
    import json, pathlib
    from backend.bii.censor import sanitize_daily_json

    out_dir = pathlib.Path("/root/algo_shared/daily")
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = {
        "date":               today,
        "phase":              phase,
        "pnl_today_jpy":      round(pnl_today),
        "pnl_cumulative_jpy": round(pnl_cumulative),
        "trade_count":        trade_count,
        "win_count":          win_count,
    }
    try:
        payload = sanitize_daily_json(raw)
    except ValueError as e:
        logger.error("BII censor blocked daily write: %s", e)
        return

    path = out_dir / f"{today}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    logger.info("BII daily JSON written (censored): %s", path)


async def _evening_summary_loop() -> None:
    """毎日19:00〜21:00の間にその日の検証結果サマリーをPushoverで送信する。"""
    from backend.notify import push as _push

    sent_today: str = ""   # 送信済み日付
    bii_written_today: str = ""  # BII JSON書き込み済み日付

    while True:
        now   = datetime.now(JST)
        today = now.strftime("%Y-%m-%d")

        # 15:30以降・平日・本日未書き込み → BII日次JSON書き込み
        if (now.hour * 60 + now.minute >= 15 * 60 + 30
                and now.weekday() < 5
                and bii_written_today != today):
            try:
                results = lab_runner.get_results()
                jp_done = [r for r in results
                           if r.get("num_trades", 0) > 0
                           and r.get("symbol", "").endswith(".T")]
                jp_session = jp_live_runner.get_session() if jp_live_runner else None
                # phase判定
                if jp_session and jp_session.get("num_trades", 0) > 0:
                    phase = "paper"
                    pnl_today   = jp_session.get("total_pnl", 0)
                    trade_count = jp_session.get("num_trades", 0)
                    win_count   = jp_session.get("win_count", 0)
                else:
                    phase = "backtest"
                    best = max(jp_done, key=lambda r: r.get("score", 0)) if jp_done else {}
                    pnl_today   = best.get("daily_pnl_jpy", 0)
                    trade_count = best.get("num_trades", 0)
                    win_count   = int(trade_count * best.get("win_rate", 0) / 100) if best else 0
                # 累積損益: DBから取得
                from backend.storage.db import get_daily_best_pnl
                history = get_daily_best_pnl(days=365)
                pnl_cumulative = sum(v for _, v in history) + pnl_today
                _write_bii_daily(today, phase, pnl_today, pnl_cumulative, trade_count, win_count)
                bii_written_today = today
            except Exception as e:
                logger.error("BII daily write error: %s", e)

        # 19:00〜21:00 かつ本日未送信
        if 19 <= now.hour < 21 and sent_today != today:
            try:
                jp_session = jp_live_runner.get_session() if jp_live_runner else None
                msg        = lab_runner.build_daily_summary(jp_session)

                # DB保存
                results   = lab_runner.get_results()
                done      = [r for r in results if r.get("num_trades", 0) > 0]
                positive  = [r for r in done if r.get("daily_pnl_jpy", 0) > 0]
                best      = max(done, key=lambda r: r.get("score", 0)) if done else {}
                regime    = lab_runner.get_regime().get("BTC-USD", {}).get("regime", "")
                save_daily_summary(today, msg, {
                    "total_strategies":    len(done),
                    "positive_strategies": len(positive),
                    "best_strategy":       best.get("strategy_name", ""),
                    "best_pnl_jpy":        best.get("daily_pnl_jpy", 0),
                    "btc_regime":          regime,
                    "jp_session_pnl":      (jp_session or {}).get("total_pnl", 0),
                })
                # 当日の全バックテスト結果をDBに保存
                for r in done:
                    upsert_backtest_agg(r, regime=regime)

                await _push(f"日次サマリー {today}", msg)
                sent_today = today
                logger.info("Evening summary sent and saved to DB.")
            except Exception as e:
                logger.error("Evening summary error: %s", e)

        await asyncio.sleep(5 * 60)   # 5分ごとにチェック


async def _prompt_opt_loop() -> None:
    """起動60分後に初回実行、以降6時間ごとに全エージェントのプロンプト最適化を実行。"""
    await asyncio.sleep(60 * 60)  # 1時間後に開始（Lab が先に安定してから）
    while True:
        logger.info("Prompt optimization loop starting...")
        try:
            await prompt_optimizer.run_all()
            logger.info("Prompt optimization complete.")
        except Exception as e:
            logger.error("Prompt optimization error: %s", e)
        await asyncio.sleep(6 * 60 * 60)  # 6時間ごと


async def _regime_analysis_loop() -> None:
    """毎朝9:15に監視銘柄のレジーム別バックテストを自動実行する。
    J-Quants日足で相場分類 + 5m足（市場開放直後）で実バックテスト。
    結果は lab_runner._regime_analysis に格納 → /api/lab/regime-analysis で取得可能。
    """
    # 監視12銘柄
    WATCHLIST = [
        "2413.T", "3697.T", "7267.T", "6645.T", "4568.T", "9432.T",
        "7203.T", "9433.T", "8306.T", "6758.T", "6098.T", "6954.T",
    ]

    ran_today: str = ""

    while True:
        now   = datetime.now(JST)
        today = now.strftime("%Y-%m-%d")

        # 9:15〜10:00 かつ平日 かつ本日未実行
        if (9 * 60 + 15 <= now.hour * 60 + now.minute < 10 * 60
                and now.weekday() < 5
                and ran_today != today):
            ran_today = today
            logger.info("レジーム分析ループ開始: %s", today)
            for symbol in WATCHLIST:
                try:
                    await lab_runner.run_regime_analysis(symbol)
                    logger.info("レジーム分析完了: %s", symbol)
                    await asyncio.sleep(3)   # レート制限対策
                except Exception as e:
                    logger.warning("レジーム分析失敗 %s: %s", symbol, e)
            logger.info("全銘柄レジーム分析完了")
        else:
            # 次の9:15まで待機
            target = now.replace(hour=9, minute=15, second=0, microsecond=0)
            if now.hour * 60 + now.minute >= 9 * 60 + 15:
                target += timedelta(days=1)
            # 土日スキップ
            while target.weekday() >= 5:
                target += timedelta(days=1)
            wait = max(60, (target - now).total_seconds())
            logger.debug("次のレジーム分析: %s (%.0f分後)", target.strftime("%m/%d %H:%M"), wait / 60)
            await asyncio.sleep(min(wait, 15 * 60))  # 最大15分ごとにチェック


@app.on_event("shutdown")
async def shutdown():
    await coinbase.stop()
    await polymarket.stop()
    await multi_asset.stop()
