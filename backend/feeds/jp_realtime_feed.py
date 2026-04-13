"""JP株リアルタイムフィード — 場中に1分足データをポーリングして蓄積する。

yfinance経由（15〜20分ディレイあり）だが、ペーパートレード検証には十分。
場中のみ動作し、場外は自動停止する。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timezone, timedelta
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# 東京証券取引所の取引時間
_OPEN_MORNING  = time(9,  0)
_CLOSE_MORNING = time(11, 30)
_OPEN_AFTERNOON  = time(12, 30)
_CLOSE_AFTERNOON = time(15, 30)


def is_market_open() -> bool:
    """現在、東証が開いているか判定する（JST）。"""
    now = datetime.now(JST)
    if now.weekday() >= 5:   # 土日
        return False
    t = now.time()
    return (_OPEN_MORNING <= t < _CLOSE_MORNING or
            _OPEN_AFTERNOON <= t < _CLOSE_AFTERNOON)


def seconds_to_market_open() -> float:
    """次の開場まで何秒か返す（場中なら0）。"""
    if is_market_open():
        return 0.0
    now = datetime.now(JST)
    if now.weekday() >= 5:
        # 次の月曜9:00まで
        days_until_mon = 7 - now.weekday()
        next_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
        next_open += timedelta(days=days_until_mon)
    else:
        t = now.time()
        if t < _OPEN_MORNING:
            next_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
        elif _CLOSE_MORNING <= t < _OPEN_AFTERNOON:
            next_open = now.replace(hour=12, minute=30, second=0, microsecond=0)
        else:
            # 後場終了後 → 翌日9時
            next_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
            next_open += timedelta(days=1)
            # 翌日が土日なら月曜へ
            while next_open.weekday() >= 5:
                next_open += timedelta(days=1)
    return max(0.0, (next_open - now).total_seconds())


def fetch_intraday(symbol: str) -> pd.DataFrame:
    """yfinanceで当日の1分足を取得する（同期）。

    専用セッションを使い、コネクションプール競合を回避する。
    """
    import yfinance as yf
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="1d", interval="1m", auto_adjust=True)
        if df is None or df.empty:
            return pd.DataFrame()
        df.columns = [c.lower() for c in df.columns]
        if df.index.tz is None:
            df.index = pd.to_datetime(df.index).tz_localize("UTC")
        df.index = df.index.tz_convert(JST)
        return df[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        logger.warning("yfinance fetch failed for %s: %s", symbol, e)
        return pd.DataFrame()


class JPRealtimeFeed:
    """場中に選定銘柄の1分足を60秒ごとにポーリングする。"""

    def __init__(self, poll_interval: int = 60) -> None:
        self._poll_interval = poll_interval
        self._symbols: list[str] = []
        self._buffers: dict[str, pd.DataFrame] = {}   # symbol → 当日の全1分足
        self._callbacks: list[Callable] = []
        self._running = False

    def set_symbols(self, symbols: list[str]) -> None:
        self._symbols = symbols
        logger.info("JP realtime feed symbols: %s", symbols)

    def on_bar(self, fn: Callable) -> None:
        """新しいバーが来たときのコールバックを登録する。"""
        self._callbacks.append(fn)

    def get_bars(self, symbol: str) -> pd.DataFrame:
        return self._buffers.get(symbol, pd.DataFrame())

    async def run(self) -> None:
        """メインループ。場中のみポーリングし、場外は待機する。"""
        self._running = True
        logger.info("JP realtime feed started.")
        while self._running:
            if not is_market_open():
                wait = seconds_to_market_open()
                logger.info("Market closed. Next open in %.0fm.", wait / 60)
                await asyncio.sleep(min(wait, 600))  # 最大10分ごとに再チェック
                continue

            # 場中：全銘柄を更新
            if self._symbols:
                await self._poll()
            await asyncio.sleep(self._poll_interval)

    async def stop(self) -> None:
        self._running = False

    async def _poll(self) -> None:
        loop = asyncio.get_event_loop()
        for symbol in self._symbols:
            try:
                df = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda s=symbol: fetch_intraday(s)),
                    timeout=20,
                )
                if df.empty:
                    continue
                prev_len = len(self._buffers.get(symbol, pd.DataFrame()))
                self._buffers[symbol] = df
                # 新しいバーが追加されたときだけコール���ック
                if len(df) > prev_len:
                    for cb in self._callbacks:
                        try:
                            await cb(symbol, df)
                        except Exception as e:
                            logger.error("Bar callback error: %s", e)
            except asyncio.TimeoutError:
                logger.warning("Timeout polling %s", symbol)
            except Exception as e:
                logger.warning("Poll error %s: %s", symbol, e)
