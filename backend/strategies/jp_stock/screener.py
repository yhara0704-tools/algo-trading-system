"""JP Stock Day-Trading Screener.

候補リストからデイトレ適正スコアを算出して上位銘柄を選定する。

スコア基準:
  - 平均日中値幅率 (ATR/Close)  : 高いほど良い（動きが大きい）
  - 平均出来高                  : 高いほど良い（入出しやすい）
  - 値幅の安定性 (std低い)      : 安定した動きが毎日ある
  - 1株あたり価格帯             : 500〜50,000円が理想

候補: Nikkei225 上位＋デイトレ人気銘柄を事前リストアップ
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)

# デイトレ候補プールリスト
# 選定基準: 出来高上位・ボラあり・スプレッドが薄い
CANDIDATE_POOL: list[dict] = [
    # ── 超流動性・高ボラ ──────────────────────────────────────────────────
    {"symbol": "9984.T",  "name": "SoftBank Group",  "sector": "通信",   "note": "米テック連動・超ボラ"},
    {"symbol": "6758.T",  "name": "Sony Group",       "sector": "電機",   "note": "国際分散・安定ボラ"},
    {"symbol": "7203.T",  "name": "Toyota",           "sector": "自動車", "note": "日経先物連動・超流動"},
    {"symbol": "8306.T",  "name": "MUFG",             "sector": "銀行",   "note": "金利感応・超出来高"},
    {"symbol": "8316.T",  "name": "SMBC",             "sector": "銀行",   "note": "金利感応・超出来高"},
    # ── 高ベータ・トレンド系 ──────────────────────────────────────────────
    {"symbol": "6861.T",  "name": "Keyence",          "sector": "電機",   "note": "トレンド強い"},
    {"symbol": "6954.T",  "name": "Fanuc",            "sector": "機械",   "note": "設備投資指標"},
    {"symbol": "6367.T",  "name": "Daikin",           "sector": "機械",   "note": "高値幅・安定トレンド"},
    {"symbol": "7974.T",  "name": "Nintendo",         "sector": "ゲーム", "note": "触媒反応・ボラ高"},
    {"symbol": "6098.T",  "name": "Recruit Holdings", "sector": "サービス","note":"テック隣接・流動高"},
    # ── セクター代表・安定流動性 ──────────────────────────────────────────
    {"symbol": "9432.T",  "name": "NTT",              "sector": "通信",   "note": "ディフェンシブ・超出来高"},
    {"symbol": "9433.T",  "name": "KDDI",             "sector": "通信",   "note": "安定・高配当"},
    {"symbol": "4063.T",  "name": "Shin-Etsu Chem",   "sector": "化学",   "note": "半導体材料・ボラ"},
    {"symbol": "4568.T",  "name": "Daiichi Sankyo",   "sector": "医薬",   "note": "触媒多・値動き大"},
    {"symbol": "6645.T",  "name": "Omron",            "sector": "電機",   "note": "FA株・テクニカル効く"},
    {"symbol": "7267.T",  "name": "Honda",            "sector": "自動車", "note": "トヨタ代替・出来高"},
    {"symbol": "8058.T",  "name": "Mitsubishi Corp",  "sector": "商社",   "note": "資源連動・ボラ"},
    {"symbol": "8031.T",  "name": "Mitsui & Co",      "sector": "商社",   "note": "資源連動・ボラ"},
    {"symbol": "6501.T",  "name": "Hitachi",          "sector": "電機",   "note": "ITシフト・高ベータ"},
    {"symbol": "2413.T",  "name": "M3",               "sector": "医療IT", "note": "成長株・高ボラ"},
    # ── 高ボラ・新興系 ─────────────────────────────────────────────────────────
    # {"symbol": "6600.T", "name": "Kioxia", "sector": "半導体", "note": "2024上場・yfinance未対応のため一時除外"},
    {"symbol": "4385.T",  "name": "Mercari",           "sector": "IT",    "note": "高ボラ・出来高���定"},
    {"symbol": "3697.T",  "name": "SHIFT",             "sector": "IT",    "note": "テック成長株・ボラあり"},
    {"symbol": "4369.T",  "name": "Tri Chemical",      "sector": "化学",  "note": "半導体材料・急騰銘柄"},
]


@dataclass
class ScreenResult:
    symbol:      str
    name:        str
    sector:      str
    note:        str
    avg_atr_pct: float   # 平均ATR / 終値 (%)
    avg_volume:  float   # 平均出来高（千株）
    atr_std:     float   # ATR安定性（低いほど良い）
    price:       float   # 現在株価
    lot_cost:    float   # 1単元コスト（円）= price × 100
    affordable:  bool    = True   # 資金内で取引可能か
    score:       float   = 0.0   # 総合スコア
    selected:    bool    = False


def is_affordable(price: float, capital_jpy: float = 200_000.0,
                  margin: float = 3.33, position_pct: float = 0.5,
                  lot_size: int = 100) -> bool:
    """指定資金・信用倍率で1単元が買えるか判定。"""
    max_pos = capital_jpy * margin * position_pct
    return price * lot_size <= max_pos


async def screen_stocks(top_n: int = 5,
                        capital_jpy: float = 200_000.0,
                        margin: float = 3.33) -> list[ScreenResult]:
    """候補全銘柄をスクリーニングしてスコア上位 top_n を返す。"""
    results = []
    tasks   = [_score_symbol(c) for c in CANDIDATE_POOL]
    scored  = await asyncio.gather(*tasks, return_exceptions=True)

    for item, res in zip(CANDIDATE_POOL, scored):
        if isinstance(res, Exception) or res is None:
            logger.debug("Skip %s: %s", item["symbol"], res)
            continue
        res.affordable = is_affordable(res.price, capital_jpy=capital_jpy, margin=margin)
        results.append(res)

    # スコア順にソート（買えない銘柄は下位に）
    results.sort(key=lambda r: (r.affordable, r.score), reverse=True)

    # 上位 top_n を選択（affordableな銘柄のみ）
    selected_count = 0
    for r in results:
        if r.affordable and selected_count < top_n:
            r.selected = True
            selected_count += 1

    return results


async def _score_symbol(candidate: dict) -> ScreenResult | None:
    sym = candidate["symbol"]
    try:
        loop = asyncio.get_event_loop()

        # J-Quantsが使える場合は優先（60日・高品質データ）
        from backend.feeds.jquants_client import is_available, get_daily_quotes_df
        if is_available():
            try:
                df = await asyncio.wait_for(
                    get_daily_quotes_df(sym, days=60),
                    timeout=20,
                )
            except Exception as e:
                logger.debug("J-Quants failed for %s, fallback to yfinance: %s", sym, e)
                df = None
        else:
            df = None

        # フォールバック: yfinance
        if df is None or len(df) < 10:
            df = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _fetch_hist(sym)),
                timeout=20,
            )

        if df is None or len(df) < 10:
            return None

        # ── 指標計算 ───────────────────────────────────────────────────────────
        df["atr"]     = df["high"] - df["low"]
        df["atr_pct"] = df["atr"] / df["close"] * 100

        avg_atr   = df["atr_pct"].mean()
        atr_std   = df["atr_pct"].std()
        avg_vol   = df["volume"].mean() / 1000   # 千株
        price     = float(df["close"].iloc[-1])

        # 出来高トレンド（直近20日 vs 全体��均）— 増加傾向は良いサイン
        vol_trend = 0.0
        if len(df) >= 20:
            recent_vol = df["volume"].tail(20).mean()
            vol_trend  = (recent_vol / avg_vol / 1000 - 1) * 100  # %

        # スキャル適性スコア
        # 高ATR × 安定 × 高出来高 × 適正価格帯
        price_ok  = 1.0 if 500 <= price <= 50_000 else 0.3
        score = (
            avg_atr   * 25 +              # ボラ重視（スキャル命��
            min(avg_vol / 500, 8) +       # 出来高（8でキャップ、流動性）
            max(vol_trend * 0.05, 0) +    # 出来高増加トレンド��ーナス
            price_ok  * 3 -
            atr_std   * 4                 # 不安定ペナルティ（毎日安定して動くのが理想）
        )

        return ScreenResult(
            symbol=sym, name=candidate["name"],
            sector=candidate["sector"], note=candidate["note"],
            avg_atr_pct=round(avg_atr, 3),
            avg_volume=round(avg_vol, 0),
            atr_std=round(atr_std, 3),
            price=price,
            lot_cost=round(price * 100, 0),   # 1単元(100株)コスト
            score=round(score, 2),
        )
    except Exception as exc:
        logger.debug("Score failed %s: %s", sym, exc)
        return None


def _fetch_hist(symbol: str) -> pd.DataFrame | None:
    import yfinance as yf
    ticker = yf.Ticker(symbol)
    df = ticker.history(period="30d", interval="1d", auto_adjust=True)
    if df is None or df.empty:
        return None
    df.columns = [c.lower() for c in df.columns]
    return df[["open", "high", "low", "close", "volume"]]
