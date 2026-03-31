"""Trading REST endpoints."""
from __future__ import annotations

import dataclasses
from typing import Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.brokers.paper_broker import PaperBroker

router = APIRouter(prefix="/api/trading")

_broker: PaperBroker | None = None
_price_fn: Callable | None = None


def inject(broker: PaperBroker, price_fn: Callable) -> None:
    global _broker, _price_fn
    _broker = broker
    _price_fn = price_fn


class OrderRequest(BaseModel):
    symbol: str
    side: str   # "buy" | "sell"
    qty: float


@router.post("/order")
async def place_order(req: OrderRequest):
    if not _broker or not _price_fn:
        raise HTTPException(503, "Trading not ready")
    if req.side not in ("buy", "sell"):
        raise HTTPException(400, "side must be buy or sell")
    if req.qty <= 0:
        raise HTTPException(400, "qty must be positive")

    price = _price_fn(req.symbol)
    if not price:
        raise HTTPException(404, f"No price data for {req.symbol}")

    order = await _broker.place_order(req.symbol, req.side, req.qty, price)
    return dataclasses.asdict(order)


@router.get("/positions")
def get_positions():
    if not _broker:
        raise HTTPException(503, "Trading not ready")
    return {"positions": [dataclasses.asdict(p) for p in _broker.get_positions()]}


@router.get("/orders")
def get_orders(limit: int = 50):
    if not _broker:
        raise HTTPException(503, "Trading not ready")
    return {"orders": [dataclasses.asdict(o) for o in _broker.get_orders(limit)]}


@router.get("/account")
def get_account():
    if not _broker:
        raise HTTPException(503, "Trading not ready")
    return dataclasses.asdict(_broker.get_account())
