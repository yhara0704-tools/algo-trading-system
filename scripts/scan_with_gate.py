"""エージェントゲート 効果検証スクリプト.

「ゲートなし」と「ゲートあり」の損益を銘柄×手法で比較し、
ゲートが本当に価値を出しているかを定量的に検証する。

実行:
    cd /root/algo-trading-system
    .venv/bin/python3 scripts/scan_with_gate.py
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from dataclasses import dataclass
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pandas as pd

from backend.backtesting.engine import run_backtest
from backend.lab.runner import fetch_ohlcv, JP_CAPITAL_JPY, MARGIN_RATIO, POSITION_PCT, LOT_SIZE
from backend.strategies.jp_stock.jp_macd_rci import JPMacdRci
from backend.strategies.jp_stock.jp_scalp import JPScalp
from backend.strategies.jp_stock.jp_breakout import JPBreakout
from backend.strategies.jp_stock.agent_gate import AgentGate

# ── 検証対象 ──────────────────────────────────────────────────────────────────
TARGETS = [
    ("2413.T", "M3"),
    ("6758.T", "Sony"),
    ("8136.T", "Sanrio"),
    ("3103.T", "Unitika"),
    # ("6613.T", "QD Laser"),  # v2: GATE除外（ノイジー銘柄）
    ("8306.T", "MUFG"),
    ("3382.T", "Seven&i"),
    ("9101.T", "NYK Line"),
]

FETCH_DAYS = 60
OUT_FILE   = pathlib.Path(__file__).resolve().parent.parent / "data" / "gate_comparison_result.json"


def _run(strat, df, gate=None):
    return run_backtest(
        strat, df,
        starting_cash=JP_CAPITAL_JPY * MARGIN_RATIO,
        fee_pct=0.0, position_pct=POSITION_PCT,
        usd_jpy=1.0, lot_size=LOT_SIZE,
        limit_slip_pct=0.003, eod_close_time=(15, 20),
        gate=gate,
    )


def make_strategies(sym: str, name: str) -> list[tuple[str, object]]:
    return [
        ("MacdRci", JPMacdRci(sym, name, interval="5m")),
        ("Scalp",   JPScalp(sym, name, interval="5m")),
        ("Breakout",JPBreakout(sym, name, interval="5m")),
    ]


@dataclass
class CompareRow:
    symbol:        str
    name:          str
    strategy:      str
    no_gate_daily: float
    no_gate_pf:    float
    no_gate_trades:int
    gate_daily:    float
    gate_pf:       float
    gate_trades:   int
    gate_skip_pct: float   # ゲートが止めたシグナルの割合
    verdict:       str     # "BETTER" / "WORSE" / "NEUTRAL"


async def main():
    print("=== エージェントゲート 効果検証 ===\n")

    # ゲート設定（デフォルト: 必須A+B, 加点1つ以上）
    gate = AgentGate(
        sma_fast=5, sma_slow=20,
        vol_ma_period=20, vol_min_ratio=0.5,   # B: v2確定値
        vol_spike_ratio=1.3,
        time_start=(9, 15), time_end=(14, 45),
        momentum_bars=3, additive_needed=1,
    )

    results: list[CompareRow] = []

    header = (
        f"{'銘柄':8s} {'手法':12s} | "
        f"{'ゲートなし日次':>10s} {'PF':>5s} {'T':>4s} | "
        f"{'ゲートあり日次':>10s} {'PF':>5s} {'T':>4s} {'Skip%':>6s} | 判定"
    )
    print(header)
    print("-" * 85)

    for sym, name in TARGETS:
        df = await fetch_ohlcv(sym, "5m", FETCH_DAYS)
        if df.empty or len(df) < 100:
            print(f"  {name}: データ不足スキップ")
            continue

        split  = len(df) // 2
        df_is  = df.iloc[split:]  # 直近30日をIS

        for strat_name, strat in make_strategies(sym, name):
            try:
                r_no  = _run(strat, df_is, gate=None)
                r_gate = _run(strat, df_is, gate=gate)

                # ゲートがスキップしたシグナル率
                sig_df_raw  = strat.generate_signals(df_is)
                raw_entries = int((sig_df_raw["signal"].isin([1, -2])).sum())
                gate_entries = r_gate.num_trades
                skip_pct = (
                    (raw_entries - gate_entries) / raw_entries * 100
                    if raw_entries > 0 else 0.0
                )

                # 判定
                if r_gate.daily_pnl_jpy > r_no.daily_pnl_jpy + 50:
                    verdict = "BETTER"
                elif r_gate.daily_pnl_jpy < r_no.daily_pnl_jpy - 50:
                    verdict = "WORSE"
                else:
                    verdict = "NEUTRAL"

                row = CompareRow(
                    symbol=sym, name=name, strategy=strat_name,
                    no_gate_daily=round(float(r_no.daily_pnl_jpy), 1),
                    no_gate_pf=round(float(r_no.profit_factor), 2),
                    no_gate_trades=r_no.num_trades,
                    gate_daily=round(float(r_gate.daily_pnl_jpy), 1),
                    gate_pf=round(float(r_gate.profit_factor), 2),
                    gate_trades=r_gate.num_trades,
                    gate_skip_pct=round(skip_pct, 1),
                    verdict=verdict,
                )
                results.append(row)

                print(
                    f"{name:8s} {strat_name:12s} | "
                    f"{r_no.daily_pnl_jpy:>+10,.0f} {r_no.profit_factor:>5.2f} {r_no.num_trades:>4d} | "
                    f"{r_gate.daily_pnl_jpy:>+10,.0f} {r_gate.profit_factor:>5.2f} {r_gate.num_trades:>4d} "
                    f"{skip_pct:>5.1f}% | {verdict}"
                )

            except Exception as e:
                print(f"  {name} × {strat_name}: エラー {e}")

    # 集計
    print("\n=== サマリー ===")
    better  = [r for r in results if r.verdict == "BETTER"]
    worse   = [r for r in results if r.verdict == "WORSE"]
    neutral = [r for r in results if r.verdict == "NEUTRAL"]
    print(f"BETTER: {len(better)}件 / WORSE: {len(worse)}件 / NEUTRAL: {len(neutral)}件")

    if better:
        avg_improvement = sum(r.gate_daily - r.no_gate_daily for r in better) / len(better)
        print(f"BETTER 平均改善幅: +{avg_improvement:,.0f}円/日")
    if worse:
        avg_degradation = sum(r.no_gate_daily - r.gate_daily for r in worse) / len(worse)
        print(f"WORSE  平均悪化幅: -{avg_degradation:,.0f}円/日")

    avg_skip = sum(r.gate_skip_pct for r in results) / len(results) if results else 0
    print(f"平均スキップ率: {avg_skip:.1f}%（ゲートが除外したシグナル）")

    # 保存
    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(
        {
            "date": str(date.today()),
            "gate_config": {
                "sma_fast": gate.sma_fast, "sma_slow": gate.sma_slow,
                "vol_min_ratio": gate.vol_min_ratio,
                "vol_spike_ratio": gate.vol_spike_ratio,
                "additive_needed": gate.additive_needed,
            },
            "results": [
                {
                    "symbol": r.symbol, "name": r.name, "strategy": r.strategy,
                    "no_gate_daily": r.no_gate_daily, "no_gate_pf": r.no_gate_pf,
                    "no_gate_trades": r.no_gate_trades,
                    "gate_daily": r.gate_daily, "gate_pf": r.gate_pf,
                    "gate_trades": r.gate_trades,
                    "gate_skip_pct": r.gate_skip_pct, "verdict": r.verdict,
                }
                for r in results
            ],
            "summary": {
                "better": len(better), "worse": len(worse), "neutral": len(neutral),
                "avg_skip_pct": round(avg_skip, 1),
            }
        },
        ensure_ascii=False, indent=2
    ))
    print(f"\n結果保存: {OUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
