"""
social/extractor.py — トレーダーのXポストからトレード手法を抽出するエージェント。

BIIが蓄積したポストを読み込み、Claudeが手法を解析して
「○○手法」として命名・構造化し、バックテスト可能なパラメータに変換する。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

# BIIのtrader_watchデータへのパス
_BII_TRADER_WATCH = Path(os.getenv(
    "BII_TRADER_WATCH_PATH",
    "/Users/himanosuke/Bull/bull_forecast/observation/trader_watch"
))
_EXTRACTED_DIR = Path(__file__).parent.parent.parent.parent / "data" / "social_strategies"
_EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ExtractedStrategy:
    """Xポストから抽出した手法の構造化データ。"""
    id:            str          # 例: "nikaido_orb_v1"
    name:          str          # 例: "二階堂ORB手法"
    handle:        str          # 元トレーダー
    market:        str          # JP_stock / BTC 等
    extracted_at:  float
    post_count:    int          # 参考にしたポスト数
    confidence:    float        # 抽出確信度 0-1

    # 手法概要
    description:   str
    entry_rules:   list[str]    # エントリー条件
    exit_rules:    list[str]    # 決済条件
    time_rules:    list[str]    # 時間帯条件（例: "寄り付き後15分は様子見"）
    risk_rules:    list[str]    # リスク管理ルール

    # バックテスト可能なパラメータ（Claudeが推定）
    params: dict[str, Any] = field(default_factory=dict)
    # 例: {
    #   "interval": "1m",
    #   "entry_type": "breakout",  # breakout / reversal / trend_follow
    #   "range_minutes": 15,
    #   "tp_ratio": 2.0,
    #   "sl_ratio": 1.0,
    #   "avoid_slots": ["09:00"],
    #   "only_slots": ["09:15", "09:30", "10:00"],
    # }

    # 元ポストサンプル
    source_posts:  list[dict] = field(default_factory=list)
    raw_analysis:  str = ""     # Claudeの生分析テキスト


_EXTRACTOR_SYSTEM = """あなたは株式トレーダーのSNS投稿を分析して、トレード手法を構造化するAIです。

与えられた複数のXポストから：
1. 手法の核心ルールを抽出する
2. エントリー・エグジット・時間帯・リスク管理のルールを整理する
3. バックテスト可能なパラメータを数値で推定する

ポストは日本語で書かれた日本株トレーダーのものが多いです。
「寄り付き」=場開き(9:00)、「引け」=場終わり(15:30)、
「ブレイク」=価格突破、「ナンピン」=追加買い、「逆張り」=逆方向エントリー。

回答は必ず指定のJSON形式で返してください。"""

_EXTRACTOR_USER_TPL = """トレーダー @{handle} の直近ポスト {n} 件を分析して、トレード手法を抽出してください。

## ポスト一覧
{posts_text}

## 出力形式（JSONのみ返してください）
{{
  "name": "手法の名前（例: 二階堂ORB手法）",
  "description": "手法の概要を2〜3文で",
  "confidence": 0.0〜1.0（ポストから手法が明確に読み取れる度合い）,
  "market": "JP_stock または BTC または FX",
  "entry_rules": ["エントリー条件を箇条書き（最大5個）"],
  "exit_rules": ["決済条件を箇条書き（最大5個）"],
  "time_rules": ["時間帯条件を箇条書き（例: 寄り付き後15分は様子見、14:30以降はポジションを持たない）"],
  "risk_rules": ["リスク管理ルール（損切りルール、ポジションサイズ等）"],
  "params": {{
    "interval": "1m または 5m",
    "entry_type": "breakout または reversal または trend_follow または vwap_reversion",
    "range_minutes": 15,
    "tp_ratio": 2.0,
    "sl_ratio": 1.0,
    "avoid_slots": ["09:00"],
    "preferred_slots": [],
    "rsi_threshold": null,
    "dev_pct": null
  }},
  "notable_quotes": ["手法を端的に表す印象的なポスト（最大3個）"]
}}"""


async def extract_strategy(
    handle: str,
    posts: list[dict],
    client: anthropic.AsyncAnthropic,
) -> ExtractedStrategy | None:
    """ポスト群から手法を抽出する。"""
    if not posts:
        logger.info("No posts for @%s", handle)
        return None

    # トレード情報を含むポストに絞り込み（最大50件）
    trade_posts = [p for p in posts if p.get("_has_trade_info")]
    if not trade_posts:
        trade_posts = posts  # フラグなければ全件使う
    sample = sorted(trade_posts, key=lambda p: p.get("created_at", ""), reverse=True)[:50]

    posts_text = "\n---\n".join(
        f"[{p.get('created_at', '')[:10]}] {p.get('text', '')}"
        for p in sample
    )

    try:
        resp = await client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2048,
            system=_EXTRACTOR_SYSTEM,
            messages=[{"role": "user", "content": _EXTRACTOR_USER_TPL.format(
                handle=handle, n=len(sample), posts_text=posts_text
            )}],
        )
        raw = resp.content[0].text.strip()

        # JSON抽出
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)

    except Exception as e:
        logger.error("Extraction failed for @%s: %s", handle, e)
        return None

    strategy_id = f"{handle}_{data.get('params', {}).get('entry_type', 'custom')}_v1"

    return ExtractedStrategy(
        id=strategy_id,
        name=data.get("name", f"{handle}手法"),
        handle=handle,
        market=data.get("market", "JP_stock"),
        extracted_at=time.time(),
        post_count=len(sample),
        confidence=float(data.get("confidence", 0.5)),
        description=data.get("description", ""),
        entry_rules=data.get("entry_rules", []),
        exit_rules=data.get("exit_rules", []),
        time_rules=data.get("time_rules", []),
        risk_rules=data.get("risk_rules", []),
        params=data.get("params", {}),
        source_posts=[{"id": p["id"], "text": p.get("text", "")[:100]} for p in sample[:5]],
        raw_analysis=raw,
    )


def save_extracted(strategy: ExtractedStrategy) -> Path:
    path = _EXTRACTED_DIR / f"{strategy.id}.json"
    path.write_text(json.dumps(asdict(strategy), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_all_extracted() -> list[ExtractedStrategy]:
    result = []
    for f in _EXTRACTED_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            result.append(ExtractedStrategy(**data))
        except Exception:
            pass
    return result


def get_posts_from_bii(handle: str | None = None) -> list[dict]:
    """BIIのtrader_watchから蓄積ポストを読み込む。"""
    data_dir = _BII_TRADER_WATCH / "data"
    if not data_dir.exists():
        logger.warning("BII trader_watch data not found: %s", data_dir)
        return []

    posts = []
    handles = [handle] if handle else [p.name for p in data_dir.iterdir() if p.is_dir()]
    for h in handles:
        h_dir = data_dir / h
        if not h_dir.is_dir():
            continue
        for f in sorted(h_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                posts.extend(data)
            except Exception:
                pass
    return posts
