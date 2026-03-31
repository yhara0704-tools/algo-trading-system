"""REST API endpoints."""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api")


# These are injected by main.py at startup
_coinbase_feed = None
_polymarket_feed = None
_multi_feed = None
_spread_analyzer = None


def inject(cb, pm, ma, sa) -> None:
    global _coinbase_feed, _polymarket_feed, _multi_feed, _spread_analyzer
    _coinbase_feed = cb
    _polymarket_feed = pm
    _multi_feed = ma
    _spread_analyzer = sa


@router.get("/assets")
async def get_assets():
    """All asset catalogue with latest prices."""
    catalogue = _multi_feed.get_catalogue() if _multi_feed else {}
    # Merge coinbase data (higher quality for crypto)
    if _coinbase_feed:
        for sym, data in _coinbase_feed.get_latest().items():
            if sym in catalogue:
                catalogue[sym].update(data)
    return {"assets": catalogue}


@router.get("/price/{symbol:path}")
async def get_price(symbol: str):
    """Latest price for a symbol."""
    # Try Coinbase first (crypto)
    if _coinbase_feed:
        data = _coinbase_feed.get_latest(symbol.upper())
        if data:
            return data
    # Fallback to multi-asset
    if _multi_feed:
        data = _multi_feed.get_latest(symbol.upper())
        if data:
            return data
    raise HTTPException(404, f"Symbol {symbol} not found")


@router.get("/ohlcv/{symbol:path}")
async def get_ohlcv(
    symbol: str,
    period: str = Query("1d", description="yfinance period: 1d, 5d, 1mo"),
    interval: str = Query("5m", description="yfinance interval: 1m, 5m, 15m, 1h"),
):
    """OHLCV candle data."""
    sym = symbol.upper()
    # For crypto: always use Binance REST API for full historical data
    if _coinbase_feed and sym in ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"):
        candles = await _coinbase_feed.get_ohlcv(sym, period=period, interval=interval)
        if candles:
            return {"symbol": sym, "candles": candles, "source": "binance"}
        # fallthrough if Binance fails
    # yfinance for everything else (FX, futures, stocks)
    if _multi_feed:
        candles = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _multi_feed.get_ohlcv(sym, period=period, interval=interval)
        )
        return {"symbol": sym, "candles": candles, "source": "yfinance"}
    raise HTTPException(404, "OHLCV unavailable")


@router.get("/spread")
async def get_spread():
    """Coinbase vs Polymarket BTC spread analysis."""
    if not _spread_analyzer:
        raise HTTPException(503, "Spread analyzer not ready")
    return {
        "latest": _spread_analyzer.get_latest(),
        "stats": _spread_analyzer.get_stats(),
        "history": _spread_analyzer.get_history(50),
    }


@router.get("/polymarket/markets")
async def get_polymarket_markets():
    """Active Polymarket BTC prediction markets."""
    if not _polymarket_feed:
        raise HTTPException(503, "Polymarket feed not ready")
    return {
        "markets": _polymarket_feed.get_markets(),
        "latest": _polymarket_feed.get_latest(),
    }


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "coinbase_symbols": list(_coinbase_feed.get_latest().keys()) if _coinbase_feed else [],
        "multi_assets": len(_multi_feed.get_latest()) if _multi_feed else 0,
        "polymarket_markets": len(_polymarket_feed.get_markets()) if _polymarket_feed else 0,
    }
