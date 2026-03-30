"""Coinbase Advanced Trade WebSocket feed — real-time BTC/crypto prices."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Callable

import websockets

logger = logging.getLogger(__name__)

_WS_URL = "wss://advanced-trade-ws.coinbase.com"
_RECONNECT_DELAY = 5  # seconds


class CoinbaseFeed:
    """Subscribe to Coinbase ticker channel and push price updates."""

    def __init__(self, symbols: list[str] | None = None) -> None:
        self.symbols: list[str] = symbols or ["BTC-USD", "ETH-USD", "SOL-USD"]
        self._callbacks: list[Callable] = []
        self._latest: dict[str, dict] = {}
        # ring buffer of (ts, price) per symbol for OHLCV 5-min
        self._ticks: dict[str, deque] = {s: deque(maxlen=10_000) for s in self.symbols}
        self._running = False

    def on_tick(self, cb: Callable) -> None:
        self._callbacks.append(cb)

    def get_latest(self, symbol: str | None = None) -> dict:
        if symbol:
            return self._latest.get(symbol, {})
        return dict(self._latest)

    def get_ohlcv(self, symbol: str, minutes: int = 60) -> list[dict]:
        """Return OHLCV candles for last `minutes` minutes (5-min bars)."""
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

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._connect()
            except Exception as exc:
                logger.warning("Coinbase WS error: %s — reconnecting in %ds", exc, _RECONNECT_DELAY)
                await asyncio.sleep(_RECONNECT_DELAY)

    async def stop(self) -> None:
        self._running = False

    async def _connect(self) -> None:
        async with websockets.connect(_WS_URL, ping_interval=20, ping_timeout=10) as ws:
            sub_msg = {
                "type": "subscribe",
                "product_ids": self.symbols,
                "channel": "ticker",
            }
            await ws.send(json.dumps(sub_msg))
            logger.info("Coinbase WS subscribed: %s", self.symbols)
            async for raw in ws:
                if not self._running:
                    break
                try:
                    self._handle(json.loads(raw))
                except Exception as exc:
                    logger.debug("Coinbase parse error: %s", exc)

    def _handle(self, msg: dict) -> None:
        channel = msg.get("channel", "")
        if channel != "ticker":
            return
        for event in msg.get("events", []):
            for ticker in event.get("tickers", []):
                sym = ticker.get("product_id")
                price = float(ticker.get("price", 0) or 0)
                if not sym or price == 0:
                    continue
                ts = time.time()
                data = {
                    "symbol": sym,
                    "price": price,
                    "bid": float(ticker.get("best_bid", price) or price),
                    "ask": float(ticker.get("best_ask", price) or price),
                    "volume_24h": float(ticker.get("volume_24_h", 0) or 0),
                    "change_24h": float(ticker.get("price_percent_chg_24_h", 0) or 0),
                    "ts": ts,
                    "source": "coinbase",
                }
                self._latest[sym] = data
                self._ticks[sym].append((ts, price))
                for cb in self._callbacks:
                    try:
                        asyncio.ensure_future(cb(data))
                    except Exception:
                        pass
