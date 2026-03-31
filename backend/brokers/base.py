"""Abstract broker interface — paper and real brokers implement this."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal
import time
import uuid

OrderSide   = Literal["buy", "sell"]
OrderStatus = Literal["filled", "rejected", "pending"]


@dataclass
class Order:
    id: str
    symbol: str
    side: OrderSide
    qty: float
    fill_price: float | None
    status: OrderStatus
    ts: float
    note: str = ""


@dataclass
class Position:
    symbol: str
    qty: float           # positive = long
    avg_price: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class Account:
    cash: float
    equity: float        # cash + open position market value
    starting_cash: float = 100_000.0


class BrokerBase(ABC):
    @abstractmethod
    async def place_order(self, symbol: str, side: OrderSide,
                          qty: float, fill_price: float) -> Order: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_orders(self, limit: int = 50) -> list[Order]: ...

    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def mark_to_market(self, prices: dict[str, float]) -> None: ...
