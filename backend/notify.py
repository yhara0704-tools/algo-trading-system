"""Pushover通知モジュール."""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


async def push(title: str, message: str, priority: int = 0) -> bool:
    """Pushover通知を送信。設定がなければ静かにスキップ。

    priority: -1=low, 0=normal, 1=high, 2=emergency(要confirm_seconds)
    """
    user  = os.getenv("PUSHOVER_USER_KEY", "")
    token = os.getenv("PUSHOVER_API_TOKEN") or os.getenv("PUSHOVER_APP_TOKEN", "")
    if not user or not token:
        logger.debug("Pushover not configured — skip notification")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_PUSHOVER_URL, data={
                "token":    token,
                "user":     user,
                "title":    title,
                "message":  message,
                "priority": priority,
            })
            resp.raise_for_status()
            logger.info("Pushover sent: %s", title)
            return True
    except Exception as exc:
        logger.warning("Pushover failed: %s", exc)
        return False
