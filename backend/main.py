"""Algo Trading Terminal — FastAPI backend.

Run:
    uvicorn backend.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.analysis.spread_analyzer import SpreadAnalyzer
from backend.feeds.coinbase_feed import CoinbaseFeed
from backend.feeds.multi_asset_feed import MultiAssetFeed
from backend.feeds.polymarket_feed import PolymarketFeed
from backend.routers import api as api_router
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

# ── Static files ───────────────────────────────────────────────────────────────
_FRONTEND = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(_FRONTEND / "static")), name="static")


@app.get("/")
async def root():
    return FileResponse(str(_FRONTEND / "index.html"))


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
    # Update spread analyzer
    if data.get("symbol") == "BTC-USD":
        spread.update_coinbase(data["price"])
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


# ── Lifecycle ──────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    coinbase.on_tick(_on_coinbase_tick)
    polymarket.on_tick(_on_polymarket_tick)
    multi_asset.on_tick(_on_multi_tick)

    asyncio.create_task(coinbase.run())
    asyncio.create_task(polymarket.run())
    asyncio.create_task(multi_asset.run())
    asyncio.create_task(_spread_broadcast_loop())
    logger.info("All feeds started.")


@app.on_event("shutdown")
async def shutdown():
    await coinbase.stop()
    await polymarket.stop()
    await multi_asset.stop()
