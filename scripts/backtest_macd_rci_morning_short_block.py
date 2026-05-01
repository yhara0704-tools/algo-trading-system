#!/usr/bin/env python3
"""F8 PoC: MacdRci × morning_first_30min_short_block の効果検証.

5/1 paper で 9:39-9:43 の連続 short stop -4,200 JPY (損失 70%) が発生した。
4/30 にも類似パターンあり再現性高い。寄付直後はボラ高で short の SL/TP が
不利に動きやすい仮説を、過去 60 日 5m データで検証する。

比較:
- 制御 (off): 既存パラメータ
- block_30min: 9:00-9:30 の short のみ禁止 (long 据え置き)
- block_15min: 9:00-9:15 の short のみ禁止 (より控えめ)
- block_60min: 9:00-10:00 の short のみ禁止 (より広範)

universe_active.json の MacdRci ペアに対し、各設定で portfolio simulation を
実行し、short trades 数 / WR / 平均 PnL / 総 PnL を比較する。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.backtesting.strategy_factory import create as make_strategy  # noqa: E402

JST = timezone(timedelta(hours=9))


def fetch_5m_history(symbol: str, days: int = 59) -> pd.DataFrame:
    # yfinance 5m データは過去 60 日上限。59 日マージンで安全に取得する。
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


def simulate_strategy(df: pd.DataFrame, signals: pd.DataFrame, tp_pct: float, sl_pct: float) -> dict:
    """既存 strategy_factory の signal を用いた簡易 in-bar simulation.

    signal:
      1 = long entry, -2 = short entry, -1 = long exit, 1 (in pos) = short exit
    """
    trades = []
    pos = None
    for ts, row in signals.iterrows():
        sig = int(row.get("signal", 0)) if not pd.isna(row.get("signal", 0)) else 0
        bar = df.loc[ts]
        if pos is None:
            if sig == 1:
                pos = {"side": "long", "entry_ts": ts, "entry": float(bar["close"]),
                       "tp": float(row.get("take_profit") or bar["close"] * (1 + tp_pct)),
                       "sl": float(row.get("stop_loss") or bar["close"] * (1 - sl_pct))}
            elif sig == -2:
                pos = {"side": "short", "entry_ts": ts, "entry": float(bar["close"]),
                       "tp": float(row.get("take_profit") or bar["close"] * (1 - tp_pct)),
                       "sl": float(row.get("stop_loss") or bar["close"] * (1 + sl_pct))}
        else:
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])
            exit_px = None
            exit_reason = None
            if pos["side"] == "long":
                if low <= pos["sl"]:
                    exit_px = pos["sl"]
                    exit_reason = "sl"
                elif high >= pos["tp"]:
                    exit_px = pos["tp"]
                    exit_reason = "tp"
                elif sig == -1:
                    exit_px = close
                    exit_reason = "signal"
            else:
                if high >= pos["sl"]:
                    exit_px = pos["sl"]
                    exit_reason = "sl"
                elif low <= pos["tp"]:
                    exit_px = pos["tp"]
                    exit_reason = "tp"
                elif sig == 1:
                    exit_px = close
                    exit_reason = "signal"
            if exit_px is not None:
                pnl_pct = (exit_px / pos["entry"] - 1.0) if pos["side"] == "long" \
                    else (pos["entry"] / exit_px - 1.0)
                trades.append({
                    "entry_ts": pos["entry_ts"].isoformat() if hasattr(pos["entry_ts"], "isoformat") else str(pos["entry_ts"]),
                    "exit_ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    "side": pos["side"],
                    "entry": pos["entry"],
                    "exit": exit_px,
                    "pnl_pct": pnl_pct,
                    "exit_reason": exit_reason,
                    "entry_hour": pos["entry_ts"].hour,
                    "entry_min": pos["entry_ts"].hour * 60 + pos["entry_ts"].minute,
                })
                pos = None
    return {"trades": trades}


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "long_n": 0, "short_n": 0, "long_pnl_pct": 0.0,
                "short_pnl_pct": 0.0, "total_pnl_pct": 0.0, "long_wr": 0.0,
                "short_wr": 0.0, "morning_short_n": 0, "morning_short_pnl_pct": 0.0}
    df = pd.DataFrame(trades)
    longs = df[df["side"] == "long"]
    shorts = df[df["side"] == "short"]
    morn = df[(df["side"] == "short") & (df["entry_min"] < 9 * 60 + 30)]
    return {
        "n": len(df),
        "long_n": len(longs),
        "short_n": len(shorts),
        "long_pnl_pct": float(longs["pnl_pct"].sum()) if len(longs) else 0.0,
        "short_pnl_pct": float(shorts["pnl_pct"].sum()) if len(shorts) else 0.0,
        "total_pnl_pct": float(df["pnl_pct"].sum()),
        "long_wr": float((longs["pnl_pct"] > 0).mean()) if len(longs) else 0.0,
        "short_wr": float((shorts["pnl_pct"] > 0).mean()) if len(shorts) else 0.0,
        "morning_short_n": len(morn),
        "morning_short_pnl_pct": float(morn["pnl_pct"].sum()) if len(morn) else 0.0,
    }


def run_one(symbol: str, df: pd.DataFrame, params: dict, label: str) -> dict:
    strategy = make_strategy("MacdRci", symbol=symbol, name=symbol, params=params)
    signals = strategy.generate_signals(df)
    sim = simulate_strategy(df, signals, params.get("tp_pct", 0.003), params.get("sl_pct", 0.002))
    summary = summarize(sim["trades"])
    summary["label"] = label
    summary["symbol"] = symbol
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=59, help="バックフィット期間 (yfinance 5m は 60 日上限)")
    ap.add_argument("--symbols", nargs="*", default=None,
                    help="対象 symbols。省略時は universe_active.json の MacdRci ペア全件")
    ap.add_argument("--out", default="data/macd_rci_morning_short_block_poc.json")
    args = ap.parse_args()

    if args.symbols:
        symbols = args.symbols
    else:
        univ = json.loads(Path("data/universe_active.json").read_text())
        # MacdRci ペアのみ、observation_only は除外
        symbols = [
            r["symbol"] for r in univ.get("symbols", [])
            if r.get("strategy") == "MacdRci"
            and not r.get("observation_only", False)
        ]
    print(f"対象 symbols: {len(symbols)}")
    print(f"  {symbols}")

    base_params = {
        "macd_fast": 3, "macd_slow": 7, "macd_signal": 9,
        "rci_periods": [10, 12, 15], "rci_min_agree": 2,
        "rci_entry_mode": 0, "tp_pct": 0.003, "sl_pct": 0.002,
        "interval": "5m",
    }

    configs = {
        "off": {**base_params, "morning_first_30min_short_block": 0},
        "block_15min": {**base_params, "morning_first_30min_short_block": 1, "morning_block_until_min": 15},
        "block_30min": {**base_params, "morning_first_30min_short_block": 1, "morning_block_until_min": 30},
        "block_60min": {**base_params, "morning_first_30min_short_block": 1, "morning_block_until_min": 60},
    }

    out_results = {"computed_at": datetime.now(JST).isoformat(),
                   "days": args.days,
                   "symbols": symbols,
                   "configs": list(configs.keys()),
                   "per_symbol": {},
                   "totals": {}}

    for sym in symbols:
        try:
            df = fetch_5m_history(sym, days=args.days)
            if df.empty or len(df) < 200:
                print(f"  skip {sym}: insufficient data (n={len(df)})")
                continue
            print(f"  {sym}: {len(df)} bars")
            out_results["per_symbol"][sym] = {}
            for label, params in configs.items():
                summary = run_one(sym, df, params, label)
                out_results["per_symbol"][sym][label] = summary
                print(f"    {label:<13} trades={summary['n']:>3} (L={summary['long_n']}/S={summary['short_n']}) "
                      f"L_pnl={summary['long_pnl_pct']*100:+6.2f}% S_pnl={summary['short_pnl_pct']*100:+6.2f}% "
                      f"total={summary['total_pnl_pct']*100:+6.2f}% morn_S={summary['morning_short_n']} (pnl={summary['morning_short_pnl_pct']*100:+5.2f}%)")
        except Exception as e:
            print(f"  ERROR {sym}: {e}")

    # 集計
    print("\n=== 全銘柄 集計 (各 config) ===")
    for label in configs.keys():
        total_long = 0
        total_short = 0
        total_long_pnl = 0.0
        total_short_pnl = 0.0
        morn_short_n = 0
        morn_short_pnl = 0.0
        n_total = 0
        for sym, by_label in out_results["per_symbol"].items():
            r = by_label.get(label, {})
            total_long += r.get("long_n", 0)
            total_short += r.get("short_n", 0)
            total_long_pnl += r.get("long_pnl_pct", 0.0)
            total_short_pnl += r.get("short_pnl_pct", 0.0)
            morn_short_n += r.get("morning_short_n", 0)
            morn_short_pnl += r.get("morning_short_pnl_pct", 0.0)
            n_total += r.get("n", 0)
        out_results["totals"][label] = {
            "n": n_total,
            "long_n": total_long,
            "short_n": total_short,
            "long_pnl_pct": total_long_pnl,
            "short_pnl_pct": total_short_pnl,
            "total_pnl_pct": total_long_pnl + total_short_pnl,
            "morning_short_n": morn_short_n,
            "morning_short_pnl_pct": morn_short_pnl,
        }
        t = out_results["totals"][label]
        print(f"  {label:<13} L={t['long_n']:>3} S={t['short_n']:>3} "
              f"L_pnl={t['long_pnl_pct']*100:+7.2f}% S_pnl={t['short_pnl_pct']*100:+7.2f}% "
              f"total={t['total_pnl_pct']*100:+7.2f}% morn_S={t['morning_short_n']:>3} (pnl={t['morning_short_pnl_pct']*100:+6.2f}%)")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    main()
