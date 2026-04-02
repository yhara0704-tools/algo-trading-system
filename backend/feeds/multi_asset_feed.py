"""Multi-asset feed — FX, futures, Japanese stocks via yfinance polling."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

import yfinance as yf

logger = logging.getLogger(__name__)

# ── Asset catalogue ────────────────────────────────────────────────────────────
ASSET_CATALOGUE: dict[str, dict] = {
    # ── Crypto (via yfinance as fallback) ──────────────────────────────────────
    "BTC-USD":  {"name": "Bitcoin",       "category": "crypto",  "currency": "USD"},
    "ETH-USD":  {"name": "Ethereum",      "category": "crypto",  "currency": "USD"},
    "SOL-USD":  {"name": "Solana",        "category": "crypto",  "currency": "USD"},
    "XRP-USD":  {"name": "Ripple",        "category": "crypto",  "currency": "USD"},
    # ── FX ─────────────────────────────────────────────────────────────────────
    "USDJPY=X": {"name": "USD/JPY",       "category": "fx",      "currency": "JPY"},
    "EURUSD=X": {"name": "EUR/USD",       "category": "fx",      "currency": "USD"},
    "GBPUSD=X": {"name": "GBP/USD",       "category": "fx",      "currency": "USD"},
    "EURJPY=X": {"name": "EUR/JPY",       "category": "fx",      "currency": "JPY"},
    "AUDUSD=X": {"name": "AUD/USD",       "category": "fx",      "currency": "USD"},
    # ── Futures ────────────────────────────────────────────────────────────────
    "ES=F":     {"name": "S&P500 Fut.",   "category": "futures", "currency": "USD"},
    "NQ=F":     {"name": "Nasdaq100 Fut.","category": "futures", "currency": "USD"},
    "YM=F":     {"name": "DowJones Fut.", "category": "futures", "currency": "USD"},
    "GC=F":     {"name": "Gold Fut.",     "category": "futures", "currency": "USD"},
    "CL=F":     {"name": "Crude Oil Fut.","category": "futures", "currency": "USD"},
    # NK=F 除外: yfinance 1.2.0 で取得不可。^N225（日経225指数）で代替済み
    # ── Japanese Stocks ────────────────────────────────────────────────────────
    "7203.T":   {"name": "Toyota",        "category": "jp_stock","currency": "JPY"},
    "6758.T":   {"name": "Sony",          "category": "jp_stock","currency": "JPY"},
    "9984.T":   {"name": "SoftBank",      "category": "jp_stock","currency": "JPY"},
    "6861.T":   {"name": "Keyence",       "category": "jp_stock","currency": "JPY"},
    "8306.T":   {"name": "MUFG",          "category": "jp_stock","currency": "JPY"},
    "4063.T":   {"name": "Shin-Etsu Chem","category": "jp_stock","currency": "JPY"},
    "^N225":    {"name": "Nikkei 225",    "category": "index",   "currency": "JPY"},
    "^GSPC":    {"name": "S&P 500",       "category": "index",   "currency": "USD"},
    # ── US Stocks ──────────────────────────────────────────────────────────────
    "AAPL":     {"name": "Apple",         "category": "us_stock","currency": "USD"},
    "MSFT":     {"name": "Microsoft",     "category": "us_stock","currency": "USD"},
    "NVDA":     {"name": "NVIDIA",        "category": "us_stock","currency": "USD"},
}

_POLL_INTERVAL = 60  # yfinance is not real-time; poll every 60s
_BATCH_SIZE = 10


class MultiAssetFeed:
    """Poll yfinance for multi-asset prices."""

    def __init__(self, symbols: list[str] | None = None) -> None:
        self.symbols: list[str] = symbols or list(ASSET_CATALOGUE.keys())
        self._callbacks: list[Callable] = []
        self._latest: dict[str, dict] = {}
        self._running = False

    def on_tick(self, cb: Callable) -> None:
        self._callbacks.append(cb)

    def get_latest(self, symbol: str | None = None) -> dict:
        if symbol:
            return self._latest.get(symbol, {})
        return dict(self._latest)

    def get_catalogue(self) -> dict:
        return {
            sym: {**ASSET_CATALOGUE.get(sym, {}), **self._latest.get(sym, {})}
            for sym in self.symbols
        }

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._fetch_all)
            except Exception as exc:
                logger.warning("MultiAsset fetch error: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False

    def _fetch_all(self) -> None:
        """Batch-fetch all symbols."""
        for i in range(0, len(self.symbols), _BATCH_SIZE):
            batch = self.symbols[i : i + _BATCH_SIZE]
            try:
                self._fetch_batch(batch)
            except Exception as exc:
                logger.debug("Batch fetch error %s: %s", batch, exc)

    def _fetch_batch(self, symbols: list[str]) -> None:
        tickers = yf.Tickers(" ".join(symbols))
        ts = time.time()
        for sym in symbols:
            try:
                info = tickers.tickers[sym].fast_info
                price = getattr(info, "last_price", None)
                if price is None or price != price:  # nan check
                    continue
                prev_close = getattr(info, "previous_close", price) or price
                change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
                meta = ASSET_CATALOGUE.get(sym, {})
                data = {
                    "symbol": sym,
                    "price": float(price),
                    "prev_close": float(prev_close),
                    "change_pct": float(change_pct),
                    "name": meta.get("name", sym),
                    "category": meta.get("category", "unknown"),
                    "currency": meta.get("currency", "USD"),
                    "ts": ts,
                    "source": "yfinance",
                }
                self._latest[sym] = data
                for cb in self._callbacks:
                    try:
                        asyncio.ensure_future(cb(data))
                    except Exception:
                        pass
            except Exception as exc:
                logger.debug("Skip %s: %s", sym, exc)

    def get_ohlcv(self, symbol: str, period: str = "1d", interval: str = "5m") -> list[dict]:
        """Fetch historical OHLCV candles synchronously."""
        try:
            df = yf.download(symbol, period=period, interval=interval, progress=False)
            if df is None or df.empty:
                return []
            result = []
            for ts, row in df.iterrows():
                result.append({
                    "time": int(ts.timestamp() * 1000),
                    "open":  float(row["Open"]),
                    "high":  float(row["High"]),
                    "low":   float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row["Volume"]),
                })
            return result
        except Exception as exc:
            logger.warning("OHLCV fetch error %s: %s", symbol, exc)
            return []
