"""BII公開ページの自動スクリーンショット.

playwright が未インストールの場合は None を返す（graceful degradation）。
使用前に VPS で一度だけ:
    pip install playwright && playwright install chromium
"""
from __future__ import annotations

import logging
import pathlib
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))

_OUT_DIR = pathlib.Path("/root/algo_shared/screenshots")


async def capture(date: str | None = None,
                  base_url: str = "http://localhost:8001") -> pathlib.Path | None:
    """BII公開ページをスクリーンショットして PNG を保存する.

    Args:
        date: YYYY-MM-DD（省略時は今日）
        base_url: サーバーベースURL

    Returns:
        保存先 Path、または playwright 未インストール時 None
    """
    if date is None:
        date = datetime.now(JST).strftime("%Y-%m-%d")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("playwright 未インストール: pip install playwright && playwright install chromium")
        return None

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _OUT_DIR / f"{date}.png"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = await browser.new_page(viewport={"width": 1200, "height": 620})
            await page.goto(f"{base_url}/bii", wait_until="domcontentloaded", timeout=30000)
            # #main が表示されるまで待つ（チャート描画完了の合図）
            await page.wait_for_selector("#main", state="visible", timeout=20000)
            await page.screenshot(path=str(out_path), full_page=False)
            await browser.close()
        logger.info("BII screenshot saved: %s", out_path)
        return out_path
    except Exception as e:
        logger.error("BII screenshot failed: %s", e)
        return None
