#!/usr/bin/env python3
"""D9 Phase 4 PoC: 緩和整合 (2 軸のみ) で頻度と効果検証.

D9 Phase 3 で「3 軸全整合 (3m+30m+60m)」がほぼ存在せず、transition も exit も
ほぼ機能しないと判明。本 PoC では:
  - 2 軸 (30m+60m): 上位 timeframe のみ
  - 2 軸 (15m+60m): 中期+上位
  - 2 軸 (3m+60m): 短期+上位
  - 2 軸 (3m+30m): 中短期
  - 3 軸 (3m+30m+60m): 既存 default (比較基準)

各 combo について:
  - 整合頻度 (aligned_up + aligned_down が全バー中の何 %)
  - 整合中の N バー後 forward return (long/short bias の有無)
  - 全 universe MacdRci 銘柄を 60 日 5m で評価

整合頻度が「使い物になるレベル (5-10% 以上)」を達成し、
forward return が信号として機能する combo があれば、Phase 5 で実戦投入を検討。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.multi_timeframe_regime import (  # noqa: E402
    MTFRADetector,
    _classify_alignment,
)

JST = timezone(timedelta(hours=9))


def fetch_5m_history(symbol: str, days: int = 59) -> pd.DataFrame:
    end = datetime.now(JST) + timedelta(days=1)
    start = end - timedelta(days=min(days, 59))
    df = yf.download(
        symbol,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        interval="5m",
        progress=False,
        auto_adjust=False,
    )
    if df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    return df


def evaluate_combo(symbol: str, df: pd.DataFrame, combo: tuple[str, ...],
                   step_bars: int = 12, fwd_n_bars: int = 12) -> dict:
    """指定 combo について、step_bars 毎に integrity を評価し forward return を計測.

    df は 5m バー前提。step_bars=12 = 60 分毎判定。
    forward return: そのバーの close → fwd_n_bars 後の close 変化率。
    """
    detector = MTFRADetector(mode="custom", custom_combo=combo)
    n_total = 0
    n_aligned_up = 0
    n_aligned_down = 0
    n_mixed = 0
    n_unknown = 0
    aligned_up_fwd: list[float] = []
    aligned_down_fwd: list[float] = []

    if len(df) < 200:
        return {"n_total": 0, "n_aligned_up": 0, "n_aligned_down": 0,
                "n_mixed": 0, "n_unknown": 0,
                "rate_aligned_up": 0.0, "rate_aligned_down": 0.0,
                "rate_aligned_total": 0.0,
                "fwd_up_mean_pct": 0.0, "fwd_up_n": 0, "fwd_up_wr": 0.0,
                "fwd_down_mean_pct": 0.0, "fwd_down_n": 0, "fwd_down_wr": 0.0}

    # 5m → 1m 相当の DataFrame として detector に渡す (実際は 5m データを 1m と見立てる)
    # MTFRADetector._resample は rule に従って resample するので、5m → 30m は 30/5=6 倍まとめ。
    # _resample は raw df を resample するのでそのまま渡せば良いが、
    # detector は 1m 前提の MIN_BARS を持つ。 5m 化された combo (3m, 1m) は使えない。
    # よって combo は (5m 以上) のみ扱う。

    closes = df["close"].values
    indexes = df.index

    # step_bars 毎にスキャン (重複削減)
    for i in range(180, len(df) - fwd_n_bars - 1, step_bars):
        window = df.iloc[max(0, i - 180):i + 1]
        decision = detector.evaluate(symbol, window)
        align = _classify_alignment(decision.directions)
        n_total += 1
        if align == "aligned_up":
            n_aligned_up += 1
            fwd = (closes[i + fwd_n_bars] - closes[i]) / closes[i]
            aligned_up_fwd.append(float(fwd))
        elif align == "aligned_down":
            n_aligned_down += 1
            fwd = (closes[i + fwd_n_bars] - closes[i]) / closes[i]
            aligned_down_fwd.append(float(fwd))
        elif align == "mixed":
            n_mixed += 1
        else:
            n_unknown += 1

    fwd_up = np.array(aligned_up_fwd) if aligned_up_fwd else np.array([])
    fwd_down = np.array(aligned_down_fwd) if aligned_down_fwd else np.array([])

    return {
        "n_total": n_total,
        "n_aligned_up": n_aligned_up,
        "n_aligned_down": n_aligned_down,
        "n_mixed": n_mixed,
        "n_unknown": n_unknown,
        "rate_aligned_up": n_aligned_up / max(1, n_total),
        "rate_aligned_down": n_aligned_down / max(1, n_total),
        "rate_aligned_total": (n_aligned_up + n_aligned_down) / max(1, n_total),
        "fwd_up_mean_pct": float(fwd_up.mean()) if len(fwd_up) else 0.0,
        "fwd_up_n": int(len(fwd_up)),
        "fwd_up_wr": float((fwd_up > 0).mean()) if len(fwd_up) else 0.0,
        "fwd_down_mean_pct": float(fwd_down.mean()) if len(fwd_down) else 0.0,
        "fwd_down_n": int(len(fwd_down)),
        "fwd_down_wr": float((fwd_down < 0).mean()) if len(fwd_down) else 0.0,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=59)
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--out", default="data/mtfra_phase4_relaxed_alignment.json")
    args = ap.parse_args()

    if args.symbols:
        symbols = args.symbols
    else:
        univ = json.loads(Path("data/universe_active.json").read_text())
        symbols = sorted({
            r["symbol"] for r in univ.get("symbols", [])
            if r.get("strategy") == "MacdRci"
            and not r.get("observation_only", False)
        })

    # combos: 5m データから resample して使えるものに限る。1m, 3m, 5m はもとが 5m 起点の
    # ため、3m / 1m を combo に含めるのは難しい (data が粗い)。15m 以上を中心に評価。
    combos = {
        "default_3axis(3m+30m+60m)": ("3m", "30m", "60m"),  # 比較基準 (5m を 3m に resample は不可、unknown 多発の可能性)
        "2axis(15m+60m)": ("15m", "60m"),
        "2axis(30m+60m)": ("30m", "60m"),
        "2axis(15m+30m)": ("15m", "30m"),
        "2axis(5m+15m)": ("5m", "15m"),
        "single(60m)": ("60m",),
    }

    out: dict = {
        "computed_at": datetime.now(JST).isoformat(),
        "days": args.days,
        "symbols": symbols,
        "combos": list(combos.keys()),
        "per_symbol": {},
        "totals": {},
    }

    print(f"対象 symbols: {len(symbols)} → {symbols}")

    for sym in symbols:
        try:
            df = fetch_5m_history(sym, days=args.days)
            if df.empty or len(df) < 300:
                print(f"  skip {sym}: insufficient data (n={len(df)})")
                continue
            print(f"\n{sym}: {len(df)} bars")
            out["per_symbol"][sym] = {}
            for label, combo in combos.items():
                r = evaluate_combo(sym, df, combo)
                out["per_symbol"][sym][label] = r
                print(f"  {label:<28} n={r['n_total']:>3} "
                      f"up_rate={r['rate_aligned_up']*100:>5.1f}% (n={r['fwd_up_n']:>3}, wr={r['fwd_up_wr']*100:>4.1f}%, ret={r['fwd_up_mean_pct']*100:+5.2f}%)  "
                      f"down_rate={r['rate_aligned_down']*100:>5.1f}% (n={r['fwd_down_n']:>3}, wr={r['fwd_down_wr']*100:>4.1f}%, ret={r['fwd_down_mean_pct']*100:+5.2f}%)")
        except Exception as e:
            print(f"  ERROR {sym}: {e}")

    print("\n=== 全銘柄 集計 ===")
    for label in combos.keys():
        n_total = 0
        n_up = 0
        n_down = 0
        all_up_fwd: list[float] = []
        all_down_fwd: list[float] = []
        for sym, by_label in out["per_symbol"].items():
            r = by_label.get(label, {})
            n_total += r.get("n_total", 0)
            n_up += r.get("n_aligned_up", 0)
            n_down += r.get("n_aligned_down", 0)
            if r.get("fwd_up_n", 0) > 0:
                all_up_fwd.extend([r["fwd_up_mean_pct"]] * r["fwd_up_n"])
            if r.get("fwd_down_n", 0) > 0:
                all_down_fwd.extend([r["fwd_down_mean_pct"]] * r["fwd_down_n"])
        rate_up = n_up / max(1, n_total)
        rate_down = n_down / max(1, n_total)
        out["totals"][label] = {
            "n_total": n_total, "n_up": n_up, "n_down": n_down,
            "rate_up": rate_up, "rate_down": rate_down,
            "rate_aligned_total": rate_up + rate_down,
            "fwd_up_n_total": len(all_up_fwd),
            "fwd_down_n_total": len(all_down_fwd),
        }
        print(f"  {label:<28} n_total={n_total:>5} up_rate={rate_up*100:>5.1f}% down_rate={rate_down*100:>5.1f}% "
              f"aligned_total_rate={(rate_up + rate_down)*100:>5.1f}% "
              f"fwd_up_n={len(all_up_fwd)} fwd_down_n={len(all_down_fwd)}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    main()
