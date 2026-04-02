#!/usr/bin/env python3
"""ローカルMacでJP株OHLCVを取得してVPSへ転送するブリッジスクリプト。

使い方:
    python scripts/push_ohlcv_cache.py [--dry-run]

仕組み:
    1. PTS_CANDIDATE_POOL の全銘柄を yfinance で取得（1m: 7日, 5m: 30日）
    2. parquet形式で /tmp/ohlcv_cache/ に保存
    3. rsync で bullvps:/root/algo_shared/ohlcv_cache/ に転送

cron登録例（毎朝9:00に実行）:
    0 9 * * 1-5 cd /path/to/algo-trading-system && .venv/bin/python scripts/push_ohlcv_cache.py
"""
from __future__ import annotations

import argparse
import logging
import pathlib
import subprocess
import time

import pandas as pd
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SYMBOLS = [
    # メガキャップ・超流動
    "9984.T", "6758.T", "7203.T", "8306.T", "8316.T", "8411.T", "8604.T",
    "9432.T", "9433.T", "9434.T",
    # 自動車・重工
    "7201.T", "7267.T", "7211.T", "7269.T",
    # 電機・機械
    "6501.T", "6752.T", "6753.T", "6861.T", "6645.T", "6954.T", "6367.T", "6326.T",
    # 半導体・テック
    "6723.T", "8035.T", "6920.T", "6600.T", "6613.T",
    # IT・インターネット
    "4385.T", "4689.T", "4751.T", "4755.T", "9449.T", "9468.T", "2432.T",
    # 医療・ヘルスケア
    "2413.T", "4568.T", "4502.T", "4592.T",
    # 商社・エネルギー
    "8058.T", "8031.T", "8002.T", "8053.T", "5020.T", "1605.T", "5401.T",
    # 海運
    "9101.T", "9104.T", "9107.T",
    # 化学・素材・その他
    "4063.T", "4911.T", "6098.T", "7974.T", "4661.T", "3382.T", "8766.T",
    # ユーザー注目銘柄
    "3103.T", "8136.T",
]

INTERVALS = [
    ("1m",  "7d"),
    ("5m",  "30d"),
    ("1d",  "60d"),
]

OUT_DIR = pathlib.Path("/tmp/ohlcv_cache")
VPS_HOST = "bullvps"
VPS_DIR  = "/root/algo_shared/ohlcv_cache/"


def fetch_one(symbol: str, interval: str, period: str) -> pd.DataFrame | None:
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval, auto_adjust=True)
        if df is None or df.empty:
            return None
        df.columns = [c.lower() for c in df.columns]
        if df.index.tz is None:
            df.index = pd.to_datetime(df.index, utc=True)
        df.index = pd.to_datetime(df.index).tz_convert("Asia/Tokyo")
        return df[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        logger.warning("fetch failed %s %s: %s", symbol, interval, e)
        return None


def main(dry_run: bool = False) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ok = 0
    ng = 0
    for symbol in SYMBOLS:
        for interval, period in INTERVALS:
            df = fetch_one(symbol, interval, period)
            if df is None or df.empty:
                logger.warning("SKIP %s %s (empty)", symbol, interval)
                ng += 1
                continue

            path = OUT_DIR / f"{symbol.replace('.', '_')}_{interval}.parquet"
            df.to_parquet(path)
            logger.info("OK  %s %s → %s (%d rows)", symbol, interval, path.name, len(df))
            ok += 1
            time.sleep(0.3)  # レートリミット対策

    logger.info("fetch done: %d ok / %d ng", ok, ng)

    if dry_run:
        logger.info("[dry-run] rsync をスキップ")
        return

    logger.info("rsync → %s:%s", VPS_HOST, VPS_DIR)
    result = subprocess.run(
        ["rsync", "-avz", "--delete", f"{OUT_DIR}/", f"{VPS_HOST}:{VPS_DIR}"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        logger.info("rsync OK:\n%s", result.stdout.strip())
    else:
        logger.error("rsync FAILED:\n%s", result.stderr.strip())
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
