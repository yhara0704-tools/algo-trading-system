"""Price feed — real-time BTC/crypto prices via Binance public WebSocket.

Coinbase Advanced Trade WS requires JWT auth since 2023, so we use Binance's
public combined stream which needs no authentication.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Callable

import httpx
import websockets

logger = logging.getLogger(__name__)

# Binance public endpoints — no auth needed
_BINANCE_WS_BASE = "wss://stream.binance.com:9443/stream"
_BINANCE_REST_BASE = "https://api.binance.com/api/v3"
_RECONNECT_DELAY = 5  # seconds

# Map internal symbols (Coinbase style) -> Binance symbols
_SYMBOL_MAP = {
    "BTC-USD": "btcusdt",
    "ETH-USD": "ethusdt",
    "SOL-USD": "solusdt",
    "XRP-USD": "xrpusdt",
}
_REVERSE_MAP = {v: k for k, v in _SYMBOL_MAP.items()}

# Binance interval strings
_INTERVAL_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "1d": "1d",
}
# (period, interval) -> limit
_PERIOD_LIMIT: dict[tuple[str, str], int] = {
    ("1d",  "1m"):  1000,
    ("5d",  "5m"):  1000,
    ("1mo", "15m"): 1000,
    ("1mo", "1h"):  720,
    ("1mo", "1d"):  30,
}


class CoinbaseFeed:
    """Subscribe to price ticker and push price updates.

    Kept as CoinbaseFeed for interface compatibility; uses Binance public WS.
    """

    def __init__(self, symbols: list[str] | None = None) -> None:
        self.symbols: list[str] = symbols or ["BTC-USD", "ETH-USD", "SOL-USD"]
        self._callbacks: list[Callable] = []
        self._latest: dict[str, dict] = {}
        self._ticks: dict[str, deque] = {s: deque(maxlen=10_000) for s in self.symbols}
        self._running = False

    def on_tick(self, cb: Callable) -> None:
        self._callbacks.append(cb)

    def get_latest(self, symbol: str | None = None) -> dict:
        if symbol:
            return self._latest.get(symbol, {})
        return dict(self._latest)

    def get_ohlcv_live(self, symbol: str, minutes: int = 60) -> list[dict]:
        """Return OHLCV candles from in-memory ring buffer (recent ticks only)."""
        now = time.time()
        cutoff = now - minutes * 60
        ticks = [(ts, p) for ts, p in self._ticks.get(symbol, []) if ts >= cutoff]
        if not ticks:
            return []
        candles: list[dict] = []
        bar_seconds = 300  # 5-min
        start = int(ticks[0][0] // bar_seconds) * bar_seconds
        end = int(now // bar_seconds) * bar_seconds + bar_seconds
        for bar_start in range(start, end, bar_seconds):
            bar_end = bar_start + bar_seconds
            prices = [p for ts, p in ticks if bar_start <= ts < bar_end]
            if prices:
                candles.append({
                    "time": bar_start * 1000,
                    "open": prices[0],
                    "high": max(prices),
                    "low": min(prices),
                    "close": prices[-1],
                })
        return candles

    async def get_ohlcv(self, symbol: str, period: str = "1d", interval: str = "5m") -> list[dict]:
        """Fetch historical OHLCV from Binance REST API."""
        binance_sym = _SYMBOL_MAP.get(symbol, "").upper()
        if not binance_sym:
            return []
        bi = _INTERVAL_MAP.get(interval, "5m")
        limit = _PERIOD_LIMIT.get((period, bi), 500)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{_BINANCE_REST_BASE}/klines",
                    params={"symbol": binance_sym, "interval": bi, "limit": limit},
                )
                resp.raise_for_status()
                rows = resp.json()
            candles = []
            for row in rows:
                candles.append({
                    "time":   int(row[0]),           # open time ms
                    "open":   float(row[1]),
                    "high":   float(row[2]),
                    "low":    float(row[3]),
                    "close":  float(row[4]),
                    "volume": float(row[5]),
                })
            return candles
        except Exception as exc:
            logger.warning("Binance OHLCV fetch error %s: %s", symbol, exc)
            return []

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._connect()
            except Exception as exc:
                logger.warning("Price WS error: %s (%s) — reconnecting in %ds", exc, type(exc).__name__, _RECONNECT_DELAY)
                await asyncio.sleep(_RECONNECT_DELAY)

    async def stop(self) -> None:
        self._running = False

    async def _connect(self) -> None:
        streams = "/".join(
            f"{_SYMBOL_MAP[s]}@ticker"
            for s in self.symbols
            if s in _SYMBOL_MAP
        )
        url = f"{_BINANCE_WS_BASE}?streams={streams}"
        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            logger.info("Binance WS subscribed: %s", self.symbols)
            async for raw in ws:
                if not self._running:
                    break
                try:
                    self._handle(json.loads(raw))
                except Exception as exc:
                    logger.debug("Price WS parse error: %s", exc)

    def _handle(self, msg: dict) -> None:
        # Binance combined stream wraps in {"stream": "...", "data": {...}}
        data = msg.get("data", msg)
        event_type = data.get("e", "")
        if event_type != "24hrTicker":
            return

        binance_sym = data.get("s", "").lower()
        sym = _REVERSE_MAP.get(binance_sym)
        if not sym:
            return

        price = float(data.get("c", 0) or 0)
        if price == 0:
            return

        ts = time.time()
        tick = {
            "symbol": sym,
            "price": price,
            "bid": float(data.get("b", price) or price),
            "ask": float(data.get("a", price) or price),
            "volume_24h": float(data.get("v", 0) or 0),
            "change_24h": float(data.get("P", 0) or 0),  # percent change
            "ts": ts,
            "source": "binance",
        }
        self._latest[sym] = tick
        self._ticks[sym].append((ts, price))
        for cb in self._callbacks:
            try:
                asyncio.ensure_future(cb(tick))
            except Exception:
                pass
