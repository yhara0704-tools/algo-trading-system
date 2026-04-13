"""BII公開用エンドポイント — 検閲済みデータのみ配信."""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/bii")

_runner = None
JST = timezone(timedelta(hours=9))


def inject(runner) -> None:
    global _runner
    _runner = runner


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    """最新の検閲済み日次サマリーを返す."""
    today = datetime.now(JST).strftime("%Y-%m-%d")
    _project_root = pathlib.Path(__file__).resolve().parent.parent.parent
    path = _project_root / "algo_shared" / "daily" / f"{today}.json"

    if path.exists():
        return json.loads(path.read_text())

    # ファイルがまだない場合はライブデータから生成（未検閲警告付き）
    if _runner is None:
        raise HTTPException(503, "No summary available")

    results = _runner.get_results()
    jp_done = [r for r in results
               if r.get("num_trades", 0) > 0
               and r.get("symbol", "").endswith(".T")]
    if not jp_done:
        return {"date": today, "phase": "backtest",
                "pnl_today_jpy": 0, "pnl_cumulative_jpy": 0,
                "trade_count": 0, "win_count": 0}

    best = max(jp_done, key=lambda r: r.get("score", 0))
    tc = best.get("num_trades", 0)
    wc = int(tc * best.get("win_rate", 0) / 100)
    return {
        "date": today,
        "phase": "backtest",
        "pnl_today_jpy": round(best.get("daily_pnl_jpy", 0)),
        "pnl_cumulative_jpy": round(best.get("daily_pnl_jpy", 0)),
        "trade_count": tc,
        "win_count": wc,
    }


@router.get("/chart")
async def get_chart() -> dict[str, Any]:
    """最良JP戦略のOHLCV＋トレードを返す（銘柄コード匿名化・軸値そのまま）."""
    if _runner is None:
        raise HTTPException(503, "Lab not ready")

    results = _runner.get_results()
    jp_done = [r for r in results
               if r.get("num_trades", 0) > 0
               and r.get("symbol", "").endswith(".T")]
    if not jp_done:
        return {"candles": [], "trades": [], "symbol_label": "銘柄A",
                "strategy_label": "戦略A", "interval": "5m"}

    best = max(jp_done, key=lambda r: r.get("score", 0))
    symbol = best.get("symbol", "")
    interval = best.get("interval", "5m")

    from backend.lab.runner import fetch_ohlcv
    df = await fetch_ohlcv(symbol, interval, days=30)

    candles = []
    if df is not None and not df.empty:
        for i, (ts, row) in enumerate(df.iterrows()):
            candles.append({
                "time": i + 1,
                "open":  float(row["open"]),
                "high":  float(row["high"]),
                "low":   float(row["low"]),
                "close": float(row["close"]),
            })

    # entry_time / exit_time 文字列 → 連番インデックスに変換
    ts_index: dict[str, int] = {}
    if df is not None and not df.empty:
        for i, ts in enumerate(df.index):
            # タイムゾーンありなしどちらでもマッチできるよう複数形式を登録
            ts_index[str(ts)] = i + 1
            ts_naive = ts.replace(tzinfo=None) if hasattr(ts, 'tzinfo') else ts
            ts_index[str(ts_naive)] = i + 1

    def _find_bar(time_str: str) -> int:
        if not time_str:
            return 0
        # 直接マッチ
        if time_str in ts_index:
            return ts_index[time_str]
        # 先頭19文字（YYYY-MM-DD HH:MM:SS）で前方一致
        prefix = str(time_str)[:19]
        for k, v in ts_index.items():
            if str(k)[:19] == prefix:
                return v
        return 0

    trades_anon = []
    for t in best.get("trades", []):
        entry_bar = _find_bar(str(t.get("entry_time", "")))
        exit_bar  = _find_bar(str(t.get("exit_time", "")))
        if entry_bar:
            trades_anon.append({**t, "entry_bar": entry_bar, "exit_bar": exit_bar})

    # トレードが含まれる範囲 ±40本に絞り込む（チャートを読みやすく）
    if trades_anon and candles:
        bars = [t["entry_bar"] for t in trades_anon] + [t["exit_bar"] for t in trades_anon if t.get("exit_bar")]
        lo = max(1, min(bars) - 40)
        hi = min(len(candles), max(bars) + 40)
        # 連番を再採番してスライス
        sliced = candles[lo - 1:hi]
        offset = lo - 1
        candles = [dict(c, time=c["time"] - offset) for c in sliced]
        trades_anon = [
            dict(t, entry_bar=t["entry_bar"] - offset,
                 exit_bar=(t["exit_bar"] - offset if t.get("exit_bar") else 0))
            for t in trades_anon
            if lo <= t["entry_bar"] <= hi
        ]

    # 銘柄ラベル: コードで並び替えた通し番号で匿名化
    all_symbols = sorted({r.get("symbol", "") for r in jp_done if r.get("symbol", "").endswith(".T")})
    sym_label = f"銘柄{chr(ord('A') + all_symbols.index(symbol))}" if symbol in all_symbols else "銘柄A"

    return {
        "candles": candles,
        "trades":  trades_anon,
        "symbol_label": sym_label,
        "strategy_label": "戦略A",
        "interval": interval,
        "score": round(best.get("score", 0), 2),
        "win_rate": round(best.get("win_rate", 0), 1),
        "daily_pnl_jpy": round(best.get("daily_pnl_jpy", 0)),
    }
