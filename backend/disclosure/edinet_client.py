"""EDINET API v2 クライアント — 大量保有報告書の取得と分類."""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

EDINET_BASE = "https://api.edinet-fsa.go.jp/api/v2"
_last_request_at: float = 0.0


def _api_key() -> str:
    return os.getenv("EDINET_API_KEY", "")


async def _throttle() -> None:
    """最低1秒の間隔を空ける。"""
    global _last_request_at
    now = time.monotonic()
    wait = max(0, 1.0 - (now - _last_request_at))
    if wait > 0:
        await asyncio.sleep(wait)
    _last_request_at = time.monotonic()


async def fetch_daily_documents(date_str: str) -> list[dict]:
    """指定日のEDINET全提出書類を取得する。"""
    await _throttle()
    key = _api_key()
    if not key:
        logger.warning("EDINET_API_KEY not set")
        return []
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{EDINET_BASE}/documents.json", params={
            "date": date_str,
            "type": 2,
            "Subscription-Key": key,
        })
        resp.raise_for_status()
        data = resp.json()
    return data.get("results", [])


async def fetch_large_holdings(date_str: str) -> list[dict]:
    """指定日の大量保有報告書(ordinanceCode=060)のみ返す。"""
    docs = await fetch_daily_documents(date_str)
    return [d for d in docs if d.get("ordinanceCode") == "060"]


def classify_filing(doc: dict) -> str:
    """報告書を "new" / "change" / "amendment" に分類する。"""
    desc = doc.get("docDescription") or ""
    parent = doc.get("parentDocID")

    if parent or "訂正" in desc:
        return "amendment"
    if "変更" in desc:
        return "change"
    return "new"


def is_amendment(doc: dict) -> bool:
    """訂正報告書かどうか。"""
    return classify_filing(doc) == "amendment"


def to_filing_dict(doc: dict, date_str: str) -> dict:
    """EDINET APIレスポンスをtob_filings用のdictに変換する。"""
    return {
        "doc_id": doc.get("docID", ""),
        "date": date_str,
        "doc_description": doc.get("docDescription", ""),
        "filer_name": doc.get("filerName", ""),
        "issuer_edinet_code": doc.get("issuerEdinetCode", ""),
        "amendment_flag": doc.get("amendmentFlag") or "0",
        "parent_doc_id": doc.get("parentDocID"),
        "form_code": doc.get("formCode", ""),
        "filing_type": classify_filing(doc),
    }
