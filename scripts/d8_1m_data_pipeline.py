#!/usr/bin/env python3
"""D8f: 1m OHLCV データ蓄積パイプライン (PoC).

yfinance の 1m データは過去 7 日分まで取得可能。これを **日次 cron で取得し、
algo_shared/ohlcv_cache/{symbol}_1m.pkl に追記蓄積** する仕組み。

目的:
  1. paper の SL slippage 仮説を 1m データで再検証 (D1c タスク)
  2. MicroScalp の per-symbol 連続最適化 (D3 拡張、当日朝 backtest)
  3. Phase 2 動的 lot_multiplier の動意スコア計算 (D7 Phase 2)

仕様:
  - yfinance.download で 7 日 × 1m を取得
  - pkl 既存読込 → 重複除去 → 結合 → 保存
  - 30 日以上前のデータは自動で trim (ファイルサイズ抑制)
  - 失敗時は既存ファイルを変更しない

VPS 設置時は cron 例 (毎日 16:30 = 大引け後):
  30 16 * * 1-5 cd /root/algo-trading-system && python3 scripts/d8_1m_data_pipeline.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

JST = timezone(timedelta(hours=9))
CACHE_DIR = Path("algo_shared/ohlcv_cache")
RETENTION_DAYS = 30  # 30 日分以上は trim


def fetch_1m(symbol: str, days: int = 7) -> pd.DataFrame:
    """yfinance から 1m データを取得 (最大 7 日)."""
    end = datetime.now(JST) + timedelta(days=1)
    start = end - timedelta(days=min(days, 7))
    df = yf.download(symbol, start=start, end=end, interval="1m",
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert("Asia/Tokyo")
    else:
        df.index = df.index.tz_convert("Asia/Tokyo")
    df = df[df.index.map(lambda t: 9 <= t.hour < 15 or (t.hour == 15 and t.minute < 30))]
    return df


def merge_and_save(symbol: str, new_df: pd.DataFrame) -> dict:
    fname = symbol.replace(".", "_") + "_1m.pkl"
    path = CACHE_DIR / fname
    existing = pd.DataFrame()
    if path.exists():
        try:
            existing = pd.read_pickle(path)
            if existing.index.tz is None:
                existing.index = existing.index.tz_localize("Asia/Tokyo")
        except Exception as e:
            print(f"  warn: {symbol} existing read failed: {e}")
            existing = pd.DataFrame()

    if existing.empty:
        merged = new_df
    else:
        # 重複除去 (新データを優先)
        merged = pd.concat([existing, new_df])
        merged = merged[~merged.index.duplicated(keep="last")]
        merged = merged.sort_index()

    # retention trim
    cutoff = datetime.now(JST) - timedelta(days=RETENTION_DAYS)
    merged = merged[merged.index >= cutoff]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    merged.to_pickle(path)
    n_days = len(set(merged.index.date)) if not merged.empty else 0
    return {
        "symbol": symbol, "rows": len(merged), "days": n_days,
        "first": str(merged.index.min()) if not merged.empty else None,
        "last": str(merged.index.max()) if not merged.empty else None,
    }


def main() -> None:
    universe = json.load(open("data/universe_active.json"))
    syms = universe.get("symbols", [])
    # active かつ MicroScalp 含む全銘柄 (1m 必要なものを優先、5m 戦略でも検証用に)
    target_syms = sorted({
        s["symbol"] for s in syms
        if not s.get("observation_only", False) or s.get("force_paper", False)
    })

    print(f"=== D8f: 1m データ蓄積パイプライン ===\n")
    print(f"対象: {len(target_syms)} 銘柄")
    print(f"cache dir: {CACHE_DIR.resolve()}")
    print(f"retention: {RETENTION_DAYS} 日\n")

    results = []
    failed = []
    for sym in target_syms:
        print(f"--- {sym} ---")
        try:
            new_df = fetch_1m(sym, days=7)
            if new_df.empty:
                print(f"  no data")
                failed.append(sym)
                continue
            r = merge_and_save(sym, new_df)
            print(f"  rows={r['rows']:5d} days={r['days']:2d} "
                  f"first={r['first']} last={r['last']}")
            results.append(r)
        except Exception as e:
            print(f"  error: {e}")
            failed.append(sym)
        time.sleep(0.5)

    summary = {
        "generated_at": datetime.now(JST).isoformat(),
        "n_processed": len(results), "n_failed": len(failed),
        "failed_symbols": failed,
        "retention_days": RETENTION_DAYS,
        "results": results,
    }
    Path("data/d8_1m_pipeline_status.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"\n=== summary ===")
    print(f"  処理成功: {len(results)} 銘柄")
    print(f"  失敗: {len(failed)} 銘柄 ({failed if failed else 'なし'})")
    print(f"\nsaved: data/d8_1m_pipeline_status.json")


if __name__ == "__main__":
    main()
