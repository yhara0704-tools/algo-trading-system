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

sys.path.insert(0, "/root/algo-trading-system")

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
    ("6613.T", "QD Laser"),
    ("8306.T", "MUFG"),
    ("3382.T", "Seven&i"),
    ("9101.T", "NYK Line"),
]

FETCH_DAYS = 60
OUT_FILE   = pathlib.Path("/root/algo-trading-system/data/gate_comparison_result.json")


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


GATE_CONFIGS = [
    {
        "label": "v1_strict",
        "desc": "旧設定: A厳格(SMA5>SMA20), B必須, 加点1",
        "kwargs": dict(
            sma_fast=5, sma_slow=20, vol_ma_period=20,
            vol_min_ratio=0.5, vol_spike_ratio=1.3,
            time_start=(9, 15), time_end=(14, 45),
            momentum_bars=3, additive_needed=1,
            trend_strict=True, mandatory_a=True, mandatory_b=True,
        ),
    },
    {
        "label": "v2_relaxed_a",
        "desc": "改良: A緩和(close>SMA20), B必須, 加点1",
        "kwargs": dict(
            sma_fast=5, sma_slow=20, vol_ma_period=20,
            vol_min_ratio=0.5, vol_spike_ratio=1.3,
            time_start=(9, 15), time_end=(14, 45),
            momentum_bars=3, additive_needed=1,
            trend_strict=False, mandatory_a=True, mandatory_b=True,
        ),
    },
    {
        "label": "v3_all_additive",
        "desc": "全加点: A/B含む6条件から3つ以上",
        "kwargs": dict(
            sma_fast=5, sma_slow=20, vol_ma_period=20,
            vol_min_ratio=0.5, vol_spike_ratio=1.3,
            time_start=(9, 15), time_end=(14, 45),
            momentum_bars=3, additive_needed=3,
            trend_strict=False, mandatory_a=False, mandatory_b=False,
        ),
    },
]


def _calc_skip_pct(strat, df_is, r_gate) -> float:
    sig_df_raw  = strat.generate_signals(df_is)
    raw_entries = int((sig_df_raw["signal"].isin([1, -2])).sum())
    gate_entries = r_gate.num_trades
    return (
        (raw_entries - gate_entries) / raw_entries * 100
        if raw_entries > 0 else 0.0
    )


def _verdict(r_gate, r_no) -> str:
    if r_gate.daily_pnl_jpy > r_no.daily_pnl_jpy + 50:
        return "BETTER"
    elif r_gate.daily_pnl_jpy < r_no.daily_pnl_jpy - 50:
        return "WORSE"
    return "NEUTRAL"


async def main():
    print("=== エージェントゲート チューニング比較 ===\n")

    # 銘柄ごとにデータを先にフェッチしてキャッシュ
    data_cache: dict[str, pd.DataFrame] = {}
    for sym, name in TARGETS:
        df = await fetch_ohlcv(sym, "5m", FETCH_DAYS)
        if df.empty or len(df) < 100:
            print(f"  {name}: データ不足スキップ")
        else:
            data_cache[sym] = df

    all_config_results = {}

    for cfg in GATE_CONFIGS:
        gate = AgentGate(**cfg["kwargs"])
        label = cfg["label"]
        print(f"\n{'='*85}")
        print(f"  設定: [{label}] {cfg['desc']}")
        print('='*85)

        header = (
            f"{'銘柄':8s} {'手法':12s} | "
            f"{'ゲートなし日次':>10s} {'PF':>5s} {'T':>4s} | "
            f"{'ゲートあり日次':>10s} {'PF':>5s} {'T':>4s} {'Skip%':>6s} | 判定"
        )
        print(header)
        print("-" * 85)

        results: list[CompareRow] = []

        for sym, name in TARGETS:
            if sym not in data_cache:
                continue
            df     = data_cache[sym]
            split  = len(df) // 2
            df_is  = df.iloc[split:]

            for strat_name, strat in make_strategies(sym, name):
                try:
                    r_no   = _run(strat, df_is, gate=None)
                    r_gate = _run(strat, df_is, gate=gate)
                    skip_pct = _calc_skip_pct(strat, df_is, r_gate)
                    verdict  = _verdict(r_gate, r_no)

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

        # 設定ごとのサマリー
        better  = [r for r in results if r.verdict == "BETTER"]
        worse   = [r for r in results if r.verdict == "WORSE"]
        neutral = [r for r in results if r.verdict == "NEUTRAL"]
        avg_skip = sum(r.gate_skip_pct for r in results) / len(results) if results else 0
        avg_gate = sum(r.gate_daily for r in results) / len(results) if results else 0
        avg_no   = sum(r.no_gate_daily for r in results) / len(results) if results else 0

        print(f"\n  [{label}] BETTER:{len(better)} WORSE:{len(worse)} NEUTRAL:{len(neutral)}"
              f"  Skip:{avg_skip:.1f}%  Gate平均日次:{avg_gate:+,.0f}  No-gate平均:{avg_no:+,.0f}")

        all_config_results[label] = {
            "config": cfg,
            "better": len(better), "worse": len(worse), "neutral": len(neutral),
            "avg_skip_pct": round(avg_skip, 1),
            "avg_gate_daily": round(avg_gate, 1),
            "avg_no_gate_daily": round(avg_no, 1),
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
        }

    # 最終比較サマリー
    print(f"\n{'='*85}")
    print("  最終比較サマリー")
    print(f"{'='*85}")
    print(f"  {'設定':25s} | {'BETTER':>6s} {'WORSE':>5s} {'SKIP%':>6s} {'Gate日次avg':>12s} | 推奨スコア")
    print("  " + "-" * 70)
    for label, r in all_config_results.items():
        score = r["better"] - r["worse"]
        print(
            f"  {label:25s} | {r['better']:>6d} {r['worse']:>5d} {r['avg_skip_pct']:>5.1f}%"
            f" {r['avg_gate_daily']:>+12,.0f} | score={score:+d}"
        )

    # 保存
    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(
        {"date": str(date.today()), "configs": all_config_results},
        ensure_ascii=False, indent=2
    ))
    print(f"\n結果保存: {OUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
