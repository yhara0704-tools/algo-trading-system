"""TOB監視デーモン — EDINET大量保有報告書を日次取得してTOB候補をスコアリング.

実行:
    nohup .venv/bin/python3 scripts/tob_monitor.py > /tmp/tob_monitor.log 2>&1 &

初回実行時: 過去180日分を自動バックフィル（通知なし）。
以降: 毎朝9:30 JSTに前日分を取得 → スコアリング → ランキング通知。
"""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")

from backend.disclosure.edinet_client import fetch_large_holdings, to_filing_dict
from backend.disclosure.tob_scorer import run_daily_scoring, KNOWN_TOB_ANNOUNCED
from backend.storage.db import (
    get_db, upsert_tob_filing, upsert_issuer_map,
    get_issuer_map, get_tob_ranking,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/tmp/tob_monitor_detail.log"),
    ],
)
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
STATE_FILE = pathlib.Path(__file__).resolve().parent.parent / "data" / "tob_monitor_state.json"
BACKFILL_DAYS = 180
RANKING_TOP_N = 20
NOTIFY_MIN_SCORE = 30.0


# ── 証券コード逆引き用の初期マッピング ────────────────────────────────────────
KNOWN_ISSUER_MAP: dict[str, tuple[str, str]] = {
    "E25678": ("4581", "大正製薬HD"), "E04939": ("9783", "ベネッセHD"),
    "E00469": ("2899", "永谷園HD"), "E04208": ("9058", "トランコム"),
    "E01876": ("6640", "I-PEX"), "E33966": ("4384", "ラクスル"),
    "E01027": ("4917", "マンダム"), "E04878": ("6197", "ソラスト"),
    "E04810": ("9749", "富士ソフト"), "E05372": ("3978", "マクロミル"),
    "E04966": ("7518", "ネットワンシステムズ"), "E02054": ("6789", "ローランドDG"),
    "E30124": ("3294", "イーグランド"), "E02677": ("8155", "三益半導体工業"),
    "E05037": ("4726", "SBテクノロジー"), "E04911": ("9613", "NTTデータ"),
    "E02558": ("7451", "三菱食品"), "E00067": ("1884", "日本道路"),
    "E01243": ("5481", "山陽特殊製鋼"), "E04874": ("9787", "イオンディライト"),
    "E03464": ("3391", "ツルハHD"), "E26990": ("7163", "住信SBIネット銀行"),
    "E03132": ("8289", "Olympicグループ"),
}


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _seed_issuer_map() -> None:
    for edinet_code, (sec_code, name) in KNOWN_ISSUER_MAP.items():
        existing = get_issuer_map(edinet_code)
        if not existing or not existing.get("sec_code"):
            upsert_issuer_map(edinet_code, sec_code, name)


def _try_resolve_issuer(issuer_edinet_code: str, all_docs: list[dict]) -> None:
    existing = get_issuer_map(issuer_edinet_code)
    if existing and existing.get("sec_code"):
        return
    for doc in all_docs:
        ec = doc.get("edinetCode") or doc.get("issuerEdinetCode") or ""
        sc = doc.get("secCode") or ""
        fn = doc.get("filerName") or ""
        if ec == issuer_edinet_code and sc:
            sc4 = sc.rstrip("0") if len(sc) == 5 and sc.endswith("0") else sc
            upsert_issuer_map(issuer_edinet_code, sc4, fn)
            return


async def ingest_day(date_str: str) -> int:
    filings = await fetch_large_holdings(date_str)
    count = 0
    for doc in filings:
        f = to_filing_dict(doc, date_str)
        issuer_ec = f["issuer_edinet_code"]
        if not issuer_ec:
            continue
        upsert_tob_filing(f)
        count += 1
    return count


async def backfill(days: int = BACKFILL_DAYS) -> None:
    """過去N日分を一括取得する（通知なし）。"""
    state = _load_state()
    done = set(state.get("backfill_done", []))

    end = datetime.now(JST).date()
    start = end - timedelta(days=days)
    current = start

    logger.info("バックフィル開始: %s 〜 %s (%d日)", start, end, days)

    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        if current.weekday() >= 5 or date_str in done:
            current += timedelta(days=1)
            continue

        try:
            count = await ingest_day(date_str)
            if count > 0:
                logger.info("  %s: %d件取得", date_str, count)
        except Exception as e:
            logger.warning("  %s: エラー %s", date_str, e)

        done.add(date_str)
        if len(done) % 10 == 0:
            state["backfill_done"] = sorted(done)
            _save_state(state)

        current += timedelta(days=1)

    state["backfill_done"] = sorted(done)
    state["backfill_completed"] = datetime.now(JST).isoformat()
    _save_state(state)
    logger.info("バックフィル完了: %d日処理", len(done))


async def send_ranking_notification(results: list[dict], prev_ranking: dict[str, int]) -> None:
    """1通のランキング通知を送信する。前回順位との比較付き。"""
    from backend.notify import push

    # スコア30以上のみ、上位N件
    candidates = [r for r in results if r["score"] >= NOTIFY_MIN_SCORE][:RANKING_TOP_N]
    if not candidates:
        logger.info("通知対象なし（スコア30以上がゼロ）")
        return

    today = datetime.now(JST).strftime("%m/%d")
    lines = [f"TOBスコア ランキング ({today})"]
    lines.append("=" * 30)

    for i, c in enumerate(candidates, 1):
        name = c["issuer_name"] or c["issuer_edinet_code"]
        sec = c["sec_code"] or "?"
        score = c["score"]
        tob_status = c.get("tob_status", "")

        # 前回順位
        prev_rank = prev_ranking.get(c["issuer_edinet_code"])
        if prev_rank is None:
            rank_change = " (NEW)"
        elif prev_rank == i:
            rank_change = " (→)"
        elif prev_rank > i:
            rank_change = f" (↑{prev_rank}位)"
        else:
            rank_change = f" (↓{prev_rank}位)"

        # TOB発表済みラベル
        tob_label = f" 【{tob_status}】" if tob_status else ""

        lines.append(f"{i}位: {name} ({sec}) {score:.0f}点{rank_change}{tob_label}")

    # 未発表で高スコアの銘柄を強調
    unannounced_high = [
        c for c in candidates
        if not c.get("tob_status") and c["score"] >= 50
    ]
    if unannounced_high:
        lines.append("")
        lines.append("--- 未発表で注目 ---")
        for c in unannounced_high[:5]:
            name = c["issuer_name"] or "?"
            sec = c["sec_code"] or "?"
            lines.append(f"  {name} ({sec}) {c['score']:.0f}点 訂正{c['amendment_count']} 提出者{c['unique_filers']}")

    msg = "\n".join(lines)
    priority = 1 if any(
        not c.get("tob_status") and c["score"] >= 60
        for c in candidates
    ) else 0

    await push(f"TOBランキング {today}", msg, priority=priority)
    logger.info("ランキング通知送信 (%d件)", len(candidates))


def _build_prev_ranking() -> dict[str, int]:
    """現在のDBランキングから前回順位マップを作る。"""
    rows = get_tob_ranking(limit=50)
    return {r["issuer_edinet_code"]: i + 1 for i, r in enumerate(rows)}


async def main():
    get_db()
    _seed_issuer_map()

    state = _load_state()
    logger.info("=" * 60)
    logger.info("TOB監視デーモン起動")
    logger.info("=" * 60)

    # 初回バックフィル（通知なし）
    if not state.get("backfill_completed"):
        await backfill()
        # バックフィル後のスコアリング
        logger.info("初回スコアリング実行...")
        results = await run_daily_scoring()
        logger.info("初回スコアリング完了: %d件（通知はスキップ）", len(results))
    else:
        # 通常起動: スコアリング + 通知
        prev_ranking = _build_prev_ranking()
        logger.info("スコアリング実行...")
        results = await run_daily_scoring()
        logger.info("スコアリング完了: %d件", len(results))
        await send_ranking_notification(results, prev_ranking)

    # ── 日次ループ ──
    while True:
        now = datetime.now(JST)
        target = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        while target.weekday() >= 5:
            target += timedelta(days=1)

        wait = (target - now).total_seconds()
        logger.info("次回スキャン: %s JST (%.0f分後)",
                     target.strftime("%m/%d %H:%M"), wait / 60)
        await asyncio.sleep(wait)

        yesterday = (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")
        logger.info("日次スキャン: %s", yesterday)
        try:
            # 前回のランキングを保存
            prev_ranking = _build_prev_ranking()

            count = await ingest_day(yesterday)
            logger.info("取得: %d件", count)

            results = await run_daily_scoring()
            logger.info("スコアリング: %d件", len(results))

            # 1通のランキング通知
            await send_ranking_notification(results, prev_ranking)

        except Exception as e:
            logger.error("日次スキャンエラー: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
