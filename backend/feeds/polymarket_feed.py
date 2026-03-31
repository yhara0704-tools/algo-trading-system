"""Polymarket feed — BTC 5-min price prediction market data.

Polymarket hosts binary prediction markets. This module polls the Gamma API
to find active BTC price markets (e.g. "BTC above $X at 5-min mark") and
fetches current YES/NO probabilities, which we convert into an implied price
to compare with Coinbase spot.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_CLOB_BASE = "https://clob.polymarket.com"
_POLL_INTERVAL = 30  # seconds


class PolymarketFeed:
    """Poll Polymarket for BTC prediction market data."""

    def __init__(self) -> None:
        self._callbacks: list[Callable] = []
        self._latest: dict = {}
        self._markets: list[dict] = []
        self._running = False

    def on_tick(self, cb: Callable) -> None:
        self._callbacks.append(cb)

    def get_latest(self) -> dict:
        return dict(self._latest)

    def get_markets(self) -> list[dict]:
        return list(self._markets)

    async def run(self) -> None:
        self._running = True
        async with httpx.AsyncClient(timeout=15) as client:
            await self._refresh_markets(client)
            while self._running:
                try:
                    await self._poll(client)
                except Exception as exc:
                    logger.warning("Polymarket poll error: %s", exc)
                await asyncio.sleep(_POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False

    async def _refresh_markets(self, client: httpx.AsyncClient) -> None:
        """Find active BTC price markets."""
        try:
            resp = await client.get(
                f"{_GAMMA_BASE}/markets",
                params={"active": "true", "closed": "false", "limit": 100},
            )
            resp.raise_for_status()
            all_markets = resp.json()
            logger.debug("Polymarket raw response type: %s, len: %s", type(all_markets), len(all_markets) if isinstance(all_markets, list) else "N/A")
            btc_markets = [
                m for m in all_markets
                if isinstance(m, dict)
                and ("BTC" in m.get("question", "").upper() or "BITCOIN" in m.get("question", "").upper())
                and m.get("active")
            ]
            self._markets = btc_markets[:20]
            logger.info("Polymarket: found %d BTC markets", len(self._markets))
        except Exception as exc:
            logger.warning("Polymarket market refresh failed: %s", exc)
            self._markets = []

    async def _poll(self, client: httpx.AsyncClient) -> None:
        if not self._markets:
            await self._refresh_markets(client)
            return

        aggregated: list[dict] = []
        for market in self._markets[:10]:  # limit API calls
            try:
                condition_id = market.get("conditionId", "")
                if not condition_id:
                    continue
                # Get current orderbook/price from CLOB
                resp = await client.get(
                    f"{_CLOB_BASE}/midpoint",
                    params={"token_id": market.get("clobTokenIds", [""])[0]},
                )
                mid_data = resp.json() if resp.status_code == 200 else {}
                mid_price = float(mid_data.get("mid", 0) or 0)

                # Parse price target from question (e.g. "Will BTC be above $95000 at 5pm?")
                question = market.get("question", "")
                target_price = _parse_btc_target(question)

                aggregated.append({
                    "market_id": condition_id[:12],
                    "question": question,
                    "target_price": target_price,
                    "yes_prob": mid_price,          # 0–1 probability YES
                    "no_prob": 1.0 - mid_price,
                    "implied_price": target_price * mid_price if target_price else None,
                    "end_date": market.get("endDate", ""),
                    "volume": float(market.get("volume", 0) or 0),
                })
            except Exception:
                continue

        if aggregated:
            # Best estimate: weighted implied price from highest-volume markets
            best = sorted(aggregated, key=lambda x: x["volume"], reverse=True)
            ts = time.time()
            self._latest = {
                "ts": ts,
                "source": "polymarket",
                "markets": aggregated,
                "best_market": best[0] if best else None,
                "implied_btc": _calc_implied_btc(aggregated),
            }
            for cb in self._callbacks:
                try:
                    asyncio.ensure_future(cb(self._latest))
                except Exception:
                    pass


def _parse_btc_target(question: str) -> float | None:
    """Extract dollar target from question string."""
    import re
    # Match patterns like $95,000 or $95000 or 95000
    m = re.search(r"\$?([\d,]+(?:\.\d+)?)\s*(?:k)?", question)
    if m:
        raw = m.group(1).replace(",", "")
        val = float(raw)
        # If question says "k" after number, multiply by 1000
        if re.search(r"\d\s*k\b", question, re.IGNORECASE):
            val *= 1000
        return val if 1000 < val < 10_000_000 else None
    return None


def _calc_implied_btc(markets: list[dict]) -> float | None:
    """Volume-weighted implied BTC price from prediction markets."""
    weighted_sum = 0.0
    total_vol = 0.0
    for m in markets:
        if m["implied_price"] and m["volume"] > 0:
            weighted_sum += m["implied_price"] * m["volume"]
            total_vol += m["volume"]
    return weighted_sum / total_vol if total_vol > 0 else None
