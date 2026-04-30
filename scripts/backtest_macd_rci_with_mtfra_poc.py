#!/usr/bin/env python3
"""MacdRci × MTFRA 統合 PoC (D9 Phase 2.5).

D9 Phase 2 で MicroScalp に MTFRA を当てたら逆効果と判明した。原因は
「MicroScalp = VWAP 戻り型 = 逆張り」 で MTFRA (トレンド継続シグナル)
と本質的に相性が悪いこと。

仮説: 順張り戦略 (MacdRci) なら MTFRA で改善するはず。
  - MacdRci のロングエントリー (MACD>0, RCI 上向き) は「足元で上昇トレンド」を捉える
  - これに「上位足 (15m/30m/60m) も上昇トレンド」 を要求すれば、フェイク GC を排除
  - = WR / PF が向上する見込み

方法:
  1. MacdRci で生成された signal をそのまま使ってベースライン PnL 算出
  2. 同 signal のうち、各バーで {15m, 30m, 60m} 整合 (direction=up) でないものを signal=0 化
  3. PnL/WR/PF を比較

5m 足ベースなので combo は:
  - default:    15m + 30m + 60m
  - aggressive: 5m + 15m + 30m + 60m  (5m 自体も評価)

対象: 9984.T MacdRci, 6613.T MacdRci, 3103.T MacdRci など Robust 銘柄
データ: data/ohlcv_5m or yfinance 30 日 5m
出力: data/macd_rci_mtfra_poc_latest.json
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.backtesting.engine import run_backtest  # noqa: E402
from backend.backtesting.strategy_factory import create as create_strategy  # noqa: E402
from backend.multi_timeframe_regime import _resample, _detect_dir, TF_RULE  # noqa: E402

JST = "Asia/Tokyo"


def fetch_5m(symbol: str, period_days: int = 30) -> pd.DataFrame | None:
    """yfinance から 5m データを取得 (最大 60 日)."""
    try:
        import yfinance as yf
        df = yf.download(symbol, period=f"{period_days}d", interval="5m",
                         auto_adjust=False, progress=False)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(JST)
    hh, mm = df.index.hour, df.index.minute
    in_morning = ((hh == 9) & (mm >= 0)) | (hh == 10) | ((hh == 11) & (mm <= 30))
    in_aft = ((hh == 12) & (mm >= 30)) | (hh == 13) | (hh == 14) | ((hh == 15) & (mm <= 30))
    return df[in_morning | in_aft]


def compute_mtfra_directions(df_5m: pd.DataFrame, combo: tuple[str, ...]) -> pd.DataFrame:
    """5m 足の各時刻について {15m, 30m, 60m} の direction を返す.

    各バーの timestamp 以下のデータを使って resample → _detect_dir。
    パフォーマンス重視で「30m / 60m は新バー出現時のみ更新、間は前回値継承」。
    """
    n = len(df_5m)
    result = {tf: ["unknown"] * n for tf in combo}

    # 各時間足を一度だけ完全 resample
    tf_dfs = {}
    for tf in combo:
        if tf == "5m":
            tf_dfs[tf] = df_5m
        else:
            tf_dfs[tf] = _resample(
                df_5m[["open", "high", "low", "close", "volume"]],
                TF_RULE.get(tf, tf.replace("m", "min")),
            )

    # 各 5m バーで, 各 tf の「ts 以下の最後の 60 本」 を取って _detect_dir
    for i in range(n):
        ts = df_5m.index[i]
        # 5m が 60 本以上溜まってから判定 (= 5 時間 = 1 営業日相当)
        if i < 60:
            continue
        for tf in combo:
            df_tf = tf_dfs[tf]
            sub = df_tf[df_tf.index <= ts].tail(60)
            min_n = 14 if tf in ("5m", "15m") else (10 if tf == "30m" else 5)
            if len(sub) < min_n:
                continue
            result[tf][i] = _detect_dir(sub)

    return pd.DataFrame(result, index=df_5m.index)


def filter_signals_by_mtfra(signals: pd.Series, dirs: pd.DataFrame) -> tuple[pd.Series, dict]:
    """signal=1/-1 のうち、対応 direction が全 up/down でないものを 0 化.

    - signal=1 (long) かつ全列 == "up" → 残す
    - signal=-1 (short) かつ全列 == "down" → 残す
    - それ以外でゼロ以外の signal → 0 化
    """
    new_sig = signals.copy()
    n_long_orig = int((signals == 1).sum())
    n_short_orig = int((signals == -1).sum())
    n_long_kept = 0
    n_short_kept = 0
    for i, sig in enumerate(signals.values):
        if sig == 0:
            continue
        row = dirs.iloc[i].values
        if any(d == "unknown" for d in row):
            new_sig.iat[i] = 0
            continue
        all_up = all(d == "up" for d in row)
        all_down = all(d == "down" for d in row)
        if sig == 1 and all_up:
            n_long_kept += 1
        elif sig == -1 and all_down:
            n_short_kept += 1
        else:
            new_sig.iat[i] = 0
    return new_sig, {
        "n_long_orig": n_long_orig,
        "n_long_kept": n_long_kept,
        "n_long_filtered": n_long_orig - n_long_kept,
        "n_short_orig": n_short_orig,
        "n_short_kept": n_short_kept,
        "n_short_filtered": n_short_orig - n_short_kept,
    }


def evaluate_signals(symbol: str, df: pd.DataFrame,
                     base_strat, label: str) -> dict:
    """与えられた DataFrame (signal 上書き済) で run_backtest."""
    # MacdRci の generate_signals は signal/stop_loss/take_profit を返す
    # ここでは事前に生成済の df をそのまま投げる用に simple wrapper
    class _Wrapper:
        def __init__(self, base, df_with_signals):
            self.meta = base.meta
            self._df = df_with_signals

        def generate_signals(self, _df_input: pd.DataFrame) -> pd.DataFrame:
            return self._df

    wrapper = _Wrapper(base_strat, df)
    result = run_backtest(
        wrapper, df,
        starting_cash=990_000.0, fee_pct=0.0,
        position_pct=0.30, usd_jpy=1.0, lot_size=100,
        limit_slip_pct=0.001, eod_close_time=(15, 25),
        subsession_cooldown_min=2, daily_loss_limit_pct=-3.0,
    )
    n = len(result.trades)
    if n == 0:
        return {"label": label, "symbol": symbol, "trades": 0,
                "wr": None, "pf": None, "pnl_jpy": 0.0,
                "long_n": 0, "short_n": 0,
                "long_pnl": 0.0, "short_pnl": 0.0,
                "trades_per_day": 0, "days": None}
    wins = [t for t in result.trades if t.pnl > 0]
    losses = [t for t in result.trades if t.pnl <= 0]
    pf = (sum(t.pnl for t in wins) / -sum(t.pnl for t in losses)) if losses else 999.0
    long_t = [t for t in result.trades if t.side == "long"]
    short_t = [t for t in result.trades if t.side == "short"]
    days = max(1, (df.index[-1].date() - df.index[0].date()).days + 1)
    return {
        "label": label, "symbol": symbol, "trades": n,
        "wr": round(len(wins) / n * 100, 1),
        "pf": round(pf, 2),
        "long_n": len(long_t), "short_n": len(short_t),
        "long_pnl": round(float(sum(t.pnl for t in long_t)), 0),
        "short_pnl": round(float(sum(t.pnl for t in short_t)), 0),
        "long_wr": round(sum(1 for t in long_t if t.pnl > 0) / max(1, len(long_t)) * 100, 1) if long_t else None,
        "short_wr": round(sum(1 for t in short_t if t.pnl > 0) / max(1, len(short_t)) * 100, 1) if short_t else None,
        "pnl_jpy": round(float(sum(t.pnl for t in result.trades)), 0),
        "days": days, "trades_per_day": round(n / days, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*",
                    default=["9984.T", "6613.T", "3103.T", "8136.T", "9107.T"],
                    help="検証銘柄 (default: Robust 上位 5 銘柄)")
    ap.add_argument("--days", type=int, default=30,
                    help="yfinance fetch 期間 (5m は最大 60 日)")
    ap.add_argument("--out", default="data/macd_rci_mtfra_poc_latest.json")
    args = ap.parse_args()

    symbols = [s if s.endswith(".T") else f"{s}.T" for s in args.symbols]

    print(f"=== MacdRci × MTFRA PoC ({len(symbols)} 銘柄, {args.days}日) ===\n")

    combos = [
        ("off", None),
        ("mtfra_30m_60m", ("30m", "60m")),
        ("mtfra_15m_30m_60m", ("15m", "30m", "60m")),
        ("mtfra_5m_15m_30m_60m", ("5m", "15m", "30m", "60m")),
    ]

    all_results: list[dict] = []
    for sym in symbols:
        df = fetch_5m(sym, period_days=args.days)
        if df is None or df.empty or len(df) < 200:
            print(f"  {sym}: skip (no data or too few bars: {0 if df is None else len(df)})")
            continue
        print(f"\n  {sym} (n={len(df)} 5m bars, "
              f"{(df.index[-1].date() - df.index[0].date()).days + 1}日)")

        # ベース MacdRci で signal 生成
        try:
            base_strat = create_strategy("MacdRci", sym, name=sym.replace(".T", ""))
            df_signals = base_strat.generate_signals(df)
        except Exception as e:
            print(f"    MacdRci 生成失敗: {e}")
            continue

        n_long = int((df_signals["signal"] == 1).sum())
        n_short = int((df_signals["signal"] == -1).sum())
        if n_long + n_short == 0:
            print(f"    signal が 0 件、スキップ")
            continue
        print(f"    base signals: long={n_long}, short={n_short}")

        for label, combo in combos:
            if combo is None:
                df_used = df_signals.copy()
                filter_stat = {"n_long_orig": n_long, "n_long_kept": n_long,
                               "n_short_orig": n_short, "n_short_kept": n_short}
            else:
                # MTFRA direction を計算
                try:
                    dirs = compute_mtfra_directions(df, combo)
                except Exception as e:
                    print(f"    {label}: MTFRA 計算失敗 {e}")
                    continue
                # filter
                new_sig, filter_stat = filter_signals_by_mtfra(
                    df_signals["signal"], dirs)
                df_used = df_signals.copy()
                df_used["signal"] = new_sig
            try:
                res = evaluate_signals(sym, df_used, base_strat, label)
            except Exception as e:
                print(f"    {label}: backtest 失敗 {e}")
                continue
            res["filter"] = filter_stat
            all_results.append(res)
            wr_str = f"{res['wr']}%" if res['wr'] is not None else "—"
            pf_str = f"{res['pf']}" if res['pf'] is not None else "—"
            kept_long = filter_stat.get("n_long_kept", 0)
            kept_short = filter_stat.get("n_short_kept", 0)
            print(f"    {label:<22} kept(L/S)={kept_long}/{kept_short}  n={res['trades']:>3} "
                  f"WR={wr_str:<6} PF={pf_str:<5} PnL={res['pnl_jpy']:>+8.0f}円")

    # ── サマリ集計 ────────────────────────────────────────
    print(f"\n=== 構成別集計 (全銘柄合計) ===\n")
    print(f"{'構成':<24} {'銘柄':>5} {'trades':>7} {'WR':>6} {'PF':>5} "
          f"{'PnL/日':>10} {'PnL合計':>10}")
    print("-" * 80)
    summary = {}
    for label, _ in combos:
        rs = [r for r in all_results if r["label"] == label]
        if not rs:
            continue
        total_trades = sum(r["trades"] for r in rs)
        total_pnl = sum(r["pnl_jpy"] for r in rs)
        rs_with = [r for r in rs if r["trades"] > 0]
        if rs_with:
            avg_wr = sum(r["wr"] * r["trades"] for r in rs_with) / max(1, total_trades)
            avg_pf = sum((r["pf"] or 0) * r["trades"] for r in rs_with) / max(1, total_trades)
            total_days = sum(r["days"] or 0 for r in rs_with)
            avg_pnl_per_day = total_pnl / max(1, total_days)
        else:
            avg_wr = avg_pf = avg_pnl_per_day = 0
        summary[label] = {
            "n_symbols": len([r for r in rs if r["trades"] > 0]),
            "total_trades": total_trades,
            "weighted_wr": round(avg_wr, 1),
            "weighted_pf": round(avg_pf, 2),
            "total_pnl": round(total_pnl, 0),
            "avg_pnl_per_day": round(avg_pnl_per_day, 0),
        }
        print(f"{label:<24} {summary[label]['n_symbols']:>5} "
              f"{total_trades:>7} {avg_wr:>5.1f}% {avg_pf:>4.2f} "
              f"{avg_pnl_per_day:>+9.0f}円 {total_pnl:>+9.0f}円")

    # 改善度
    base = summary.get("off")
    if base:
        print(f"\n=== ベースライン (off) からの改善度 ===")
        for label, _ in combos[1:]:
            s = summary.get(label)
            if not s:
                continue
            d_wr = s["weighted_wr"] - base["weighted_wr"]
            d_pnl = s["total_pnl"] - base["total_pnl"]
            d_pnl_pct = (s["total_pnl"] - base["total_pnl"]) / max(1, abs(base["total_pnl"])) * 100 if base["total_pnl"] else 0
            d_trades = s["total_trades"] - base["total_trades"]
            print(f"  {label:<24} ΔWR={d_wr:+.1f}%pt  ΔPnL={d_pnl:+.0f}円 ({d_pnl_pct:+.0f}%)  "
                  f"Δtrades={d_trades:+d}")

    out_path = ROOT / args.out
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "n_symbols": len(symbols),
        "n_days": args.days,
        "configs": [c[0] for c in combos],
        "summary": summary,
        "results": all_results,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=> 保存: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
