"""Social Strategy REST API — Xポスト手法抽出・バックテスト."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, BackgroundTasks

router = APIRouter(prefix="/api/social", tags=["social"])

_extractor_running: set[str] = set()


@router.get("/strategies")
def list_strategies():
    """抽出済み手法一覧。"""
    from backend.strategies.social.extractor import load_all_extracted
    strategies = load_all_extracted()
    return {"strategies": [
        {
            "id":          s.id,
            "name":        s.name,
            "handle":      s.handle,
            "market":      s.market,
            "confidence":  s.confidence,
            "post_count":  s.post_count,
            "description": s.description,
            "entry_rules": s.entry_rules,
            "exit_rules":  s.exit_rules,
            "time_rules":  s.time_rules,
            "risk_rules":  s.risk_rules,
            "params":      s.params,
        }
        for s in strategies
    ]}


@router.get("/posts/{handle}")
def get_posts(handle: str):
    """BIIから蓄積ポストを取得。"""
    from backend.strategies.social.extractor import get_posts_from_bii
    posts = get_posts_from_bii(handle)
    return {
        "handle": handle,
        "total":  len(posts),
        "trade_posts": sum(1 for p in posts if p.get("_has_trade_info")),
        "posts":  posts[:20],  # 先頭20件のみ返す
    }


@router.get("/posts")
def list_posts_summary():
    """全ハンドルのポスト蓄積状況。"""
    from pathlib import Path
    import json
    bii_path = Path(os.getenv(
        "BII_TRADER_WATCH_PATH",
        "/Users/himanosuke/Bull/bull_forecast/observation/trader_watch"
    )) / "data"

    summary = []
    if bii_path.exists():
        for h_dir in bii_path.iterdir():
            if not h_dir.is_dir():
                continue
            total = 0
            trade = 0
            for f in h_dir.glob("*.json"):
                try:
                    posts = json.loads(f.read_text())
                    total += len(posts)
                    trade += sum(1 for p in posts if p.get("_has_trade_info"))
                except Exception:
                    pass
            if total > 0:
                summary.append({
                    "handle":      h_dir.name,
                    "total_posts": total,
                    "trade_posts": trade,
                })
    return {"handles": summary}


@router.post("/extract/{handle}")
async def extract_strategy(handle: str, background_tasks: BackgroundTasks):
    """特定ハンドルの手法を抽出（バックグラウンド）。"""
    if handle in _extractor_running:
        return {"status": "already_running", "handle": handle}
    background_tasks.add_task(_run_extraction, handle)
    return {"status": "started", "handle": handle}


@router.post("/extract")
async def extract_all(background_tasks: BackgroundTasks):
    """全ハンドルの手法を抽出（バックグラウンド）。"""
    from pathlib import Path
    bii_path = Path(os.getenv(
        "BII_TRADER_WATCH_PATH",
        "/Users/himanosuke/Bull/bull_forecast/observation/trader_watch"
    )) / "data"

    handles = []
    if bii_path.exists():
        handles = [p.name for p in bii_path.iterdir() if p.is_dir()]

    for h in handles:
        if h not in _extractor_running:
            background_tasks.add_task(_run_extraction, h)

    return {"status": "started", "handles": handles}


@router.get("/profiles")
def list_profiles():
    """BIIで蓄積されたトレーダープロファイル一覧。"""
    bii_path = Path(os.getenv(
        "BII_TRADER_WATCH_PATH",
        "/Users/himanosuke/Bull/bull_forecast/observation/trader_watch"
    )) / "data"

    profiles = []
    if bii_path.exists():
        for h_dir in bii_path.iterdir():
            if not h_dir.is_dir():
                continue
            prof_file = h_dir / "profile.json"
            if prof_file.exists():
                try:
                    profiles.append(json.loads(prof_file.read_text(encoding="utf-8")))
                except Exception:
                    pass
    return {"profiles": profiles}


@router.get("/profiles/{handle}")
def get_profile(handle: str):
    """特定トレーダーのプロファイル。"""
    bii_path = Path(os.getenv(
        "BII_TRADER_WATCH_PATH",
        "/Users/himanosuke/Bull/bull_forecast/observation/trader_watch"
    )) / "data" / handle / "profile.json"

    if not bii_path.exists():
        raise HTTPException(404, f"No profile for @{handle}")
    return json.loads(bii_path.read_text(encoding="utf-8"))


async def _run_extraction(handle: str) -> None:
    import anthropic
    from backend.strategies.social.extractor import (
        get_posts_from_bii, extract_strategy, save_extracted
    )
    _extractor_running.add(handle)
    try:
        posts  = get_posts_from_bii(handle)
        if not posts:
            return
        client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        result = await extract_strategy(handle, posts, client)
        if result:
            path = save_extracted(result)
            from backend.notify import push
            await push(
                f"🔍 手法抽出完了: @{handle}",
                f"{result.name}\n確信度: {result.confidence:.0%}\n{result.description[:100]}",
                priority=0,
            )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Extraction error for @%s: %s", handle, e)
    finally:
        _extractor_running.discard(handle)
