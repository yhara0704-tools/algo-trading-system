"""TOB監視エンドポイント — スコアランキングと報告書履歴."""
from __future__ import annotations

import json
from fastapi import APIRouter

from backend.storage.db import (
    get_tob_ranking,
    get_tob_filings,
    get_tob_score_history,
    get_issuer_map,
)

router = APIRouter(prefix="/api/tob", tags=["tob"])


@router.get("/ranking")
def ranking(limit: int = 30) -> dict:
    """TOB候補スコアランキングを返す。"""
    rows = get_tob_ranking(limit=limit)
    for r in rows:
        if isinstance(r.get("score_detail"), str):
            r["score_detail"] = json.loads(r["score_detail"])
    return {"candidates": rows, "total": len(rows)}


@router.get("/ranking/{edinet_code}")
def issuer_detail(edinet_code: str) -> dict:
    """個別銘柄の詳細（報告書履歴＋スコア推移）。"""
    issuer = get_issuer_map(edinet_code) or {}
    filings = get_tob_filings(edinet_code, days=365)
    scores = get_tob_score_history(edinet_code, days=90)
    for s in scores:
        if isinstance(s.get("score_detail"), str):
            s["score_detail"] = json.loads(s["score_detail"])
    return {
        "issuer": issuer,
        "filings": filings,
        "score_history": scores,
    }


@router.get("/filings")
def recent_filings(days: int = 7) -> dict:
    """直近の大量保有報告書一覧（全銘柄）。"""
    from backend.storage.db import get_active_issuers, get_tob_filings as _get
    from datetime import datetime, timedelta

    issuers = get_active_issuers(days=days)
    all_filings = []
    for ec in issuers:
        all_filings.extend(_get(ec, days=days))
    all_filings.sort(key=lambda x: x.get("date", ""), reverse=True)
    return {"filings": all_filings, "total": len(all_filings)}
