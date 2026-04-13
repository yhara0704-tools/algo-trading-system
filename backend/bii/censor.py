"""BII発信前コンテンツ検閲モジュール.

目的: note/X投稿を通じたツール・手法・ロジックの流出を防ぐ。

検閲ルール:
  1. 日次JSONのホワイトリスト制 — 許可フィールド以外は自動除去
  2. テキストのブロックリスト制 — 禁止ワードを自動マスク
  3. 構造チェック — スキーマ違反は書き込みをブロック

許可フィールド（日次JSON）:
  date, phase, pnl_today_jpy, pnl_cumulative_jpy, trade_count, win_count

禁止情報（テキスト）:
  - ツール名: J-Quants, yfinance, Binance, FastAPI, TradingView 等
  - 戦略名: ORB, VWAP, EMA, RSI, Bollinger 等の具体的なロジック名
  - パラメータ: stop_loss, take_profit, position_pct 等の数値設定
  - 銘柄リスト・監視銘柄
  - APIキー・トークン類（万が一の混入防止）
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── 日次JSONのホワイトリスト ─────────────────────────────────────────────────
DAILY_JSON_WHITELIST: set[str] = {
    "date",
    "phase",
    "pnl_today_jpy",
    "pnl_cumulative_jpy",
    "trade_count",
    "win_count",
    "symbol",          # 当日実際に取引した銘柄コードのみ（監視リスト不可）
}

# symbol は取引ゼロ日は省略可能なのでオプション扱い
_OPTIONAL_FIELDS: set[str] = {"symbol"}

# ── テキストブロックリスト ────────────────────────────────────────────────────
# (パターン, 置換文字列)
_TEXT_BLOCKLIST: list[tuple[re.Pattern, str]] = [
    # ツール・ライブラリ名
    (re.compile(r"J-?Quants", re.I),          "データプロバイダー"),
    (re.compile(r"yfinance", re.I),            "データプロバイダー"),
    (re.compile(r"Binance", re.I),             "取引所API"),
    (re.compile(r"FastAPI", re.I),             "バックエンド"),
    (re.compile(r"TradingView", re.I),         "チャートツール"),
    (re.compile(r"Pushover", re.I),            "通知ツール"),
    (re.compile(r"jquantsapi", re.I),          "データプロバイダー"),
    (re.compile(r"uvicorn", re.I),             "サーバー"),

    # 戦略・ロジック名
    (re.compile(r"\bORB\b"),                   "戦略A"),
    (re.compile(r"Opening\s*Range\s*Breakout", re.I), "戦略A"),
    (re.compile(r"\bVWAP\b"),                  "戦略B"),
    (re.compile(r"Vwap\s*Reversion", re.I),    "戦略B"),
    (re.compile(r"\bEMA\s*Cross", re.I),       "戦略C"),
    (re.compile(r"\bRSI\b.*\bBolling", re.I),  "戦略D"),
    (re.compile(r"Momentum\s*5[mM]in", re.I),  "戦略E"),
    (re.compile(r"\bJPScalp\b", re.I),         "戦略F"),
    (re.compile(r"\bJPBreakout\b", re.I),      "戦略G"),

    # パラメータ・設定値
    (re.compile(r"position_pct\s*[=:]\s*[\d.]+", re.I),    "[設定値]"),
    (re.compile(r"stop.?loss\s*[=:]\s*[\d.]+%?", re.I),    "[設定値]"),
    (re.compile(r"take.?profit\s*[=:]\s*[\d.]+%?", re.I),  "[設定値]"),
    (re.compile(r"margin\s*[=:]\s*[\d.]+", re.I),          "[設定値]"),
    (re.compile(r"limit_slip\w*\s*[=:]\s*[\d.]+", re.I),   "[設定値]"),

    # 銘柄コード（東証コード xxxx.T 形式）
    (re.compile(r"\b\d{4}\.T\b"),              "[銘柄]"),

    # APIキー・トークン類（英数字40文字以上の連続）
    (re.compile(r"[A-Za-z0-9_\-]{40,}"),       "[REDACTED]"),

    # ファイルパス
    (re.compile(r"/root/algo[-_]?trading[-_]?system\S*", re.I), "[パス]"),
    (re.compile(r"/root/algo_shared\S*", re.I),                  "[パス]"),
    (re.compile(r"/Users/\w+/algo[-_]?trading[-_]?system\S*", re.I), "[パス]"),
    (re.compile(r"/Users/\w+/algo_shared\S*", re.I),                  "[パス]"),
]

# ── フェーズの許可値 ─────────────────────────────────────────────────────────
ALLOWED_PHASES: set[str] = {"backtest", "paper", "live"}


def sanitize_daily_json(data: dict[str, Any]) -> dict[str, Any]:
    """日次JSONをホワイトリストでフィルタリングして返す。

    許可フィールド以外は除去。型・値のバリデーションも実施。
    Returns:
        クリーンなdict
    Raises:
        ValueError: 必須フィールド不足またはphase不正
    """
    cleaned: dict[str, Any] = {}

    # ホワイトリスト適用
    for key in DAILY_JSON_WHITELIST:
        if key in data:
            cleaned[key] = data[key]

    removed = set(data.keys()) - DAILY_JSON_WHITELIST
    if removed:
        logger.warning("BII censor: removed fields %s", removed)

    # 必須フィールドチェック（オプションフィールドは除外）
    required = DAILY_JSON_WHITELIST - _OPTIONAL_FIELDS
    missing = required - set(cleaned.keys())
    if missing:
        raise ValueError(f"BII daily JSON: 必須フィールドが不足: {missing}")

    # phaseバリデーション
    if cleaned.get("phase") not in ALLOWED_PHASES:
        raise ValueError(f"BII daily JSON: 不正なphase値: {cleaned.get('phase')}")

    # 数値サニタイズ（NaN/Inf対策）
    for key in ("pnl_today_jpy", "pnl_cumulative_jpy"):
        v = cleaned.get(key, 0)
        if not isinstance(v, (int, float)) or v != v:  # NaN check
            logger.warning("BII censor: invalid numeric %s=%s, set to 0", key, v)
            cleaned[key] = 0

    return cleaned


def sanitize_text(text: str) -> str:
    """テキストからブロックリストの語句をマスクして返す。"""
    result = text
    for pattern, replacement in _TEXT_BLOCKLIST:
        new = pattern.sub(replacement, result)
        if new != result:
            logger.info("BII censor: masked pattern '%s'", pattern.pattern[:40])
            result = new
    return result


def check_and_log(data: dict[str, Any], context: str = "") -> list[str]:
    """dataの全文字列値にブロックリストをチェックし、検出した違反を返す（除去はしない）。

    Returns:
        違反パターンのリスト（空なら問題なし）
    """
    violations: list[str] = []
    text = str(data)
    for pattern, _ in _TEXT_BLOCKLIST:
        if pattern.search(text):
            violations.append(f"{context}: pattern '{pattern.pattern[:40]}'")
    return violations
