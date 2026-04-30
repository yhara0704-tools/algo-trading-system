#!/usr/bin/env python3
"""MicroScalp v5 — MTFRA フィルタ統合版バックテスト (D9 Phase 2).

D9 Phase 2 で実装した MTFRA フィルタを MicroScalp に組み込み、4 構成で比較:
  - off:        フィルタなし (v3 ベースライン)
  - default:    3m + 30m + 60m 整合 (D9 Phase 1.6 の実用最強解)
  - aggressive: 1m + 3m + 15m + 60m 整合 (高 WR モード)
  - per_symbol: data/mtfra_optimal_per_symbol.json から銘柄別最適化

データソース: data/ohlcv_1m/<symbol>/*.parquet (D6 で蓄積した 7 日分)
出力: data/micro_scalp_v5_mtfra_latest.json
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.backtesting.engine import run_backtest  # noqa: E402
from backend.backtesting.strategy_factory import create as create_strategy  # noqa: E402

OHLCV_DIR = ROOT / "data/ohlcv_1m"
JST = "Asia/Tokyo"


def load_1m(symbol: str) -> pd.DataFrame | None:
    """ohlcv_1m から 1m データを連結ロード."""
    sdir = OHLCV_DIR / symbol.replace(".", "_")
    if not sdir.exists():
        return None
    parts = []
    for p in sorted(sdir.glob("*.parquet")):
        try:
            d = pd.read_parquet(p)
            if not d.empty:
                parts.append(d)
        except Exception:
            continue
    if not parts:
        return None
    df = pd.concat(parts).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(JST)
    # ザラ場のみ抽出 (9:00-11:30, 12:30-15:30)
    hh, mm = df.index.hour, df.index.minute
    in_morn = ((hh == 9) & (mm >= 0)) | (hh == 10) | ((hh == 11) & (mm <= 30))
    in_aft = ((hh == 12) & (mm >= 30)) | (hh == 13) | (hh == 14) | ((hh == 15) & (mm <= 30))
    return df[in_morn | in_aft]


def evaluate(symbol: str, df: pd.DataFrame, params: dict, label: str) -> dict:
    name = symbol.replace(".T", "")
    strat = create_strategy("MicroScalp", symbol, name=name, params=params)
    result = run_backtest(
        strat, df,
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
                "trades_per_day": 0, "days": None}
    wins = [t for t in result.trades if t.pnl > 0]
    losses = [t for t in result.trades if t.pnl <= 0]
    pf = (sum(t.pnl for t in wins) / -sum(t.pnl for t in losses)) if losses else 999.0
    long_t = [t for t in result.trades if t.side == "long"]
    short_t = [t for t in result.trades if t.side == "short"]
    days = max(1, (df.index[-1].date() - df.index[0].date()).days + 1)
    return {
        "label": label, "symbol": symbol,
        "trades": n,
        "wr": round(len(wins) / n * 100, 1),
        "pf": round(pf, 2),
        "long_n": len(long_t), "short_n": len(short_t),
        "long_pnl": round(float(sum(t.pnl for t in long_t)), 0),
        "short_pnl": round(float(sum(t.pnl for t in short_t)), 0),
        "long_wr": round(sum(1 for t in long_t if t.pnl > 0) / max(1, len(long_t)) * 100, 1) if long_t else None,
        "short_wr": round(sum(1 for t in short_t if t.pnl > 0) / max(1, len(short_t)) * 100, 1) if short_t else None,
        "pnl_jpy": round(float(sum(t.pnl for t in result.trades)), 0),
        "days": days,
        "trades_per_day": round(n / days, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*",
                    help="検証銘柄 (省略時は ohlcv_1m 全銘柄)")
    ap.add_argument("--out", default="data/micro_scalp_v5_mtfra_latest.json")
    args = ap.parse_args()

    if args.symbols:
        symbols = [s if s.endswith(".T") else f"{s}.T" for s in args.symbols]
    else:
        symbols = sorted([
            p.name.replace("_", ".") for p in OHLCV_DIR.iterdir() if p.is_dir()
        ])

    print(f"=== MicroScalp v5 (MTFRA) 検証 ({len(symbols)} 銘柄) ===\n")

    base_params = {
        "interval": "1m",
        "tp_jpy": 5.0, "sl_jpy": 5.0,
        "entry_dev_jpy": 8.0, "atr_period": 10,
        "atr_min_jpy": 3.0, "atr_max_jpy": 0.0,
        "timeout_bars": 2, "cooldown_bars": 5,
        "avoid_open_min": 5, "avoid_close_min": 30,
        "morning_only": False, "allow_short": True,
        "max_trades_per_day": 0,
        "allowed_time_windows": ["09:00-09:30", "12:30-15:00"],
        "open_bias_mode": False,
    }

    configs = [
        ("v3_baseline_off", {**base_params, "mtfra_mode": "off"}),
        ("v5_mtfra_default", {**base_params, "mtfra_mode": "default"}),
        ("v5_mtfra_aggressive", {**base_params, "mtfra_mode": "aggressive"}),
        ("v5_mtfra_per_symbol", {**base_params, "mtfra_mode": "per_symbol"}),
    ]

    all_results: list[dict] = []
    for sym in symbols:
        df = load_1m(sym)
        if df is None or df.empty:
            print(f"  {sym}: skip (no data)")
            continue
        print(f"  {sym} (n={len(df)} bars)")
        for label, params in configs:
            try:
                res = evaluate(sym, df, params, label)
            except Exception as e:
                print(f"    {label}: ERROR {e}")
                continue
            all_results.append(res)
            wr_str = f"{res['wr']}%" if res['wr'] else "—"
            pf_str = f"{res['pf']}" if res['pf'] else "—"
            print(f"    {label:<22} n={res['trades']:>3} WR={wr_str:<6} "
                  f"PF={pf_str:<5} PnL={res['pnl_jpy']:>+7.0f}円")

    # ── 構成別の集計 ─────────────────────────────────────
    print(f"\n=== 構成別集計 (全銘柄合計) ===\n")
    print(f"{'構成':<24} {'銘柄':>5} {'trades':>7} "
          f"{'WR':>6} {'PF':>5} {'PnL/日':>10} {'PnL合計':>10}")
    print("-" * 80)
    summary = {}
    for label, _ in configs:
        rs = [r for r in all_results if r["label"] == label]
        if not rs:
            continue
        total_trades = sum(r["trades"] for r in rs)
        total_pnl = sum(r["pnl_jpy"] for r in rs)
        rs_with = [r for r in rs if r["trades"] > 0]
        if rs_with:
            avg_wr = sum(r["wr"] * r["trades"] for r in rs_with) / max(1, sum(r["trades"] for r in rs_with))
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

    # ── 改善度サマリ ─────────────────────────────────────
    base = summary.get("v3_baseline_off")
    if base:
        print(f"\n=== ベースライン (off) からの改善度 ===\n")
        for label, _ in configs[1:]:
            s = summary.get(label)
            if not s:
                continue
            d_wr = s["weighted_wr"] - base["weighted_wr"]
            d_pnl = s["total_pnl"] - base["total_pnl"]
            d_pnl_pct = (s["total_pnl"] - base["total_pnl"]) / max(1, abs(base["total_pnl"])) * 100 if base["total_pnl"] else 0
            d_trades = s["total_trades"] - base["total_trades"]
            print(f"  {label:<24} ΔWR={d_wr:+.1f}%pt  ΔPnL={d_pnl:+.0f}円 ({d_pnl_pct:+.0f}%)  "
                  f"Δtrades={d_trades:+d}")

    # 出力
    out_path = ROOT / args.out
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "n_symbols": len(symbols),
        "configs": [c[0] for c in configs],
        "summary": summary,
        "results": all_results,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=> 保存: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
