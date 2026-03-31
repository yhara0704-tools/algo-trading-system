"""PTS（夜間取引）スクリーナー — 翌営業日の監視銘柄を事前選定する。

PTSの直接APIは非公開のため、前営業日の出来高異常・値動き・モメンタムを
代替指標として使い、翌日に動きそうな銘柄をスコアリングする。

採点ロジック:
  - 出来高比率     (昨日 / 5日平均): 急増は需給変化のサイン
  - 前日日中値幅率 (high-low) / close: 大きいほどモメンタムあり
  - 前日変化率     |close - open| / open: 勢いの確認
  - 価格帯ボーナス 500〜30000円が操作しやすい
  - 連続上昇/下降  3日以上のトレンド継続はブレイクアウト候補

使い方:
  results = await pts_screen(top_n=5)
  # 通常の ScreenResult と互換。is_pts_candidate=True のものが追加銘柄。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import pandas as pd

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# PTS候補プール — 通常のデイトレ候補より広めに取る
# 値嵩株・出来高薄も含め、前日急動組を拾うためにプールを広げる
PTS_CANDIDATE_POOL: list[dict] = [
    # ── 指数寄与度高・超流動 ─────────────────────────────────────────────────
    {"symbol": "9984.T",  "name": "SoftBank Group",   "sector": "通信"},
    {"symbol": "6758.T",  "name": "Sony Group",        "sector": "電機"},
    {"symbol": "7203.T",  "name": "Toyota",            "sector": "自動車"},
    {"symbol": "8306.T",  "name": "MUFG",              "sector": "銀行"},
    {"symbol": "8316.T",  "name": "SMBC",              "sector": "銀行"},
    {"symbol": "6861.T",  "name": "Keyence",           "sector": "電機"},
    {"symbol": "6954.T",  "name": "Fanuc",             "sector": "機械"},
    {"symbol": "6367.T",  "name": "Daikin",            "sector": "機械"},
    {"symbol": "7974.T",  "name": "Nintendo",          "sector": "ゲーム"},
    {"symbol": "6098.T",  "name": "Recruit Holdings",  "sector": "サービス"},
    {"symbol": "9432.T",  "name": "NTT",               "sector": "通信"},
    {"symbol": "9433.T",  "name": "KDDI",              "sector": "通信"},
    {"symbol": "4063.T",  "name": "Shin-Etsu Chem",    "sector": "化学"},
    {"symbol": "4568.T",  "name": "Daiichi Sankyo",    "sector": "医薬"},
    {"symbol": "6645.T",  "name": "Omron",             "sector": "電機"},
    {"symbol": "7267.T",  "name": "Honda",             "sector": "自動車"},
    {"symbol": "8058.T",  "name": "Mitsubishi Corp",   "sector": "商社"},
    {"symbol": "8031.T",  "name": "Mitsui & Co",       "sector": "商社"},
    {"symbol": "6501.T",  "name": "Hitachi",           "sector": "電機"},
    {"symbol": "2413.T",  "name": "M3",                "sector": "医療IT"},
    # ── 半導体・テック関連 ────────────────────────────────────────────────────
    {"symbol": "6723.T",  "name": "Renesas",           "sector": "半導体"},
    {"symbol": "8035.T",  "name": "Tokyo Electron",    "sector": "半導体"},
    {"symbol": "6920.T",  "name": "Lasertec",          "sector": "半導体"},
    {"symbol": "4661.T",  "name": "Oriental Land",     "sector": "レジャー"},
    {"symbol": "3382.T",  "name": "Seven & i",         "sector": "小売"},
    # ── 金融・保険 ────────────────────────────────────────────────────────────
    {"symbol": "8411.T",  "name": "Mizuho FG",         "sector": "銀行"},
    {"symbol": "8766.T",  "name": "Tokio Marine",      "sector": "保険"},
    {"symbol": "8604.T",  "name": "Nomura Holdings",   "sector": "証券"},
    # ── エネルギー・素材 ─────────────────────────────────────────────────────
    {"symbol": "5020.T",  "name": "ENEOS Holdings",    "sector": "エネルギー"},
    {"symbol": "4911.T",  "name": "Shiseido",          "sector": "化粧品"},
]


@dataclass
class PTSResult:
    symbol:            str
    name:              str
    sector:            str
    price:             float
    prev_volume_ratio: float   # 前日出来高 / 5日平均出来高
    prev_range_pct:    float   # 前日 (high-low)/close * 100
    prev_change_pct:   float   # 前日 |close-open|/open * 100
    trend_days:        int     # 上昇継続日数（負=下降継続）
    pts_score:         float   # 総合スコア
    signal:            str     # "breakout_candidate" | "momentum" | "reversion" | "watch"
    selected:          bool    = False
    # ScreenResult と統合するための互換フィールド
    avg_atr_pct:       float   = 0.0
    avg_volume:        float   = 0.0
    atr_std:           float   = 0.0
    note:              str     = "PTS候補"
    is_pts_candidate:  bool    = True


async def pts_screen(top_n: int = 5) -> list[PTSResult]:
    """前日の動きからPTS候補銘柄をスコアリングして返す。

    top_n: selected=True にする上位件数
    """
    tasks = [_score_pts(c) for c in PTS_CANDIDATE_POOL]
    scored = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[PTSResult] = []
    for item, res in zip(PTS_CANDIDATE_POOL, scored):
        if isinstance(res, Exception) or res is None:
            logger.debug("PTS skip %s: %s", item["symbol"], res)
            continue
        results.append(res)

    results.sort(key=lambda r: r.pts_score, reverse=True)

    for i, r in enumerate(results):
        r.selected = i < top_n

    logger.info("PTS screen: %d/%d candidates scored. Top=%s",
                len(results), len(PTS_CANDIDATE_POOL),
                [r.symbol for r in results[:top_n]])
    return results


async def _score_pts(candidate: dict) -> PTSResult | None:
    sym = candidate["symbol"]
    try:
        loop = asyncio.get_event_loop()
        df = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _fetch_recent(sym)),
            timeout=20,
        )
        if df is None or len(df) < 6:
            return None

        price = float(df["close"].iloc[-1])
        if price < 100:  # 極端な低位株は除外
            return None

        # 前日データ（最終行 = 今日の場中なら-2、引け後なら-1）
        # yfinance の period="7d" interval="1d" は確定日足を返すため
        # 最終行が前営業日終値になる
        prev = df.iloc[-1]
        prev_vol  = float(prev["volume"])
        avg_5d_vol = df["volume"].iloc[-6:-1].mean()
        vol_ratio  = prev_vol / avg_5d_vol if avg_5d_vol > 0 else 1.0

        prev_range_pct  = (float(prev["high"]) - float(prev["low"])) / float(prev["close"]) * 100
        prev_change_pct = abs(float(prev["close"]) - float(prev["open"])) / float(prev["open"]) * 100

        # トレンド継続日数
        closes = df["close"].values
        trend_days = _calc_trend_days(closes)

        # スコアリング
        price_bonus = 1.0 if 500 <= price <= 30_000 else 0.3
        score = (
            vol_ratio       * 3.0 +   # 出来高急増 (最重要)
            prev_range_pct  * 1.5 +   # 前日値幅
            prev_change_pct * 1.0 +   # 前日方向性
            abs(trend_days) * 0.5 +   # トレンド継続
            price_bonus     * 1.0
        )

        # シグナル分類
        if vol_ratio >= 2.0 and prev_range_pct >= 2.0:
            signal = "breakout_candidate"
        elif abs(trend_days) >= 3:
            signal = "momentum"
        elif vol_ratio >= 1.5 and prev_change_pct >= 1.5:
            signal = "watch"
        else:
            signal = "normal"

        # 5日平均ATRも計算（ScreenResult互換）
        df["atr"] = df["high"] - df["low"]
        avg_atr_pct = (df["atr"] / df["close"] * 100).mean()

        return PTSResult(
            symbol=sym,
            name=candidate["name"],
            sector=candidate["sector"],
            price=round(price, 0),
            prev_volume_ratio=round(vol_ratio, 2),
            prev_range_pct=round(prev_range_pct, 2),
            prev_change_pct=round(prev_change_pct, 2),
            trend_days=trend_days,
            pts_score=round(score, 2),
            signal=signal,
            avg_atr_pct=round(avg_atr_pct, 3),
            avg_volume=round(df["volume"].mean() / 1000, 0),
        )
    except Exception as e:
        logger.debug("PTS score error %s: %s", sym, e)
        return None


def _fetch_recent(symbol: str) -> pd.DataFrame | None:
    import yfinance as yf
    ticker = yf.Ticker(symbol)
    df = ticker.history(period="10d", interval="1d", auto_adjust=True)
    if df is None or df.empty:
        return None
    df.columns = [c.lower() for c in df.columns]
    return df[["open", "high", "low", "close", "volume"]]


def _calc_trend_days(closes) -> int:
    """終値の連続上昇/下降日数を返す。正=上昇、負=下降。"""
    if len(closes) < 2:
        return 0
    direction = None
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        diff = closes[i] - closes[i - 1]
        if diff == 0:
            break
        d = 1 if diff > 0 else -1
        if direction is None:
            direction = d
            count = 1
        elif d == direction:
            count += 1
        else:
            break
    return (direction or 1) * count
