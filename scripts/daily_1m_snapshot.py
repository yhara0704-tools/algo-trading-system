#!/usr/bin/env python3
"""毎営業日の 1m データ蓄積パイプライン.

ユーザー指針 (2026-04-30 17:34):
> これから 1 分足データも蓄積。

yfinance は 1m データを過去 7 日分しか提供しないため、毎営業日 15:30 以降に
当日 (および取れる範囲の過去日) の 1m データを取得して `data/ohlcv_1m/<symbol>/<date>.parquet`
に永続化していく。30-90 日積めば、現在の「7 日サンプル制約」を超えて
まともな WF 検証 / 銘柄カテゴリ統計が可能になる。

cron 設定例 (M2 で VPS に登録):
  30 15 * * 1-5 cd /root/algo-trading-system && .venv/bin/python scripts/daily_1m_snapshot.py

スキーマ:
  data/ohlcv_1m/<symbol>/<YYYY-MM-DD>.parquet
    columns: [open, high, low, close, volume]
    index: DatetimeIndex (Asia/Tokyo)
    9:00-15:25 のザラ場のみ保存 (前場/後場、ランチ除外)

特徴:
  - **冪等**: 既存ファイルがある日はスキップ (--force でのみ上書き)
  - **失敗耐性**: 1 銘柄失敗しても他は継続
  - **メタデータ**: data/ohlcv_1m/_index.json で全体の蓄積状況を管理
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_full_universe_profile import load_all_symbols  # noqa: E402

JST = "Asia/Tokyo"
OHLCV_DIR = ROOT / "data/ohlcv_1m"
INDEX_FILE = OHLCV_DIR / "_index.json"


def fetch_1m(symbol: str, days: int = 7) -> pd.DataFrame | None:
    try:
        df = yf.download(symbol, period=f"{days}d", interval="1m",
                         auto_adjust=False, progress=False)
    except Exception as exc:
        print(f"  [{symbol}] fetch err: {exc}")
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                              "Close": "close", "Volume": "volume"})
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    if df.empty:
        return None
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(JST)
    # ザラ場のみ
    hh, mm = df.index.hour, df.index.minute
    in_morning = ((hh == 9) & (mm >= 0)) | (hh == 10) | ((hh == 11) & (mm <= 30))
    in_afternoon = ((hh == 12) & (mm >= 30)) | (hh == 13) | (hh == 14) | ((hh == 15) & (mm <= 30))
    return df[in_morning | in_afternoon]


def load_index() -> dict:
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text())
        except Exception:
            return {"symbols": {}}
    return {"symbols": {}}


def save_index(idx: dict) -> None:
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(idx, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def save_day_parquet(symbol: str, day: str, df_day: pd.DataFrame) -> Path:
    sym_dir = OHLCV_DIR / symbol.replace(".", "_")
    sym_dir.mkdir(parents=True, exist_ok=True)
    out_path = sym_dir / f"{day}.parquet"
    df_day.to_parquet(out_path, engine="pyarrow", compression="zstd")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="", help="カンマ区切り、空 = universe 全銘柄")
    ap.add_argument("--days", type=int, default=7, help="yfinance period (max 7d for 1m)")
    ap.add_argument("--force", action="store_true", help="既存ファイルも上書き")
    ap.add_argument("--sleep", type=float, default=0.3)
    args = ap.parse_args()

    if args.symbols:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        sym_info = load_all_symbols()
        syms = sorted(sym_info.keys())
    print(f"=== 1m snapshot === {len(syms)} symbols, period={args.days}d, "
          f"force={args.force}")

    idx = load_index()
    saved_total = 0
    skipped_total = 0
    failed_total = 0
    new_days_total: set[str] = set()

    for i, sym in enumerate(syms, 1):
        sym_key = sym.replace(".", "_")
        df = fetch_1m(sym, days=args.days)
        if df is None or df.empty:
            print(f"  [{i:>3}/{len(syms)}] {sym} no data")
            failed_total += 1
            time.sleep(args.sleep)
            continue
        days_in_data = sorted({str(d) for d in df.index.date})
        sym_idx = idx["symbols"].setdefault(sym_key, {"days": [], "last_updated": None})
        existing_days = set(sym_idx.get("days", []))
        saved_n = 0
        skipped_n = 0
        for day in days_in_data:
            day_path = OHLCV_DIR / sym_key / f"{day}.parquet"
            if not args.force and day_path.exists():
                skipped_n += 1
                continue
            df_day = df[df.index.date == datetime.strptime(day, "%Y-%m-%d").date()]
            if df_day.empty:
                continue
            try:
                save_day_parquet(sym, day, df_day)
                saved_n += 1
                if day not in existing_days:
                    sym_idx["days"].append(day)
                    new_days_total.add(day)
            except Exception as exc:
                print(f"  [{sym}] save err {day}: {exc}")
                failed_total += 1
        sym_idx["days"] = sorted(set(sym_idx["days"]))
        sym_idx["last_updated"] = datetime.now().isoformat()
        sym_idx["n_days"] = len(sym_idx["days"])
        sym_idx["earliest"] = sym_idx["days"][0] if sym_idx["days"] else None
        sym_idx["latest"] = sym_idx["days"][-1] if sym_idx["days"] else None
        saved_total += saved_n
        skipped_total += skipped_n
        print(f"  [{i:>3}/{len(syms)}] {sym:<7} saved={saved_n} skip={skipped_n} "
              f"total_days={sym_idx['n_days']} range={sym_idx['earliest']} ~ {sym_idx['latest']}")
        time.sleep(args.sleep)

    idx["last_run"] = datetime.now().isoformat()
    idx["n_symbols"] = len(idx["symbols"])
    idx["n_days_total_unique"] = len({d for s in idx["symbols"].values() for d in s.get("days", [])})
    save_index(idx)

    print(f"\n=== サマリ ===")
    print(f"  saved files: {saved_total}")
    print(f"  skipped (already exists): {skipped_total}")
    print(f"  failed: {failed_total}")
    print(f"  new days added: {len(new_days_total)} {sorted(new_days_total)}")
    print(f"  index: {INDEX_FILE.relative_to(ROOT)}")
    print(f"  total symbols tracked: {idx['n_symbols']}")
    print(f"  unique days across all: {idx['n_days_total_unique']}")


if __name__ == "__main__":
    main()
