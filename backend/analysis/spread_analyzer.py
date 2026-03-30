"""Spread analyzer — Coinbase spot vs Polymarket 5-min BTC prediction."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class SpreadSnapshot:
    ts: float
    coinbase_price: float
    polymarket_implied: float | None
    spread_usd: float | None          # spot - implied
    spread_pct: float | None          # spread as % of spot
    signal: str = "neutral"           # "long", "short", "neutral"
    confidence: float = 0.0           # 0–1


class SpreadAnalyzer:
    """Tracks Coinbase vs Polymarket BTC price spread and generates signals."""

    def __init__(self, history_limit: int = 500) -> None:
        self._history: deque[SpreadSnapshot] = deque(maxlen=history_limit)
        self._coinbase_price: float | None = None
        self._polymarket_implied: float | None = None

    # ── Data ingestion ─────────────────────────────────────────────────────────

    def update_coinbase(self, price: float) -> None:
        self._coinbase_price = price
        self._maybe_compute()

    def update_polymarket(self, implied: float | None) -> None:
        self._polymarket_implied = implied
        self._maybe_compute()

    # ── Snapshot generation ────────────────────────────────────────────────────

    def _maybe_compute(self) -> SpreadSnapshot | None:
        if self._coinbase_price is None:
            return None
        snap = self._compute()
        self._history.append(snap)
        return snap

    def _compute(self) -> SpreadSnapshot:
        spot = self._coinbase_price  # type: ignore[assignment]
        implied = self._polymarket_implied

        spread_usd = (spot - implied) if implied else None
        spread_pct = (spread_usd / spot * 100) if spread_usd is not None else None

        signal, confidence = self._signal(spread_pct)
        return SpreadSnapshot(
            ts=time.time(),
            coinbase_price=spot,
            polymarket_implied=implied,
            spread_usd=spread_usd,
            spread_pct=spread_pct,
            signal=signal,
            confidence=confidence,
        )

    def _signal(self, spread_pct: float | None) -> tuple[str, float]:
        """Simple mean-reversion signal.

        If market predicts price BELOW current spot (spread > 0), short signal.
        If market predicts price ABOVE current spot (spread < 0), long signal.
        Threshold: ±0.3% for weak signal, ±0.8% for strong.
        """
        if spread_pct is None:
            return "neutral", 0.0

        abs_spread = abs(spread_pct)
        if abs_spread < 0.3:
            return "neutral", 0.0
        elif abs_spread < 0.8:
            confidence = min((abs_spread - 0.3) / 0.5, 1.0)
            direction = "short" if spread_pct > 0 else "long"
            return direction, round(confidence, 3)
        else:
            confidence = min((abs_spread - 0.8) / 0.5 + 0.5, 1.0)
            direction = "short" if spread_pct > 0 else "long"
            return direction, round(confidence, 3)

    # ── Query ──────────────────────────────────────────────────────────────────

    def get_latest(self) -> dict | None:
        if not self._history:
            return None
        s = self._history[-1]
        return _snap_to_dict(s)

    def get_history(self, limit: int = 100) -> list[dict]:
        snaps = list(self._history)[-limit:]
        return [_snap_to_dict(s) for s in snaps]

    def get_stats(self) -> dict:
        """Rolling statistics over available history."""
        if not self._history:
            return {}
        spreads = [s.spread_pct for s in self._history if s.spread_pct is not None]
        if not spreads:
            return {"count": len(self._history), "spread_available": False}
        import statistics as st
        return {
            "count": len(self._history),
            "spread_available": True,
            "mean_spread_pct": round(st.mean(spreads), 4),
            "stdev_spread_pct": round(st.stdev(spreads), 4) if len(spreads) > 1 else 0,
            "max_spread_pct": round(max(spreads), 4),
            "min_spread_pct": round(min(spreads), 4),
            "long_signals": sum(1 for s in self._history if s.signal == "long"),
            "short_signals": sum(1 for s in self._history if s.signal == "short"),
        }


def _snap_to_dict(s: SpreadSnapshot) -> dict:
    return {
        "ts": s.ts,
        "coinbase_price": s.coinbase_price,
        "polymarket_implied": s.polymarket_implied,
        "spread_usd": round(s.spread_usd, 2) if s.spread_usd is not None else None,
        "spread_pct": round(s.spread_pct, 4) if s.spread_pct is not None else None,
        "signal": s.signal,
        "confidence": s.confidence,
    }
