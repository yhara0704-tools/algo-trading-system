#!/usr/bin/env python3
"""JPParabolicSwing の疑似約定 PnL（手数料・スリッページ込み）+ 期間分割ロバスト確認。

前提（データ）:
  - 15m は yfinance 経由だと実質約59日が上限。長期ウィンドウは ``algo_shared/ohlcv_cache``
    の parquet が居る環境でのみ意味がある（``fetch_ohlcv(..., days=大)`` で過去に遡る）。

コスト:
  - 既定で ``data/backtest_cost_model.json`` の ``fee_pct`` / ``limit_slip_pct`` を使用。
  - 買い: 約定足の始値 × (1 + slip)、売り: × (1 − slip)。各腿に fee_pct を按分。

約定ルール（シンプル実行モデル）:
  - signal[i]==1 → 翌足始値で買い（long）
  - signal[i]==-1 → min_hold 経過後のみ翌足始値で売り
  - 損切り: 保有中の足で low が SL 以下なら売り（ギャップ時は始値と SL 準の不利側）

使い方::

  .venv/bin/python scripts/sim_parabolic_swing_pnl.py --days-15m 59
  .venv/bin/python scripts/sim_parabolic_swing_pnl.py --chunk-regimes 3 --days-15m 800
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from backend.lab.runner import fetch_ohlcv  # noqa: E402
from backend.strategies.jp_stock.jp_parabolic_swing import JPParabolicSwing  # noqa: E402

JST = timezone(timedelta(hours=9))


def _load_cost_model(path: Path) -> tuple[float, float]:
    fee = 0.0005
    slip = 0.003
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            fee = float(raw.get("fee_pct", fee))
            slip = float(raw.get("limit_slip_pct", slip))
        except Exception:
            pass
    return fee, slip


def _symbols_from_universe(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out: list[str] = []
        for row in data.get("symbols") or []:
            if row.get("robust") is True and row.get("symbol"):
                out.append(str(row["symbol"]))
        return sorted(set(out))
    except Exception:
        return []


def _slice_windows(df: pd.DataFrame, *, chunks: int) -> list[tuple[str, pd.DataFrame]]:
    """時系列を連続チャンクに分割（ロバスト簡易チェック用）。

    注意: チャンクはデータを切るだけなので、先頭チャンクで PSAR/RCI のウォームアップが
    不足し、全日シリーズ連続実行時の合計取引数・損益と一致しない。
    """
    if df is None or df.empty or chunks <= 1:
        return [("all", df)]
    n = len(df)
    if n < chunks * 30:
        return [("all", df)]
    edges = [int(round(i * n / chunks)) for i in range(chunks + 1)]
    parts: list[tuple[str, pd.DataFrame]] = []
    for i in range(chunks):
        a, b = edges[i], edges[i + 1]
        if b <= a:
            continue
        label = f"chunk_{i + 1}_of_{chunks}"
        parts.append((label, df.iloc[a:b]))
    return parts if parts else [("all", df)]


@dataclass
class SimStats:
    trades: int = 0
    wins: int = 0
    pnl_jpy: float = 0.0
    fees_jpy: float = 0.0
    gross_pnl_jpy: float = 0.0
    by_reason: dict[str, int] = field(default_factory=dict)


def simulate_portfolio_clean(
    sig: pd.DataFrame,
    *,
    shares: int,
    fee_pct: float,
    slip_pct: float,
    min_hold_bars: int,
    max_hold_bars: int,
    entry_cooldown_bars: int,
    sl_mode: str,
    sl_pct_from_entry: float,
) -> SimStats:
    """signal[i] は足終値時点で確定 → 約定は足 i+1 の始値。"""
    st = SimStats()
    df = sig
    if df.empty or len(df) < 3:
        return st

    o = df["open"].astype(float)
    low = df["low"].astype(float)
    sigv = df["signal"].astype(int)
    psar = df["psar_15m"].astype(float) if "psar_15m" in df.columns else None

    pos = 0
    fee_buy_acc = 0.0
    entry_notional = 0.0
    sl_price = float("nan")
    next_entry_allowed_t = 0
    entry_i = -1

    # t = 約定バー（open[t] で約定）。signal[t-1] が直前に確定したシグナル。
    for t in range(1, len(df)):
        ot = float(o.iloc[t])
        lt = float(low.iloc[t])

        # ── エントリー: 平坦かつ直前シグナルが 1 ─────────────────────
        if pos == 0 and int(sigv.iloc[t - 1]) == 1 and t >= next_entry_allowed_t:
            buy_px = ot * (1.0 + slip_pct)
            fee_buy = fee_pct * buy_px * shares
            fee_buy_acc = fee_buy
            entry_notional = buy_px * shares
            pos = 1
            entry_i = t
            bars_held = 1
            if sl_mode == "psar_15m" and psar is not None and pd.notna(psar.iloc[t]):
                sl_price = float(psar.iloc[t])
            elif sl_mode == "entry_pct":
                sl_price = buy_px * (1.0 - sl_pct_from_entry)
            else:
                sl_d = df["psar_d"].iloc[t] if "psar_d" in df.columns else float("nan")
                sl_price = float(sl_d) if pd.notna(sl_d) else (
                    float(psar.iloc[t]) if psar is not None and pd.notna(psar.iloc[t]) else float("nan")
                )
            # エントリー当日の盘中 SL（同一足）
            if pos == 1 and pd.notna(sl_price) and lt <= sl_price:
                sell_px = min(ot * (1.0 - slip_pct), float(sl_price) * (1.0 - slip_pct))
                fee_sell = fee_pct * sell_px * shares
                proceeds = sell_px * shares
                gross = proceeds - entry_notional
                pnl = gross - fee_buy_acc - fee_sell
                st.trades += 1
                if pnl > 0:
                    st.wins += 1
                st.pnl_jpy += pnl
                st.gross_pnl_jpy += gross
                st.fees_jpy += fee_buy_acc + fee_sell
                st.by_reason["stop_loss"] = st.by_reason.get("stop_loss", 0) + 1
                pos = 0
                next_entry_allowed_t = t + 1 + entry_cooldown_bars
                fee_buy_acc = 0.0
            continue

        if pos == 0:
            continue

        # ── ポジション管理（entry_i .. t）────────────────────────────
        bars_held = t - entry_i + 1
        if sl_mode == "psar_15m" and psar is not None and pd.notna(psar.iloc[t]):
            sl_price = max(sl_price, float(psar.iloc[t]))

        # 損切り
        if pd.notna(sl_price) and lt <= sl_price:
            sell_px = min(ot * (1.0 - slip_pct), float(sl_price) * (1.0 - slip_pct))
            fee_sell = fee_pct * sell_px * shares
            proceeds = sell_px * shares
            gross = proceeds - entry_notional
            pnl = gross - fee_buy_acc - fee_sell
            st.trades += 1
            if pnl > 0:
                st.wins += 1
            st.pnl_jpy += pnl
            st.gross_pnl_jpy += gross
            st.fees_jpy += fee_buy_acc + fee_sell
            st.by_reason["stop_loss"] = st.by_reason.get("stop_loss", 0) + 1
            pos = 0
            next_entry_allowed_t = t + 1 + entry_cooldown_bars
            fee_buy_acc = 0.0
            continue

        if max_hold_bars > 0 and bars_held >= max_hold_bars:
            sell_px = ot * (1.0 - slip_pct)
            fee_sell = fee_pct * sell_px * shares
            proceeds = sell_px * shares
            gross = proceeds - entry_notional
            pnl = gross - fee_buy_acc - fee_sell
            st.trades += 1
            if pnl > 0:
                st.wins += 1
            st.pnl_jpy += pnl
            st.gross_pnl_jpy += gross
            st.fees_jpy += fee_buy_acc + fee_sell
            st.by_reason["max_hold"] = st.by_reason.get("max_hold", 0) + 1
            pos = 0
            next_entry_allowed_t = t + 1 + entry_cooldown_bars
            fee_buy_acc = 0.0
            continue

        # 利確シグナル（直前足で -1 が立ったら当足始値で売り）
        if bars_held >= min_hold_bars and int(sigv.iloc[t - 1]) == -1:
            sell_px = ot * (1.0 - slip_pct)
            fee_sell = fee_pct * sell_px * shares
            proceeds = sell_px * shares
            gross = proceeds - entry_notional
            pnl = gross - fee_buy_acc - fee_sell
            st.trades += 1
            if pnl > 0:
                st.wins += 1
            st.pnl_jpy += pnl
            st.gross_pnl_jpy += gross
            st.fees_jpy += fee_buy_acc + fee_sell
            st.by_reason["take_profit_signal"] = st.by_reason.get("take_profit_signal", 0) + 1
            pos = 0
            next_entry_allowed_t = t + 1 + entry_cooldown_bars
            fee_buy_acc = 0.0
            continue

    return st


def _n225_context(first_ts: pd.Timestamp, last_ts: pd.Timestamp) -> dict:
    """日経平均の同一期間リターン（15m が短いときのレジーム文脈）。"""
    try:
        import yfinance as yf
    except Exception:
        return {}
    try:
        tk = yf.Ticker("^N225")
        df = tk.history(start=first_ts.tz_convert(None).date(), end=(last_ts + pd.Timedelta(days=1)).tz_convert(None).date(), auto_adjust=True)
        if df is None or df.empty or len(df) < 2:
            return {}
        c0 = float(df["Close"].iloc[0])
        c1 = float(df["Close"].iloc[-1])
        ret = (c1 / c0 - 1.0) * 100.0
        return {"n225_period_return_pct": round(ret, 3), "n225_bars": len(df)}
    except Exception:
        return {}


async def _main(args: argparse.Namespace) -> None:
    fee_pct, slip_pct_default = _load_cost_model(ROOT / "data" / "backtest_cost_model.json")
    slip_pct = args.slip_pct if args.slip_pct is not None else slip_pct_default
    fee_pct = args.fee_pct if args.fee_pct is not None else fee_pct

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        symbols = _symbols_from_universe(ROOT / "data" / "universe_active.json")
    if not symbols:
        symbols = ["6613.T", "3103.T", "9984.T", "6752.T", "1605.T"]

    chunks_n = max(1, int(args.chunk_regimes))

    results: list[dict] = []
    first_ts_global: pd.Timestamp | None = None
    last_ts_global: pd.Timestamp | None = None

    for sym in symbols:
        df_15_full = await fetch_ohlcv(sym, "15m", args.days_15m)
        df_h1_full = await fetch_ohlcv(sym, "1h", args.days_1h)
        df_d_full = await fetch_ohlcv(sym, "1d", args.days_d)
        if df_15_full is None or df_15_full.empty:
            results.append({"symbol": sym, "error": "no_15m"})
            continue

        windows = _slice_windows(df_15_full, chunks=chunks_n)
        for wlabel, df_15_w in windows:
            ts0 = df_15_w.index[0]
            ts1 = df_15_w.index[-1]
            if first_ts_global is None or ts0 < first_ts_global:
                first_ts_global = ts0
            if last_ts_global is None or ts1 > last_ts_global:
                last_ts_global = ts1

            df_h1_w = df_h1_full
            df_d_w = df_d_full
            if df_h1_w is not None and not df_h1_w.empty:
                df_h1_w = df_h1_w[(df_h1_w.index >= ts0) & (df_h1_w.index <= ts1)]
            if df_d_w is not None and not df_d_w.empty:
                df_d_w = df_d_w[(df_d_w.index >= ts0.normalize()) & (df_d_w.index <= ts1)]

            name = sym.replace(".T", "")
            strat = JPParabolicSwing(sym, name)
            strat.attach(df_d=df_d_w, df_h1=df_h1_w)
            sig = strat.generate_signals(df_15_w)
            st = simulate_portfolio_clean(
                sig,
                shares=args.shares,
                fee_pct=fee_pct,
                slip_pct=slip_pct,
                min_hold_bars=strat.min_hold_bars,
                max_hold_bars=strat.max_hold_bars,
                entry_cooldown_bars=strat.entry_cooldown_bars,
                sl_mode=strat.sl_mode,
                sl_pct_from_entry=strat.sl_pct_from_entry,
            )
            results.append({
                "symbol": sym,
                "window": wlabel,
                "bars_15m": len(df_15_w),
                "first_ts": str(df_15_w.index[0]),
                "last_ts": str(df_15_w.index[-1]),
                "trades": st.trades,
                "wins": st.wins,
                "win_rate": round(st.wins / st.trades, 4) if st.trades else 0.0,
                "pnl_jpy": round(st.pnl_jpy, 1),
                "fees_jpy": round(st.fees_jpy, 1),
                "gross_pnl_jpy": round(st.gross_pnl_jpy, 1),
                "by_reason": dict(st.by_reason),
                "fee_pct": fee_pct,
                "slip_pct": slip_pct,
                "shares": args.shares,
            })

    n225 = {}
    if first_ts_global is not None and last_ts_global is not None:
        n225 = _n225_context(pd.Timestamp(first_ts_global), pd.Timestamp(last_ts_global))

    by_window: dict[str, dict] = {}
    for r in results:
        if "error" in r:
            continue
        w = r["window"]
        acc = by_window.setdefault(w, {"pnl_jpy": 0.0, "trades": 0, "wins": 0, "rows": 0})
        acc["pnl_jpy"] += r["pnl_jpy"]
        acc["trades"] += r["trades"]
        acc["wins"] += r["wins"]
        acc["rows"] += 1

    summary_windows = {}
    for w, acc in by_window.items():
        summary_windows[w] = {
            "grand_pnl_jpy": round(acc["pnl_jpy"], 1),
            "total_trades": acc["trades"],
            "win_rate": round(acc["wins"] / acc["trades"], 4) if acc["trades"] else 0.0,
            "per_symbol_rows": acc["rows"],
        }

    payload = {
        "generated_at": datetime.now(JST).isoformat(),
        "cost_model": {"fee_pct": fee_pct, "slip_pct": slip_pct, "source": "cli_or_backtest_cost_model.json"},
        "benchmark_context": n225,
        "note_15m_history": (
            "15m は yfinance では約59日が上限のため、昨年暴落などの長期レジーム検証には "
            "algo_shared/ohlcv_cache の長期 parquet が必要。chunk は取得済み範囲内の時系列分割。"
        ),
        "args": {
            "symbols": symbols,
            "days_15m": args.days_15m,
            "chunk_regimes": chunks_n,
            "shares": args.shares,
        },
        "summary_by_window": summary_windows,
        "per_symbol": results,
    }

    out_path = Path(args.out) if args.out else ROOT / "data" / "sim_parabolic_swing_pnl_latest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description="JPParabolicSwing PnL with fees/slippage + regime chunks")
    ap.add_argument("--symbols", default="", help="Comma-separated; default: universe_active robust symbols")
    ap.add_argument("--days-15m", type=int, default=59)
    ap.add_argument("--days-1h", type=int, default=59)
    ap.add_argument("--days-d", type=int, default=800)
    ap.add_argument("--shares", type=int, default=100)
    ap.add_argument("--fee-pct", type=float, default=None)
    ap.add_argument("--slip-pct", type=float, default=None)
    ap.add_argument("--chunk-regimes", type=int, default=1, help="1=全体のみ, 3=時系列3分割でロバスト簡易検証")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
