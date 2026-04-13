"""PTS（夜間取引）スクリーナー — 翌営業日の監視銘柄を事前選定する.

選定ロジック（2段階）:
  1. 価格フィルター: MAX_STOCK_PRICE 以下の銘柄のみ対象（買えない株は除外）
  2. 活動度ランキング: 過去N日間の「|変動率| × 売買代金」平均で順位付け
     → 上位top_n銘柄を選択してから詳細スコアリング

使い方:
  results = await pts_screen(top_n=15, lookback_days=30)
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# ── ユニバース（広め） ─────────────────────────────────────────────────────────
# 価格フィルター後にランキングするので、値嵩株も含めて網羅する
# 実際の取引可否はMAX_STOCK_PRICEで自動判断される
PTS_CANDIDATE_POOL: list[dict] = [
    # ── メガキャップ・超流動 ───────────────────────────────────────────────────
    {"symbol": "9984.T",  "name": "SoftBank Group",    "sector": "通信"},
    {"symbol": "6758.T",  "name": "Sony Group",         "sector": "電機"},
    {"symbol": "7203.T",  "name": "Toyota",             "sector": "自動車"},
    {"symbol": "8306.T",  "name": "MUFG",               "sector": "銀行"},
    {"symbol": "8316.T",  "name": "SMBC",               "sector": "銀行"},
    {"symbol": "8411.T",  "name": "Mizuho FG",          "sector": "銀行"},
    {"symbol": "8604.T",  "name": "Nomura Holdings",    "sector": "証券"},
    {"symbol": "9432.T",  "name": "NTT",                "sector": "通信"},
    {"symbol": "9433.T",  "name": "KDDI",               "sector": "通信"},
    {"symbol": "9434.T",  "name": "SoftBank Corp",      "sector": "通信"},
    # ── 自動車・重工 ─────────────────────────────────────────────────────────
    {"symbol": "7201.T",  "name": "Nissan",             "sector": "自動車"},
    {"symbol": "7267.T",  "name": "Honda",              "sector": "自動車"},
    {"symbol": "7211.T",  "name": "Mitsubishi Motors",  "sector": "自動車"},
    {"symbol": "7269.T",  "name": "Suzuki",             "sector": "自動車"},
    # ── 電機・機械 ───────────────────────────────────────────────────────────
    {"symbol": "6501.T",  "name": "Hitachi",            "sector": "電機"},
    {"symbol": "6752.T",  "name": "Panasonic",          "sector": "電機"},
    {"symbol": "6753.T",  "name": "Sharp",              "sector": "電機"},
    {"symbol": "6861.T",  "name": "Keyence",            "sector": "電機"},
    {"symbol": "6645.T",  "name": "Omron",              "sector": "電機"},
    {"symbol": "6954.T",  "name": "Fanuc",              "sector": "機械"},
    {"symbol": "6367.T",  "name": "Daikin",             "sector": "機械"},
    {"symbol": "6326.T",  "name": "Kubota",             "sector": "機械"},
    # ── 半導体・テック ────────────────────────────────────────────────────────
    {"symbol": "6723.T",  "name": "Renesas",            "sector": "半導体"},
    {"symbol": "8035.T",  "name": "Tokyo Electron",     "sector": "半導体"},
    {"symbol": "6920.T",  "name": "Lasertec",           "sector": "半導体"},
    # {"symbol": "6600.T",  "name": "Kioxia",  "sector": "半導体"},  # yfinance未対応（2024上場、データなし）
    {"symbol": "6613.T",  "name": "QD Laser",           "sector": "光半導体"},
    # ── IT・インターネット ────────────────────────────────────────────────────
    {"symbol": "4385.T",  "name": "Mercari",            "sector": "IT"},
    {"symbol": "4689.T",  "name": "LY Corp",            "sector": "IT"},
    {"symbol": "4751.T",  "name": "CyberAgent",         "sector": "IT"},
    {"symbol": "4755.T",  "name": "Rakuten",            "sector": "IT"},
    {"symbol": "9449.T",  "name": "GMO Internet",       "sector": "IT"},
    {"symbol": "9468.T",  "name": "Kadokawa",           "sector": "IT"},
    {"symbol": "2432.T",  "name": "DeNA",               "sector": "IT"},
    # ── 医療・ヘルスケア ─────────────────────────────────────────────────────
    {"symbol": "2413.T",  "name": "M3",                 "sector": "医療IT"},
    {"symbol": "4568.T",  "name": "Daiichi Sankyo",     "sector": "医薬"},
    {"symbol": "4502.T",  "name": "Takeda",             "sector": "医薬"},
    {"symbol": "4592.T",  "name": "SanBio",             "sector": "バイオ"},
    # ── 商社・エネルギー ─────────────────────────────────────────────────────
    {"symbol": "8058.T",  "name": "Mitsubishi Corp",    "sector": "商社"},
    {"symbol": "8031.T",  "name": "Mitsui & Co",        "sector": "商社"},
    {"symbol": "8002.T",  "name": "Marubeni",           "sector": "商社"},
    {"symbol": "8053.T",  "name": "Sumitomo Corp",      "sector": "商社"},
    {"symbol": "5020.T",  "name": "ENEOS Holdings",     "sector": "エネルギー"},
    {"symbol": "1605.T",  "name": "INPEX",              "sector": "エネルギー"},
    {"symbol": "5401.T",  "name": "Nippon Steel",       "sector": "鉄鋼"},
    # ── 海運・物流 ───────────────────────────────────────────────────────────
    {"symbol": "9101.T",  "name": "NYK Line",           "sector": "海運"},
    {"symbol": "9104.T",  "name": "MOL",                "sector": "海運"},
    {"symbol": "9107.T",  "name": "Kawasaki Kisen",     "sector": "海運"},
    # ── 化学・素材 ───────────────────────────────────────────────────────────
    {"symbol": "4063.T",  "name": "Shin-Etsu Chem",     "sector": "化学"},
    {"symbol": "4911.T",  "name": "Shiseido",           "sector": "化粧品"},
    # ── サービス・レジャー ───────────────────────────────────────────────────
    {"symbol": "6098.T",  "name": "Recruit Holdings",   "sector": "サービス"},
    {"symbol": "7974.T",  "name": "Nintendo",           "sector": "ゲーム"},
    {"symbol": "4661.T",  "name": "Oriental Land",      "sector": "レジャー"},
    {"symbol": "3382.T",  "name": "Seven & i",          "sector": "小売"},
    {"symbol": "8766.T",  "name": "Tokio Marine",       "sector": "保険"},
    # ── 高ボラ・ユーザー注目銘柄 ─────────────────────────────────────────────
    {"symbol": "3103.T",  "name": "Unitika",            "sector": "繊維"},
    {"symbol": "8136.T",  "name": "Sanrio",             "sector": "エンタメ"},
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
    signal:            str     # "breakout_candidate" | "momentum" | "watch" | "normal"
    activity_score:    float   = 0.0  # |変動率| × 売買代金の過去平均（ランキング用）
    selected:          bool    = False
    # ScreenResult 互換フィールド
    avg_atr_pct:       float   = 0.0
    avg_volume:        float   = 0.0
    atr_std:           float   = 0.0
    note:              str     = "PTS候補"
    is_pts_candidate:  bool    = True


def _load_1d_cache(symbol: str) -> pd.DataFrame | None:
    """1日足キャッシュを読み込む（Mac rsync経由 or yfinance フォールバック）。"""
    _project_root = pathlib.Path(__file__).resolve().parent.parent.parent.parent
    cache_path = (_project_root / "algo_shared" / "ohlcv_cache"
                  / f"{symbol.replace('.', '_')}_1d.parquet")
    if cache_path.exists():
        try:
            df = pd.read_parquet(cache_path)
            if not df.empty:
                return df[["open", "high", "low", "close", "volume"]]
        except Exception:
            pass
    # フォールバック: yfinance
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="40d", interval="1d", auto_adjust=True)
        if df is not None and not df.empty:
            df.columns = [c.lower() for c in df.columns]
            return df[["open", "high", "low", "close", "volume"]]
    except Exception:
        pass
    return None


def rank_by_historical_activity(
    max_price: float,
    lookback_days: int = 30,
    top_n: int = 15,
) -> list[dict]:
    """価格フィルター先 → |変動率|×売買代金 でランキング → 上位top_n返す.

    Args:
        max_price:     1株の上限価格（この価格以下の銘柄のみ対象）
        lookback_days: 過去何日分で活動度を計算するか
        top_n:         返す銘柄数

    Returns:
        PTS_CANDIDATE_POOLの辞書リスト（上位top_n件）
    """
    scores: list[tuple[float, dict]] = []

    for candidate in PTS_CANDIDATE_POOL:
        sym = candidate["symbol"]
        df = _load_1d_cache(sym)
        if df is None or len(df) < 5:
            continue

        # 最新株価で価格フィルター（買えない銘柄は完全除外）
        latest_price = float(df["close"].iloc[-1])
        if latest_price > max_price or latest_price < 100:
            continue

        # 過去lookback_days分を使用
        df_recent = df.tail(lookback_days)

        # 活動度 = |変動率| × 売買代金
        # 変動率 = |close - open| / open
        # 売買代金 = close × volume (円)
        change_pct = (df_recent["close"] - df_recent["open"]).abs() / df_recent["open"]
        trading_value = df_recent["close"] * df_recent["volume"]
        activity = (change_pct * trading_value).mean()

        scores.append((activity, candidate))

    # 活動度の高い順に並べて上位top_n返す
    scores.sort(key=lambda x: -x[0])
    selected = [c for _, c in scores[:top_n]]

    logger.info(
        "rank_by_historical_activity: %d/%d affordable, top%d=%s",
        len(scores), len(PTS_CANDIDATE_POOL), top_n,
        [c["symbol"] for c in selected],
    )
    return selected


async def pts_screen(
    top_n: int = 15,
    lookback_days: int = 30,
    max_price: float = 3_300.0,
) -> list[PTSResult]:
    """価格フィルター→活動度ランキング→詳細スコアリングの2段階選定.

    Args:
        top_n:         最終的に selected=True にする銘柄数
        lookback_days: 活動度計算の遡り日数
        max_price:     1株の上限価格
    """
    # ① 価格フィルター先 + 活動度ランキングで候補を絞る
    ranked_candidates = rank_by_historical_activity(
        max_price=max_price,
        lookback_days=lookback_days,
        top_n=top_n * 3,  # 詳細スコアリング用に多めに取る（上位45→最終15）
    )

    # ② 絞った候補に対して詳細スコアリング
    tasks = [_score_pts(c) for c in ranked_candidates]
    scored = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[PTSResult] = []
    for item, res in zip(ranked_candidates, scored):
        if isinstance(res, Exception) or res is None:
            logger.debug("PTS skip %s: %s", item["symbol"], res)
            continue
        results.append(res)

    results.sort(key=lambda r: r.pts_score, reverse=True)

    for i, r in enumerate(results):
        r.selected = i < top_n

    logger.info("PTS screen: %d scored. Top=%s",
                len(results), [r.symbol for r in results if r.selected])
    return results


async def _score_pts(candidate: dict) -> PTSResult | None:
    sym = candidate["symbol"]
    try:
        loop = asyncio.get_event_loop()
        df = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _load_1d_cache(sym)),
            timeout=20,
        )
        if df is None or len(df) < 6:
            return None

        price = float(df["close"].iloc[-1])
        if price < 100:
            return None

        prev = df.iloc[-1]
        prev_vol   = float(prev["volume"])
        avg_5d_vol = df["volume"].iloc[-6:-1].mean()
        vol_ratio  = prev_vol / avg_5d_vol if avg_5d_vol > 0 else 1.0

        prev_range_pct  = (float(prev["high"]) - float(prev["low"])) / float(prev["close"]) * 100
        prev_change_pct = abs(float(prev["close"]) - float(prev["open"])) / float(prev["open"]) * 100

        closes     = df["close"].values
        trend_days = _calc_trend_days(closes)

        # 活動度スコア（ランキング用）
        change_pct    = (df["close"] - df["open"]).abs() / df["open"]
        trading_value = df["close"] * df["volume"]
        activity_score = float((change_pct * trading_value).mean())

        price_bonus = 1.0 if 500 <= price <= 30_000 else 0.3
        score = (
            vol_ratio       * 3.0 +
            prev_range_pct  * 1.5 +
            prev_change_pct * 1.0 +
            abs(trend_days) * 0.5 +
            price_bonus     * 1.0
        )

        if vol_ratio >= 2.0 and prev_range_pct >= 2.0:
            signal = "breakout_candidate"
        elif abs(trend_days) >= 3:
            signal = "momentum"
        elif vol_ratio >= 1.5 and prev_change_pct >= 1.5:
            signal = "watch"
        else:
            signal = "normal"

        df["atr"]   = df["high"] - df["low"]
        avg_atr_pct = float((df["atr"] / df["close"] * 100).mean())

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
            activity_score=round(activity_score / 1e9, 3),  # 10億円単位で見やすく
            avg_atr_pct=round(avg_atr_pct, 3),
            avg_volume=round(df["volume"].mean() / 1000, 0),
        )
    except Exception as e:
        logger.debug("PTS score error %s: %s", sym, e)
        return None


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
