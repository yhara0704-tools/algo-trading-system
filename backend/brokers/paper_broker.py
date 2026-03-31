"""Paper trading broker — in-memory, no real money."""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from typing import Callable

from .base import Account, BrokerBase, Order, OrderSide, Position


class PaperBroker(BrokerBase):
    def __init__(self, starting_cash: float = 100_000.0) -> None:
        self._cash = starting_cash
        self._starting_cash = starting_cash
        self._positions: dict[str, Position] = {}
        self._orders: deque[Order] = deque(maxlen=500)
        self._fill_callbacks: list[Callable] = []

    def on_fill(self, cb: Callable) -> None:
        self._fill_callbacks.append(cb)

    async def place_order(self, symbol: str, side: OrderSide,
                          qty: float, fill_price: float) -> Order:
        order = Order(
            id=str(uuid.uuid4())[:8],
            symbol=symbol,
            side=side,
            qty=qty,
            fill_price=None,
            status="pending",
            ts=time.time(),
        )

        if side == "buy":
            cost = fill_price * qty
            if cost > self._cash:
                order.status = "rejected"
                order.note = "insufficient cash"
                self._orders.appendleft(order)
                return order
            self._cash -= cost
            pos = self._positions.get(symbol)
            if pos:
                total = pos.qty + qty
                pos.avg_price = (pos.avg_price * pos.qty + fill_price * qty) / total
                pos.qty = total
            else:
                self._positions[symbol] = Position(
                    symbol=symbol, qty=qty, avg_price=fill_price
                )

        elif side == "sell":
            pos = self._positions.get(symbol)
            sell_qty = min(qty, pos.qty if pos else 0.0)
            if sell_qty < 1e-9:
                order.status = "rejected"
                order.note = "no position"
                self._orders.appendleft(order)
                return order
            realized = (fill_price - pos.avg_price) * sell_qty
            pos.realized_pnl += realized
            pos.qty -= sell_qty
            self._cash += fill_price * sell_qty
            order.qty = sell_qty
            if pos.qty < 1e-9:
                del self._positions[symbol]

        order.fill_price = fill_price
        order.status = "filled"
        self._orders.appendleft(order)

        for cb in self._fill_callbacks:
            asyncio.ensure_future(cb(order, self.get_account()))

        return order

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_orders(self, limit: int = 50) -> list[Order]:
        return list(self._orders)[:limit]

    def get_account(self) -> Account:
        mkt_value = sum(
            p.qty * p.avg_price for p in self._positions.values()
        )
        return Account(
            cash=self._cash,
            equity=self._cash + mkt_value,
            starting_cash=self._starting_cash,
        )

    def mark_to_market(self, prices: dict[str, float]) -> None:
        for sym, pos in self._positions.items():
            if sym in prices:
                pos.unrealized_pnl = (prices[sym] - pos.avg_price) * pos.qty
