"""J-Quants API クライアント（V2 SDK ラッパー）。

jquants-api-client v2 を使用。
環境変数:
  JQUANTS_API_KEY  ... ダッシュボードのAPIキー（V2用）
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, timedelta
from functools import lru_cache

# J-Quants無料プランのレートリミット対策: 同時リクエスト数を3に制限
_semaphore: asyncio.Semaphore | None = None

def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(3)
    return _semaphore

logger = logging.getLogger(__name__)

_api_key = os.getenv("JQUANTS_API_KEY") or os.getenv("JQUANTS_REFRESH_TOKEN", "")


def is_available() -> bool:
    """J-Quants APIキーが設定されているか。"""
    return bool(_api_key)


@lru_cache(maxsize=1)
def _get_client():
    """ClientV2シングルトンを返す。"""
    import jquantsapi
    return jquantsapi.ClientV2(api_key=_api_key)


async def get_daily_quotes_df(symbol: str, days: int = 60):
    """日足OHLCVをpandas DataFrameで返す。

    Returns:
        DataFrame with columns: open, high, low, close, volume
        index: DatetimeIndex
    """
    import asyncio
    import pandas as pd

    code = _normalize_code(symbol)
    loop = asyncio.get_event_loop()

    def _fetch():
        cli = _get_client()
        df = cli.get_eq_bars_daily(code=code)
        if df is None or df.empty:
            return None
        # カラム名を小文字統一
        col_map = {"O": "open", "H": "high", "L": "low",
                   "C": "close", "Vo": "volume", "Va": "turnover",
                   "AdjO": "adj_open", "AdjH": "adj_high",
                   "AdjL": "adj_low", "AdjC": "adj_close",
                   "AdjVo": "adj_volume"}
        df = df.rename(columns=col_map)
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        return df.tail(days)

    try:
        async with _get_semaphore():
            await asyncio.sleep(0.2)   # 429対策: リクエスト間に少し間隔を空ける
            return await loop.run_in_executor(None, _fetch)
    except Exception as e:
        logger.warning("J-Quants daily fetch failed for %s: %s", symbol, e)
        return None


async def get_5min_quotes_df(symbol: str, days: int = 30):
    """5分足OHLCVをpandas DataFrameで返す。

    Returns:
        DataFrame with columns: open, high, low, close, volume
        index: DatetimeIndex (Asia/Tokyo)
    """
    import asyncio
    import pandas as pd
    from datetime import date, timedelta

    code = _normalize_code(symbol)
    loop = asyncio.get_event_loop()

    def _fetch():
        cli = _get_client()
        today = date.today()
        from_date = (today - timedelta(days=days)).strftime("%Y%m%d")
        to_date = today.strftime("%Y%m%d")
        df = cli.get_eq_bars_5minute(code=code, from_yyyymmdd=from_date, to_yyyymmdd=to_date)
        if df is None or df.empty:
            return None
        df = df.rename(columns={"O": "open", "H": "high", "L": "low", "C": "close", "Vo": "volume"})
        df["datetime"] = pd.to_datetime(df["Date"].astype(str) + " " + df["Time"].astype(str))
        df = df.set_index("datetime")
        df.index = df.index.tz_localize("Asia/Tokyo")
        return df[["open", "high", "low", "close", "volume"]].sort_index()

    try:
        async with _get_semaphore():
            await asyncio.sleep(0.2)
            return await loop.run_in_executor(None, _fetch)
    except Exception as e:
        logger.warning("J-Quants 5min fetch failed for %s: %s", symbol, e)
        return None


async def get_stock_list() -> list[dict]:
    """上場銘柄マスタを返す。"""
    import asyncio
    loop = asyncio.get_event_loop()

    def _fetch():
        cli = _get_client()
        df = cli.get_list()
        return df.to_dict(orient="records")

    return await loop.run_in_executor(None, _fetch)


async def ping() -> bool:
    """接続テスト。"""
    try:
        lst = await get_stock_list()
        return len(lst) > 0
    except Exception as e:
        logger.error("J-Quants ping failed: %s", e)
        return False


def _normalize_code(symbol: str) -> str:
    """'7203.T' → '7203'"""
    return symbol.split(".")[0]
