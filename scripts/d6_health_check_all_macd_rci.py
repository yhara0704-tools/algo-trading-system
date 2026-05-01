#!/usr/bin/env python3
"""D6d: 全 MacdRci 銘柄の 60日 実測 PnL を集計し、unhealthy 判定.

D6a で取得した data/d6_macd_rci_time_window_wr.json から各銘柄の
合計 PnL を再計算 (windows 別 PnL の合計)、universe 値 (oos_daily) と比較。

判定:
  - 60d 実測 PnL/日 が 0 円以下 → unhealthy (demote 推奨)
  - 60d 実測 PnL/日 が universe oos_daily の 30% 未満 → caution (universe 過大評価)

出力:
  data/d6_macd_rci_health_check.json
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))


def main() -> None:
    twr = json.load(open("data/d6_macd_rci_time_window_wr.json"))
    universe = json.load(open("data/universe_active.json"))
    macd_by_sym = {s["symbol"]: s for s in universe["symbols"] if s["strategy"] == "MacdRci"}

    print(f"=== D6d: 全 MacdRci 60日実測 health check ===\n")
    print(f"  {'symbol':10} {'days':>4} {'trades':>7} {'tot_pnl':>9} {'pnl/d':>7} {'oos_d':>6} {'ratio':>6} status")
    print(f"  " + "-" * 75)

    rows = []
    unhealthy = []
    caution = []

    for sym, r in twr["results"].items():
        n_days = r["n_days"]
        total_trades = r["total_trades"]
        windows = r["by_window"]
        total_pnl = sum(w.get("pnl", 0) for w in windows.values())
        pnl_per_day = total_pnl / n_days if n_days > 0 else 0
        oos_daily = float(macd_by_sym.get(sym, {}).get("oos_daily", 0) or 0)
        # observation_only かどうか取得
        obs_only = bool(macd_by_sym.get(sym, {}).get("observation_only", False))
        ratio = pnl_per_day / oos_daily if oos_daily > 0 else 0
        # 判定
        status = "OK"
        if pnl_per_day < 0:
            status = "UNHEALTHY"
            if not obs_only:
                unhealthy.append({
                    "symbol": sym, "pnl_per_day": pnl_per_day,
                    "oos_daily": oos_daily, "n_days": n_days,
                })
        elif oos_daily > 200 and ratio < 0.3:
            status = "OVERESTIMATE"
            if not obs_only:
                caution.append({
                    "symbol": sym, "pnl_per_day": pnl_per_day,
                    "oos_daily": oos_daily, "ratio": ratio,
                })
        if obs_only:
            status += " (obs_only)"
        print(f"  {sym:10} {n_days:>4} {total_trades:>7} {total_pnl:>+9.0f} "
              f"{pnl_per_day:>+7.0f} {oos_daily:>+6.0f} {ratio:>6.2f} {status}")

        rows.append({
            "symbol": sym,
            "n_days": n_days,
            "trades": total_trades,
            "total_pnl_60d": total_pnl,
            "pnl_per_day": pnl_per_day,
            "oos_daily": oos_daily,
            "ratio": ratio,
            "status": status,
        })

    print()
    print(f"=== UNHEALTHY (実測 PnL/日 < 0、demote 推奨) ===")
    for u in unhealthy:
        print(f"  {u['symbol']}: {u['pnl_per_day']:+.0f} 円/日 (oos_daily={u['oos_daily']:.0f})")

    print(f"\n=== OVERESTIMATE (oos_daily に対して実測 < 30%、要観察) ===")
    for c in caution:
        print(f"  {c['symbol']}: {c['pnl_per_day']:+.0f}/{c['oos_daily']:.0f} 円/日 (ratio={c['ratio']:.2f})")

    out_path = Path("data/d6_macd_rci_health_check.json")
    out_path.write_text(json.dumps({
        "generated_at": datetime.now(JST).isoformat(),
        "rows": rows,
        "unhealthy": unhealthy,
        "caution": caution,
        "n_unhealthy": len(unhealthy),
        "n_caution": len(caution),
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    main()
