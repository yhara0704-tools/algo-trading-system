"""TOBスコアリングエンジン — 大量保有報告書のパターンから買収確度を算出."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# yfinance PBR/時価総額キャッシュ（24h）
_market_cache: dict[str, tuple[float, dict]] = {}  # sec_code -> (timestamp, data)
_CACHE_TTL = 86400  # 24時間

# TOB発表済み銘柄（手動管理、判明次第追加）
KNOWN_TOB_ANNOUNCED: dict[str, str] = {
    # sec_code: "TOB状況"
    "4917": "MBO進行中(KKR対抗)",
    "6197": "MBO進行中",
    "4384": "MBO成立(2026/2)",
    "9749": "KKR買収成立(2025/2)",
    "3978": "CVC買収成立",
    "7518": "SCSK買収成立",
    "6789": "MBO成立",
    "8155": "信越化学TOB成立(上場廃止)",
    "4726": "SB完全子会社化成立",
    "9613": "NTT完全子会社化成立",
    "7451": "三菱商事TOB成立",
    "1884": "清水建設TOB成立",
    "5481": "日本製鉄TOB成立",
    "9787": "イオンTOB成立",
    "3391": "イオンTOB成立",
    "7163": "NTTドコモTOB成立",
    "9783": "EQT+創業家MBO成立",
    "2899": "丸の内キャピタルMBO成立",
    "9058": "ベインキャピタルMBO成立",
    "6640": "MBO成立",
    "4581": "大手門MBO成立",
    "6901": "日本モノづくり未来投資F TOB(上場廃止予定)",
}


async def fetch_market_data(sec_code: str) -> dict:
    """yfinanceでPBR・時価総額を取得する。キャッシュ24h。"""
    if not sec_code or len(sec_code) != 4 or not sec_code.isdigit():
        return {}

    now = time.time()
    cached = _market_cache.get(sec_code)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    try:
        import yfinance as yf
        ticker = f"{sec_code}.T"
        info = await asyncio.to_thread(lambda: yf.Ticker(ticker).info)
        result = {
            "pbr": info.get("priceToBook"),
            "market_cap_b": (info.get("marketCap") or 0) / 1e8,  # 億円
        }
        _market_cache[sec_code] = (now, result)
        return result
    except Exception as e:
        logger.warning("yfinance error for %s: %s", sec_code, e)
        return {}


def _is_routine_institutional(doc_description: str) -> bool:
    """「特例対象株券等」はルーティンの機関投資家報告でTOBシグナルとしては弱い。"""
    return "特例対象株券等" in (doc_description or "")


def compute_score(
    total_filings: int,
    amendment_count: int,
    regular_amendment_count: int,
    unique_filers: int,
    has_old_amendment: bool,
    pbr: float | None,
    market_cap_b: float | None,
) -> tuple[float, dict]:
    """TOBスコアを算出する。(スコア, 内訳dict) を返す。

    regular_amendment_count: 「特例対象株券等」を除いた訂正数（より強いシグナル）
    """
    detail = {}

    # 訂正報告書数 (最大30点)
    # 通常訂正は10点/件、特例対象株券等の訂正は2点/件
    institutional_amend = amendment_count - regular_amendment_count
    s_amend = min(regular_amendment_count * 10 + institutional_amend * 2, 30)
    detail["amendment"] = s_amend

    # 報告書総数 (最大20点)
    s_filings = min(total_filings * 3, 20)
    detail["filings"] = s_filings

    # ユニーク提出者数 (最大15点)
    s_filers = min(max(unique_filers - 1, 0) * 5, 15)
    detail["filers"] = s_filers

    # 古い訂正あり (15点)
    s_old = 15 if has_old_amendment else 0
    detail["old_amendment"] = s_old

    # PBR < 1.0 (10点)
    s_pbr = 10 if (pbr is not None and pbr < 1.0) else 0
    detail["pbr"] = s_pbr

    # 時価総額 (最大10点)
    if market_cap_b is not None and market_cap_b > 0:
        if market_cap_b < 2000:
            s_cap = 10
        elif market_cap_b < 5000:
            s_cap = 5
        else:
            s_cap = 0
    else:
        s_cap = 0
    detail["market_cap"] = s_cap

    total = s_amend + s_filings + s_filers + s_old + s_pbr + s_cap
    return total, detail


async def score_issuer(issuer_edinet_code: str, skip_market_data: bool = False) -> dict | None:
    """1つの発行体のTOBスコアを算出する。tob_scores用のdictを返す。"""
    from backend.storage.db import get_tob_filings, get_issuer_map

    filings = get_tob_filings(issuer_edinet_code, days=180)
    if not filings:
        return None

    # 集計
    amendment_count = sum(
        1 for f in filings if f.get("filing_type") == "amendment"
    )
    # 通常訂正（特例対象株券等を除く）
    regular_amendment_count = sum(
        1 for f in filings
        if f.get("filing_type") == "amendment"
        and not _is_routine_institutional(f.get("doc_description", ""))
    )
    filer_names = set(f.get("filer_name", "") for f in filings if f.get("filer_name"))
    unique_filers = len(filer_names)

    # 古い訂正: parentDocIDが存在する訂正報告書があるか
    has_old_amendment = any(
        f.get("parent_doc_id") and f.get("filing_type") == "amendment"
        for f in filings
    )

    # EDINETコード→証券コードの変換
    issuer_map = get_issuer_map(issuer_edinet_code)
    sec_code = (issuer_map or {}).get("sec_code", "")
    issuer_name = (issuer_map or {}).get("issuer_name", issuer_edinet_code)

    # 市場データ取得（skip_market_dataでスキップ可能）
    if not skip_market_data and sec_code:
        market = await fetch_market_data(sec_code)
    else:
        market = {}
    pbr = market.get("pbr")
    market_cap_b = market.get("market_cap_b")

    score, detail = compute_score(
        total_filings=len(filings),
        amendment_count=amendment_count,
        regular_amendment_count=regular_amendment_count,
        unique_filers=unique_filers,
        has_old_amendment=has_old_amendment,
        pbr=pbr,
        market_cap_b=market_cap_b,
    )

    # TOB発表済みラベル
    tob_status = KNOWN_TOB_ANNOUNCED.get(sec_code, "")

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "issuer_edinet_code": issuer_edinet_code,
        "issuer_name": issuer_name,
        "sec_code": sec_code,
        "total_filings_6m": len(filings),
        "amendment_count": amendment_count,
        "regular_amendment_count": regular_amendment_count,
        "unique_filers": unique_filers,
        "has_old_amendment": has_old_amendment,
        "pbr": pbr,
        "market_cap_b": market_cap_b,
        "score": score,
        "score_detail": detail,
        "tob_status": tob_status,
    }


async def run_daily_scoring() -> list[dict]:
    """全アクティブ発行体のスコアを計算し、DBに保存する。

    Phase 1: 全銘柄を市場データなしでスコア算出（高速）
    Phase 2: スコア50以上の銘柄のみyfinanceで市場データを取得して再計算
    """
    from backend.storage.db import get_active_issuers, upsert_tob_score

    issuers = get_active_issuers(days=180)
    logger.info("TOBスコアリング: %d発行体", len(issuers))

    # Phase 1: 市場データなしで全銘柄スコア算出
    results = []
    for edinet_code in issuers:
        score_dict = await score_issuer(edinet_code, skip_market_data=True)
        if score_dict and score_dict["score"] > 0:
            results.append(score_dict)

    # Phase 2: 上位銘柄のみ市場データ付きで再計算
    logger.info("Phase2: スコア50以上 %d件で市場データ取得",
                 sum(1 for r in results if r["score"] >= 50))
    for i, r in enumerate(results):
        if r["score"] >= 50 and r.get("sec_code"):
            enriched = await score_issuer(r["issuer_edinet_code"], skip_market_data=False)
            if enriched:
                results[i] = enriched

    # DB保存
    for r in results:
        upsert_tob_score(r)

    results.sort(key=lambda x: x["score"], reverse=True)
    if results:
        top = results[0]
        logger.info("TOBスコア最高: %s (%s) = %.0f点",
                     top["issuer_name"], top["sec_code"], top["score"])
    return results
