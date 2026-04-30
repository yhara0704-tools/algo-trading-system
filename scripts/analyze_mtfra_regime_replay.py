#!/usr/bin/env python3
"""Multi-Timeframe Regime Alignment (MTFRA) リプレイ検証 (D9 Phase 1).

ユーザー提案 (2026-04-30 18:10):
> レジームを時間足ごとにリアルタイムで常に取得するようにしたら、
> 手法のエントリータイミングやエグジットタイミングが今より高精度になる。
> 例: 1H + 15m + 5m のレジームが一致している時、1m が任意のレジームになった
> 際が適切なエントリータイミングである。

現状の jp_live_runner は単一時間足のみで regime を判定している。本スクリプトは
ohlcv_1m データ (D6 で蓄積) を 1m/5m/15m/60m にリサンプリングし、

  1. 整合 (5m + 15m + 60m が同じ trending) の発生頻度
  2. 整合時刻からの forward return (5min, 15min, 30min) を非整合時と比較

を測定して、MTFRA フィルタの仮説を定量検証する。

入力: data/ohlcv_1m/<symbol>/*.parquet
出力: data/mtfra_replay_latest.json
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.market_regime import _detect, _calc_adx  # noqa: E402

OHLCV_DIR = ROOT / "data/ohlcv_1m"


def load_symbol_data(symbol: str) -> pd.DataFrame:
    """全日分の 1m データを連結して時系列順に返す."""
    sdir = OHLCV_DIR / symbol
    if not sdir.exists():
        return pd.DataFrame()
    parts = []
    for p in sorted(sdir.glob("*.parquet")):
        try:
            d = pd.read_parquet(p)
            if not d.empty:
                parts.append(d)
        except Exception:
            continue
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """1m → 任意時間足にリサンプル."""
    if df.empty:
        return df
    agg = {
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }
    out = df.resample(rule, label="right", closed="right").agg(agg).dropna()
    return out


def detect_regime_simple(df: pd.DataFrame) -> str:
    """少ないバー数でも動く簡易レジーム判定 (前場対応).

    `market_regime._detect` は 50 本必要。MTFRA では 60m の前日までの
    バーが少ないので、20-50 本での簡易判定にフォールバックする。
    """
    if len(df) < 14:
        return "unknown"
    if len(df) >= 50:
        try:
            return _detect("MTFRA", df).regime
        except Exception:
            pass
    close = df["close"]
    high = df["high"]
    low = df["low"]
    ema20 = close.ewm(span=20, adjust=False).mean()
    if len(ema20) < 5:
        return "unknown"
    slope = (ema20.iloc[-1] - ema20.iloc[-5]) / max(1e-9, ema20.iloc[-5]) * 100
    adx = _calc_adx(high, low, close, period=min(14, len(df) - 1))
    if adx > 25:
        return "trending_up" if slope > 0 else "trending_down"
    if adx < 18:
        return "ranging"
    return "trending_up" if slope > 0.05 else (
        "trending_down" if slope < -0.05 else "ranging"
    )


def alignment_status(r5: str, r15: str, r60: str) -> tuple[str, str]:
    """3 軸整合の判定.

    Returns:
        (alignment_label, direction): "aligned_up" | "aligned_down" | "mixed" | "unknown"
    """
    if any(r in {"unknown"} for r in [r5, r15, r60]):
        return "unknown", "none"
    ups = sum(1 for r in [r5, r15, r60] if r == "trending_up")
    downs = sum(1 for r in [r5, r15, r60] if r == "trending_down")
    if ups == 3:
        return "aligned_up", "long"
    if downs == 3:
        return "aligned_down", "short"
    if ups == 2 and r5 == "trending_up":
        return "partial_up", "long"
    if downs == 2 and r5 == "trending_down":
        return "partial_down", "short"
    return "mixed", "none"


def replay_symbol(symbol: str, fwd_minutes: list[int]) -> dict:
    """1 銘柄を MTFRA でリプレイし、forward return を集計."""
    df_1m = load_symbol_data(symbol)
    if df_1m.empty or len(df_1m) < 200:
        return {"symbol": symbol, "n_bars_1m": len(df_1m), "skipped": "insufficient"}

    df_5m = resample(df_1m, "5min")
    df_15m = resample(df_1m, "15min")
    df_60m = resample(df_1m, "60min")

    # 各時間足のレジームを「ローリング判定」する。
    # メモリ節約のため、評価時刻ごとに過去 200 本だけ取って判定。
    eval_times = df_1m.index[df_1m.index.minute % 5 == 0]  # 5 分刻みで判定

    results = []
    aligned_counts: dict[str, int] = defaultdict(int)
    forward_returns: dict[str, list[float]] = defaultdict(list)

    for ts in eval_times:
        # 各時間足について ts 以前のバーを切り出す
        d5 = df_5m[df_5m.index <= ts].tail(60)
        d15 = df_15m[df_15m.index <= ts].tail(40)
        d60 = df_60m[df_60m.index <= ts].tail(30)
        if len(d5) < 14 or len(d15) < 14 or len(d60) < 5:
            continue
        r5 = detect_regime_simple(d5)
        r15 = detect_regime_simple(d15)
        r60 = detect_regime_simple(d60)
        align, direction = alignment_status(r5, r15, r60)
        aligned_counts[align] += 1

        # forward return 計算 (1m close ベース)
        cur_close = float(df_1m.loc[ts, "close"]) if ts in df_1m.index else None
        if cur_close is None:
            continue
        for fm in fwd_minutes:
            ts_fwd_target = ts + pd.Timedelta(minutes=fm)
            fwd = df_1m[df_1m.index >= ts_fwd_target].head(1)
            if fwd.empty:
                continue
            fwd_close = float(fwd["close"].iloc[0])
            ret_pct = (fwd_close - cur_close) / cur_close * 100
            # 方向ありの整合は方向調整 (long なら +ret、short なら -ret)
            if direction == "long":
                signed = ret_pct
            elif direction == "short":
                signed = -ret_pct
            else:
                signed = ret_pct  # 方向なし時は素のリターン
            forward_returns[f"{align}_{fm}m"].append(signed)
        results.append({
            "ts": ts, "r5": r5, "r15": r15, "r60": r60,
            "align": align, "direction": direction,
        })

    # 統計
    stats = {}
    for key, vals in forward_returns.items():
        if not vals:
            continue
        arr = np.array(vals)
        align, fm = key.rsplit("_", 1)
        stats.setdefault(align, {})[fm] = {
            "n": int(len(arr)),
            "mean_ret_pct": round(float(arr.mean()), 4),
            "median_ret_pct": round(float(np.median(arr)), 4),
            "win_rate_pct": round(float((arr > 0).mean() * 100), 1),
            "p25": round(float(np.percentile(arr, 25)), 4),
            "p75": round(float(np.percentile(arr, 75)), 4),
        }

    return {
        "symbol": symbol,
        "n_bars_1m": len(df_1m),
        "n_eval_times": int(len(results)),
        "aligned_counts": dict(aligned_counts),
        "stats_per_alignment": stats,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", help="検証銘柄 (省略時は全 ohlcv_1m)")
    ap.add_argument("--fwd", nargs="*", type=int, default=[5, 15, 30],
                    help="forward 分数 (default: 5 15 30)")
    ap.add_argument("--out", default="data/mtfra_replay_latest.json")
    args = ap.parse_args()

    if args.symbols:
        symbols = args.symbols
    else:
        symbols = sorted([p.name for p in OHLCV_DIR.iterdir() if p.is_dir()])

    print(f"=== MTFRA Replay 検証 ({len(symbols)} 銘柄, fwd={args.fwd}min) ===\n")

    all_results = []
    for sym in symbols:
        print(f"  処理中: {sym} ...", end=" ", flush=True)
        try:
            res = replay_symbol(sym, args.fwd)
        except Exception as e:
            print(f"ERROR {e}")
            continue
        if res.get("skipped"):
            print(f"skip ({res.get('skipped')})")
            continue
        n_eval = res.get("n_eval_times", 0)
        print(f"eval={n_eval}, "
              f"aligned_up={res['aligned_counts'].get('aligned_up', 0)}, "
              f"aligned_down={res['aligned_counts'].get('aligned_down', 0)}")
        all_results.append(res)

    if not all_results:
        print("\n!! 検証可能なデータなし")
        return

    # ── 全銘柄統合の集計 ──────────────────────────────────────
    print(f"\n=== 統合: 整合状態別 forward return ===\n")
    agg_returns: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    agg_counts: dict[str, int] = defaultdict(int)

    for res in all_results:
        for k, v in res["aligned_counts"].items():
            agg_counts[k] += v
        for align, by_fm in res["stats_per_alignment"].items():
            for fm, st in by_fm.items():
                # mean × n で sum 化（簡易合算）
                agg_returns[align][fm].append((st["mean_ret_pct"], st["n"], st["win_rate_pct"]))

    print(f"{'整合状態':<14} {'発生回数':>8} | "
          f"{'5m_ret':>8} {'5m_WR':>6} | "
          f"{'15m_ret':>8} {'15m_WR':>6} | "
          f"{'30m_ret':>8} {'30m_WR':>6}")
    print("-" * 100)
    summary = {}
    for align in ["aligned_up", "aligned_down", "partial_up", "partial_down",
                  "mixed", "unknown"]:
        cnt = agg_counts.get(align, 0)
        if cnt == 0:
            continue
        row = [f"{align:<14}", f"{cnt:>8}"]
        align_summary = {"count": cnt}
        for fm in ["5m", "15m", "30m"]:
            entries = agg_returns.get(align, {}).get(fm, [])
            if not entries:
                row.extend([f"{'-':>8}", f"{'-':>6}"])
                continue
            # 重み付き平均 (n を重みに)
            total_n = sum(n for _, n, _ in entries)
            wmean = sum(m * n for m, n, _ in entries) / max(total_n, 1)
            wwr = sum(wr * n for _, n, wr in entries) / max(total_n, 1)
            row.extend([f"{wmean:>+7.3f}%", f"{wwr:>5.1f}%"])
            align_summary[f"{fm}m"] = {
                "weighted_mean_ret_pct": round(wmean, 4),
                "weighted_wr_pct": round(wwr, 1),
                "total_n": total_n,
            }
        summary[align] = align_summary
        print(" | ".join(row))

    # ── 結論的サマリ ──────────────────────────────────────
    print("\n=== 結論 (ベンチマーク = mixed) ===\n")
    base = summary.get("mixed", {})
    for fm_key in ["5m", "15m", "30m"]:
        if fm_key not in base:
            continue
        base_ret = base[fm_key]["weighted_mean_ret_pct"]
        base_wr = base[fm_key]["weighted_wr_pct"]
        print(f"  -- {fm_key} forward (mixed基準: ret={base_ret:+.3f}%, WR={base_wr:.1f}%) --")
        for label in ["aligned_up", "aligned_down", "partial_up", "partial_down"]:
            if label not in summary or fm_key not in summary[label]:
                continue
            d_ret = summary[label][fm_key]["weighted_mean_ret_pct"] - base_ret
            d_wr = summary[label][fm_key]["weighted_wr_pct"] - base_wr
            n = summary[label][fm_key]["total_n"]
            print(f"    {label:<14} Δret={d_ret:+.3f}%pt  ΔWR={d_wr:+.1f}%pt  (n={n})")

    out_path = ROOT / args.out
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "n_symbols": len(all_results),
        "fwd_minutes": args.fwd,
        "summary": summary,
        "per_symbol": all_results,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=> 保存: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
