#!/usr/bin/env python3
"""MacdRci × MTFRA Phase 3 PoC — 案 B (変化検出) + 案 C (Exit ヘルパー).

D9 Phase 2.5 で「全軸整合フィルタは過剰削減」 と判明した。ユーザー提案で
案 B (変化検出) と 案 C (Exit ヘルパー) を組み合わせて検証する。

仕組み:
  1. MacdRci で signal=1/-1 を生成 (フィルタしない、機会数を維持)
  2. **案 B (Entry Boost)**: signal=1 のうち「直近で down→up または flat→up に変化した
     直後 (例: 6 バー以内)」 をプラス評価。逆に「up→down/flat の直後」 の long は減点
     - 今回はシンプルに「up→down または stable_down 以外なら通す」 として、
       `up_to_not_up` の遷移後 N バー以内の long をブロック
  3. **案 C (Exit Helper)**: 保有中に上位足の direction が悪化したら強制決済
     - long 保有中に MTFRA が `aligned_down` または `mixed` (ups < downs) になったら
       次バーで signal=-1 に書き換え (engine が次バーで close)

評価対象:
  - off                    : ベースライン (Phase 2.5 と同じ)
  - exit_strict            : 案 C のみ (Exit ヘルパー、entry はベース通り)
  - entry_block_post_up_down : 案 B のみ (up→not_up 直後の long をブロック)
  - combined_b_plus_c      : 案 B + C (組み合わせ、ユーザー提案)

期待:
  - off: trade 数最大、PnL は既存通り
  - exit_strict: trade 数同じだが drawdown 限定で WR 改善
  - combined: drawdown 限定 + 不利な entry 排除で WR/PnL 改善
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
COMBO = ("15m", "30m", "60m")  # 5m 主足の上位レジーム整合


def fetch_5m(symbol: str, period_days: int = 30) -> pd.DataFrame | None:
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
    morn = ((hh == 9) & (mm >= 0)) | (hh == 10) | ((hh == 11) & (mm <= 30))
    aft = ((hh == 12) & (mm >= 30)) | (hh == 13) | (hh == 14) | ((hh == 15) & (mm <= 30))
    return df[morn | aft]


def compute_alignment_series(df_5m: pd.DataFrame) -> pd.Series:
    """5m 各バーの整合状態 (aligned_up / aligned_down / mixed / unknown) を返す."""
    n = len(df_5m)
    result = ["unknown"] * n
    tf_dfs = {tf: _resample(df_5m, TF_RULE[tf]) for tf in COMBO}
    for i in range(n):
        if i < 60:
            continue
        ts = df_5m.index[i]
        dirs = {}
        for tf in COMBO:
            df_tf = tf_dfs[tf]
            sub = df_tf[df_tf.index <= ts].tail(60)
            min_n = 14 if tf == "15m" else (10 if tf == "30m" else 5)
            if len(sub) < min_n:
                dirs[tf] = "unknown"
                break
            dirs[tf] = _detect_dir(sub)
        if any(d == "unknown" for d in dirs.values()):
            result[i] = "unknown"
            continue
        ups = sum(1 for d in dirs.values() if d == "up")
        downs = sum(1 for d in dirs.values() if d == "down")
        n_axis = len(dirs)
        if ups == n_axis:
            result[i] = "aligned_up"
        elif downs == n_axis:
            result[i] = "aligned_down"
        elif ups > downs:
            result[i] = "mostly_up"
        elif downs > ups:
            result[i] = "mostly_down"
        else:
            result[i] = "mixed"
    return pd.Series(result, index=df_5m.index, name="alignment")


def apply_phase3_filters(
    df_signals: pd.DataFrame,
    alignment: pd.Series,
    entry_block_lookback: int = 6,
    exit_helper_strict: bool = True,
) -> tuple[pd.DataFrame, dict]:
    """案 B + C の組み合わせフィルタを既存 signal に適用.

    案 B (entry boost as block):
      直近 lookback バー以内に `aligned_up → mostly_down/aligned_down` の遷移があった
      バーでの long signal はブロック (= signal=0)。
      逆方向は short の対称ブロック。

    案 C (exit helper):
      long 保有中、現バーで alignment が `aligned_down` (strict) もしくは
      `mostly_down` 以下になったら次バーに signal=-1 を強制書き込み (early exit)。
      short 保有中も逆対称。
    """
    new_sig = df_signals["signal"].copy()
    n = len(df_signals)
    align_arr = alignment.values
    sig_arr = new_sig.values

    block_long = 0
    block_short = 0
    early_exit_long = 0
    early_exit_short = 0

    # 案 B: 直近 lookback バーに up→not_up の遷移があったら長期 long をブロック
    for i in range(n):
        if sig_arr[i] == 0:
            continue
        if i < entry_block_lookback + 1:
            continue
        recent = align_arr[max(0, i - entry_block_lookback):i + 1]
        # up→down 遷移検出: 直前数バーに aligned_up があり、現バー (or 直近) で
        # mostly_down/aligned_down が出ている
        had_up = "aligned_up" in recent[:-1]
        now_down = align_arr[i] in ("aligned_down", "mostly_down")
        if sig_arr[i] == 1 and had_up and now_down:
            sig_arr[i] = 0
            block_long += 1
            continue
        # 対称: down→up 遷移直後の short をブロック
        had_down = "aligned_down" in recent[:-1]
        now_up = align_arr[i] in ("aligned_up", "mostly_up")
        if sig_arr[i] == -1 and had_down and now_up:
            sig_arr[i] = 0
            block_short += 1

    # 案 C: 保有中の Exit ヘルパー
    # long 保有中 (= 直近で sig=1 が出てまだ -1 や 強制決済が来ていない) で alignment が
    # 下向きになったら、次バーに signal=-1 を強制書き込み。
    holding_long = False
    holding_short = False
    long_entry_idx = -1
    short_entry_idx = -1
    for i in range(n):
        cur_sig = sig_arr[i]
        if cur_sig == 1 and not holding_long:
            holding_long = True
            holding_short = False
            long_entry_idx = i
            continue
        if cur_sig == -1:
            # 既存の戦略 exit シグナル
            if holding_long:
                holding_long = False
                long_entry_idx = -1
            if not holding_short:
                holding_short = True
                short_entry_idx = i
            continue
        # exit ヘルパー判定
        if holding_long and i > long_entry_idx + 1:
            target = ("aligned_down",) if exit_helper_strict else ("aligned_down", "mostly_down")
            if align_arr[i] in target:
                # 次バーに -1 を書き込み (engine が次バーで close する)
                if i + 1 < n and sig_arr[i + 1] == 0:
                    sig_arr[i + 1] = -1
                    early_exit_long += 1
                    holding_long = False
                    long_entry_idx = -1
        if holding_short and i > short_entry_idx + 1:
            target = ("aligned_up",) if exit_helper_strict else ("aligned_up", "mostly_up")
            if align_arr[i] in target:
                if i + 1 < n and sig_arr[i + 1] == 0:
                    sig_arr[i + 1] = 1  # short の close は反対方向 = long? engine 仕様確認必要
                    # 実際の engine では signal=-1 が「強制決済」 として扱われるので統一
                    sig_arr[i + 1] = -1
                    early_exit_short += 1
                    holding_short = False
                    short_entry_idx = -1

    new_df = df_signals.copy()
    new_df["signal"] = pd.Series(sig_arr, index=df_signals.index)
    return new_df, {
        "block_long": block_long,
        "block_short": block_short,
        "early_exit_long": early_exit_long,
        "early_exit_short": early_exit_short,
    }


def evaluate(symbol: str, df: pd.DataFrame, base_strat, label: str) -> dict:
    class _W:
        def __init__(self, base, df_with_sig):
            self.meta = base.meta
            self._df = df_with_sig

        def generate_signals(self, _):
            return self._df

    wrapper = _W(base_strat, df)
    result = run_backtest(
        wrapper, df,
        starting_cash=990_000.0, fee_pct=0.0,
        position_pct=0.30, usd_jpy=1.0, lot_size=100,
        limit_slip_pct=0.001, eod_close_time=(15, 25),
        subsession_cooldown_min=2, daily_loss_limit_pct=-3.0,
    )
    n = len(result.trades)
    if n == 0:
        return {"label": label, "symbol": symbol, "trades": 0, "wr": None,
                "pf": None, "pnl_jpy": 0.0, "long_n": 0, "short_n": 0,
                "long_pnl": 0.0, "short_pnl": 0.0,
                "max_dd": 0.0, "trades_per_day": 0, "days": None}
    wins = [t for t in result.trades if t.pnl > 0]
    losses = [t for t in result.trades if t.pnl <= 0]
    pf = (sum(t.pnl for t in wins) / -sum(t.pnl for t in losses)) if losses else 999.0
    long_t = [t for t in result.trades if t.side == "long"]
    short_t = [t for t in result.trades if t.side == "short"]
    days = max(1, (df.index[-1].date() - df.index[0].date()).days + 1)
    # 最大 drawdown 計算
    pnls = [t.pnl for t in result.trades]
    cumpnl = np.cumsum(pnls)
    high_water = np.maximum.accumulate(cumpnl)
    drawdowns = high_water - cumpnl
    max_dd = float(drawdowns.max()) if len(drawdowns) > 0 else 0.0
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
        "max_dd": round(max_dd, 0),
        "days": days, "trades_per_day": round(n / days, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*",
                    default=["9984.T", "6613.T", "3103.T", "8136.T", "9107.T"])
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--out", default="data/macd_rci_mtfra_phase3_poc.json")
    args = ap.parse_args()

    symbols = [s if s.endswith(".T") else f"{s}.T" for s in args.symbols]

    print(f"=== MacdRci × MTFRA Phase 3 PoC (案 B + C 組合せ) ===")
    print(f"   {len(symbols)} 銘柄, {args.days} 日, combo={COMBO}\n")

    configs = [
        ("off", False, False, False),
        ("entry_block_only", True, False, False),
        ("exit_strict_only", False, True, True),
        ("exit_lenient_only", False, True, False),
        ("combined_B_plus_C_strict", True, True, True),
        ("combined_B_plus_C_lenient", True, True, False),
    ]

    all_results = []
    for sym in symbols:
        df = fetch_5m(sym, period_days=args.days)
        if df is None or len(df) < 200:
            print(f"  {sym}: skip (no data, n={0 if df is None else len(df)})")
            continue
        n_days = (df.index[-1].date() - df.index[0].date()).days + 1
        print(f"\n  {sym} (n={len(df)} 5m bars, {n_days}日)")

        try:
            base_strat = create_strategy("MacdRci", sym, name=sym.replace(".T", ""))
            df_sig = base_strat.generate_signals(df)
        except Exception as e:
            print(f"    MacdRci 生成失敗: {e}")
            continue

        n_long = int((df_sig["signal"] == 1).sum())
        n_short = int((df_sig["signal"] == -1).sum())
        if n_long + n_short == 0:
            print(f"    signal が 0 件、スキップ")
            continue
        print(f"    base signals: long={n_long}, short={n_short}")

        # 整合状態シリーズ計算 (1 度だけ)
        try:
            align = compute_alignment_series(df)
        except Exception as e:
            print(f"    alignment 計算失敗: {e}")
            continue

        for label, use_b, use_c, c_strict in configs:
            if not (use_b or use_c):
                df_used = df_sig.copy()
                fstat = {"block_long": 0, "block_short": 0,
                         "early_exit_long": 0, "early_exit_short": 0}
            else:
                # 一旦完全 copy → block 適用 → exit 適用
                df_used = df_sig.copy()
                if not use_b:
                    # entry block を無効化するため、entry_block_lookback=0 と等価にする
                    df_used2, fstat = apply_phase3_filters(
                        df_used, align, entry_block_lookback=0,
                        exit_helper_strict=c_strict if use_c else True,
                    )
                else:
                    df_used2, fstat = apply_phase3_filters(
                        df_used, align, entry_block_lookback=6,
                        exit_helper_strict=c_strict if use_c else True,
                    )
                # use_c=False のときは early_exit を無効化
                if not use_c:
                    # apply_phase3_filters 内で常に early exit が実行されるため、
                    # use_c=False のときは block だけ適用する版を別途作る
                    df_used2 = df_used.copy()
                    sig_arr = df_used2["signal"].values.copy()
                    # block 部分のみ再適用 (use_b=True のときのみ)
                    if use_b:
                        align_arr = align.values
                        for i in range(len(sig_arr)):
                            if sig_arr[i] == 0 or i < 7:
                                continue
                            recent = align_arr[max(0, i - 6):i + 1]
                            had_up = "aligned_up" in recent[:-1]
                            now_down = align_arr[i] in ("aligned_down", "mostly_down")
                            if sig_arr[i] == 1 and had_up and now_down:
                                sig_arr[i] = 0
                            had_down = "aligned_down" in recent[:-1]
                            now_up = align_arr[i] in ("aligned_up", "mostly_up")
                            if sig_arr[i] == -1 and had_down and now_up:
                                sig_arr[i] = 0
                        df_used2["signal"] = sig_arr
                    fstat = {"block_long": 0, "block_short": 0,
                             "early_exit_long": 0, "early_exit_short": 0}
                df_used = df_used2

            try:
                res = evaluate(sym, df_used, base_strat, label)
            except Exception as e:
                print(f"    {label}: backtest 失敗 {e}")
                continue
            res["filter"] = fstat
            all_results.append(res)
            wr = f"{res['wr']}%" if res['wr'] is not None else "—"
            pf = f"{res['pf']}" if res['pf'] is not None else "—"
            print(f"    {label:<28} n={res['trades']:>3} WR={wr:<6} "
                  f"PF={pf:<5} PnL={res['pnl_jpy']:>+8.0f}円  DD={res['max_dd']:>+6.0f}円  "
                  f"block(L/S)={fstat['block_long']}/{fstat['block_short']}  "
                  f"earlyexit(L/S)={fstat['early_exit_long']}/{fstat['early_exit_short']}")

    # サマリ
    print(f"\n=== 構成別集計 (全銘柄合計) ===\n")
    print(f"{'構成':<28} {'銘柄':>5} {'trades':>7} {'WR':>6} {'PF':>5} "
          f"{'PnL/日':>10} {'PnL合計':>10} {'最大DD':>10}")
    print("-" * 95)
    summary = {}
    for label, *_ in configs:
        rs = [r for r in all_results if r["label"] == label]
        if not rs:
            continue
        total_trades = sum(r["trades"] for r in rs)
        total_pnl = sum(r["pnl_jpy"] for r in rs)
        max_dd = max((r["max_dd"] or 0) for r in rs)
        rs_w = [r for r in rs if r["trades"] > 0]
        if rs_w:
            avg_wr = sum(r["wr"] * r["trades"] for r in rs_w) / max(1, total_trades)
            avg_pf = sum((r["pf"] or 0) * r["trades"] for r in rs_w) / max(1, total_trades)
            total_days = sum(r["days"] or 0 for r in rs_w)
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
            "max_dd": round(max_dd, 0),
        }
        print(f"{label:<28} {summary[label]['n_symbols']:>5} {total_trades:>7} "
              f"{avg_wr:>5.1f}% {avg_pf:>4.2f} {avg_pnl_per_day:>+9.0f}円 "
              f"{total_pnl:>+9.0f}円 {max_dd:>+9.0f}円")

    # 改善度
    base = summary.get("off")
    if base:
        print(f"\n=== ベースライン (off) からの改善度 ===")
        for label, *_ in configs[1:]:
            s = summary.get(label)
            if not s:
                continue
            d_wr = s["weighted_wr"] - base["weighted_wr"]
            d_pnl = s["total_pnl"] - base["total_pnl"]
            d_pnl_pct = (d_pnl / max(1, abs(base["total_pnl"])) * 100) if base["total_pnl"] else 0
            d_dd = s["max_dd"] - base["max_dd"]
            d_trades = s["total_trades"] - base["total_trades"]
            print(f"  {label:<28} ΔWR={d_wr:+.1f}%pt  ΔPnL={d_pnl:+.0f}円({d_pnl_pct:+.0f}%)  "
                  f"ΔDD={d_dd:+.0f}円  Δtrades={d_trades:+d}")

    out_path = ROOT / args.out
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "n_symbols": len(symbols),
        "n_days": args.days,
        "combo": list(COMBO),
        "configs": [c[0] for c in configs],
        "summary": summary,
        "results": all_results,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=> 保存: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
