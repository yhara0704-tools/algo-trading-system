"""TradeGuard — エントリー後の保護・監視・異変検知.

1. 同一セクター銘柄の自動監視 → 異変検知で緊急撤退
2. 曜日×時間帯の勝率記録
3. スリッページの現実的な記録
4. イベント日の識別
5. DD後の復元速度計測
"""
from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import pandas as pd

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "data"

# ── セクターマップ読み込み ────────────────────────────────────────────────────

_sector_map: dict | None = None


def _load_sector_map() -> dict:
    global _sector_map
    if _sector_map is None:
        path = DATA_DIR / "sector_map.json"
        if path.exists():
            _sector_map = json.loads(path.read_text())
        else:
            _sector_map = {}
    return _sector_map


def get_sector(symbol: str) -> str | None:
    """銘柄のセクターを返す。"""
    sm = _load_sector_map()
    for sector, info in sm.items():
        for stock in info.get("domestic", []):
            if stock["symbol"] == symbol:
                return sector
    return None


def get_sector_peers(symbol: str) -> list[str]:
    """同一セクターの他の銘柄を返す。"""
    sm = _load_sector_map()
    sector = get_sector(symbol)
    if not sector:
        return []
    peers = []
    for stock in sm[sector].get("domestic", []):
        if stock["symbol"] != symbol:
            peers.append(stock["symbol"])
    return peers


def get_correlated_symbols(symbol: str) -> list[str]:
    """同一セクター + US proxy を含む監視対象を返す。"""
    sm = _load_sector_map()
    sector = get_sector(symbol)
    if not sector:
        return []
    result = []
    for stock in sm[sector].get("domestic", []):
        if stock["symbol"] != symbol:
            result.append(stock["symbol"])
    for stock in sm[sector].get("us_proxy", []):
        result.append(stock["symbol"])
    return result


# ── 異変検知 ──────────────────────────────────────────────────────────────────

def detect_peer_anomaly(
    peer_prices: dict[str, list[float]],
    threshold_pct: float = -1.5,
    exclude_symbols: set[str] | None = None,
) -> list[dict]:
    """同一セクター銘柄の急落を検知する。

    peer_prices: {symbol: [recent_prices...]} (新しい方が後ろ)
    threshold_pct: この%以上の下落で異変とみなす
    exclude_symbols: 決算日銘柄など、個社要因で除外する銘柄
    """
    anomalies = []
    exclude = exclude_symbols or set()
    for sym, prices in peer_prices.items():
        if sym in exclude:
            continue
        if len(prices) < 2:
            continue
        change_pct = (prices[-1] - prices[0]) / prices[0] * 100
        if change_pct <= threshold_pct:
            anomalies.append({
                "symbol": sym,
                "change_pct": round(change_pct, 2),
                "from": prices[0],
                "to": prices[-1],
            })
    return anomalies


# ── 曜日×時間帯の勝率記録 ─────────────────────────────────────────────────────

_weekday_hour_stats: dict[str, dict] = defaultdict(
    lambda: {"wins": 0, "losses": 0, "total_pnl": 0.0}
)


def record_trade_timing(trade_time: datetime, pnl: float) -> None:
    """取引の曜日×時間帯を記録する。"""
    weekday = trade_time.strftime("%a")  # Mon, Tue, ...
    hour = trade_time.hour
    key = f"{weekday}_{hour:02d}"
    s = _weekday_hour_stats[key]
    if pnl > 0:
        s["wins"] += 1
    else:
        s["losses"] += 1
    s["total_pnl"] += pnl


def get_timing_stats() -> dict[str, dict]:
    """曜日×時間帯の勝率統計を返す。"""
    result = {}
    for key, s in _weekday_hour_stats.items():
        total = s["wins"] + s["losses"]
        if total == 0:
            continue
        result[key] = {
            "wins": s["wins"],
            "losses": s["losses"],
            "win_rate": s["wins"] / total,
            "avg_pnl": s["total_pnl"] / total,
            "total_pnl": s["total_pnl"],
        }
    return result


# ── イベント日識別 ─────────────────────────────────────────────────────────────

# SQ日（毎月第2金曜）、日銀会合（不定期だが大体月2回）
# 静的リストとして主要日を登録（都度更新可能）
EVENT_DATES: dict[str, str] = {
    # 2026年のSQ日（第2金曜）
    "2026-01-09": "SQ", "2026-02-13": "SQ", "2026-03-13": "SQ(メジャー)",
    "2026-04-10": "SQ", "2026-05-08": "SQ", "2026-06-12": "SQ(メジャー)",
    "2026-07-10": "SQ", "2026-08-14": "SQ", "2026-09-11": "SQ(メジャー)",
    "2026-10-09": "SQ", "2026-11-13": "SQ", "2026-12-11": "SQ(メジャー)",
    # 決算シーズン
    "2026-01-28": "決算集中", "2026-01-29": "決算集中", "2026-01-30": "決算集中",
    "2026-04-27": "決算集中", "2026-04-28": "決算集中",
    "2026-07-27": "決算集中", "2026-07-28": "決算集中",
    "2026-10-26": "決算集中", "2026-10-27": "決算集中",
}


def get_event(date_str: str) -> str | None:
    """指定日のイベントを返す。"""
    return EVENT_DATES.get(date_str)


def is_high_risk_day(date_str: str) -> bool:
    """SQ日やメジャーSQ日かどうか。"""
    event = EVENT_DATES.get(date_str, "")
    return "SQ" in event or "決算集中" in event


# ── 決算日チェック ─────────────────────────────────────────────────────────────

_earnings_cache: dict[str, str] = {}  # symbol -> next_earnings_date


async def is_earnings_day(symbol: str, date_str: str) -> bool:
    """指定銘柄が当日決算発表かどうか。yfinanceのcalendarから取得。"""
    # キャッシュがあればそれを使う
    cached = _earnings_cache.get(symbol)
    if cached == date_str:
        return True
    if cached and cached != date_str:
        return False

    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        if cal is not None and not cal.empty:
            # Earnings Date を取得
            if hasattr(cal, 'iloc'):
                for col in cal.columns:
                    val = str(cal[col].iloc[0]) if len(cal[col]) > 0 else ""
                    if date_str in val:
                        _earnings_cache[symbol] = date_str
                        return True
    except Exception:
        pass
    return False


def is_earnings_day_sync(symbol: str, date_str: str) -> bool:
    """同期版（バックテスト用）。キャッシュのみ参照。"""
    return _earnings_cache.get(symbol) == date_str


# ── スリッページ記録 ──────────────────────────────────────────────────────────

_slippage_records: list[dict] = []


def record_slippage(symbol: str, expected_price: float, actual_price: float,
                    side: str = "long") -> None:
    """スリッページを記録する。"""
    slip_pct = abs(actual_price - expected_price) / expected_price * 100
    _slippage_records.append({
        "symbol": symbol,
        "expected": expected_price,
        "actual": actual_price,
        "slip_pct": round(slip_pct, 4),
        "side": side,
    })


def get_avg_slippage(symbol: str = "") -> float:
    """平均スリッページ%を返す。"""
    if symbol:
        records = [r for r in _slippage_records if r["symbol"] == symbol]
    else:
        records = _slippage_records
    if not records:
        return 0.0
    return sum(r["slip_pct"] for r in records) / len(records)


# ── DD復元速度計測 ─────────────────────────────────────────────────────────────

def compute_recovery_stats(daily_pnls: list[float], capital: float) -> dict:
    """最大DD後の復元速度を計測する。"""
    if not daily_pnls:
        return {}

    import numpy as np
    cumulative = np.cumsum([0] + daily_pnls) + capital
    peak = np.maximum.accumulate(cumulative)
    drawdown = cumulative - peak

    # 最大DDのポイント
    max_dd_idx = int(np.argmin(drawdown))
    max_dd_value = float(drawdown[max_dd_idx])
    max_dd_pct = max_dd_value / float(peak[max_dd_idx]) * 100 if peak[max_dd_idx] > 0 else 0

    # 復元にかかった日数
    recovery_days = 0
    if max_dd_idx < len(cumulative) - 1:
        peak_at_dd = float(peak[max_dd_idx])
        for i in range(max_dd_idx + 1, len(cumulative)):
            if cumulative[i] >= peak_at_dd:
                recovery_days = i - max_dd_idx
                break
        if recovery_days == 0:
            recovery_days = -1  # 未回復

    return {
        "max_dd_jpy": round(abs(max_dd_value)),
        "max_dd_pct": round(max_dd_pct, 1),
        "max_dd_day": max_dd_idx,
        "recovery_days": recovery_days,
        "recovered": recovery_days > 0,
    }
