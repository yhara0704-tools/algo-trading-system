"""セクター内相対強度スキャナー.

各セクターの国内株・米国proxyの任意期間リターンを計算し、
リーダー/出遅れ銘柄を検出してPushover通知する。

実行:
    python3 scripts/sector_scanner.py              # 全セクター
    python3 scripts/sector_scanner.py --sector 半導体  # 特定セクター
    python3 scripts/sector_scanner.py --days 3,5,10    # 期間指定
"""
from __future__ import annotations
import argparse, asyncio, json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import yfinance as yf

SECTOR_MAP  = Path(__file__).parent.parent / "data" / "sector_map.json"
PUSHOVER_USER  = os.getenv("PUSHOVER_USER_KEY", "")
PUSHOVER_TOKEN = os.getenv("PUSHOVER_API_TOKEN", "")

JST = timezone(timedelta(hours=9))
DEFAULT_PERIODS = [1, 3, 5, 10, 20]  # 日数


# ── 価格取得 ──────────────────────────────────────────────────────────────────
def fetch_returns(symbols: list[str], max_days: int = 25) -> dict[str, dict[int, float]]:
    """yfinanceで各銘柄のN日リターンを取得."""
    results: dict[str, dict[int, float]] = {}
    tickers = yf.Tickers(" ".join(symbols))
    hist = tickers.history(period=f"{max_days + 5}d", auto_adjust=True)["Close"]

    if isinstance(hist, pd.Series):
        hist = hist.to_frame()

    for sym in symbols:
        col = sym
        if col not in hist.columns:
            continue
        s = hist[col].dropna()
        if len(s) < 2:
            continue
        ret = {}
        for d in DEFAULT_PERIODS:
            if len(s) > d:
                ret[d] = (s.iloc[-1] / s.iloc[-1 - d] - 1) * 100
        results[sym] = ret
    return results


# ── 出遅れ検出 ────────────────────────────────────────────────────────────────
def detect_laggards(
    sector_name: str,
    domestic: list[dict],
    us_proxy: list[dict],
    returns: dict[str, dict[int, float]],
    period: int = 5,
) -> dict:
    """国内株同士でリーダー/出遅れを判定。米国proxyは参考表示のみ。"""
    def make_rows(stocks: list[dict]) -> list[dict]:
        rows = []
        for s in stocks:
            ret = returns.get(s["symbol"], {})
            if not ret:
                continue
            rows.append({
                "symbol": s["symbol"], "name": s["name"], "role": s["role"],
                **{f"ret_{d}d": round(ret.get(d, float("nan")), 2) for d in DEFAULT_PERIODS}
            })
        return rows

    domestic_rows = make_rows(domestic)
    us_rows       = make_rows(us_proxy)

    if not domestic_rows:
        return {}

    df = pd.DataFrame(domestic_rows)
    key = f"ret_{period}d"
    if key not in df.columns:
        return {}

    df = df.sort_values(key, ascending=False).reset_index(drop=True)
    df["rank"] = df[key].rank(ascending=False)

    # リーダー: 上位1/3、出遅れ: 下位1/3（国内株のみで判定）
    n = len(df)
    leaders  = df[df["rank"] <= max(1, n // 3)]
    laggards = df[df["rank"] > n - max(1, n // 3)]

    return {
        "sector":   sector_name,
        "period":   period,
        "domestic": df.to_dict("records"),
        "us_proxy": us_rows,
        "leaders":  leaders.to_dict("records"),
        "laggards": laggards.to_dict("records"),
    }


# ── レポート生成 ──────────────────────────────────────────────────────────────
def format_report(sectors_data: list[dict], period: int) -> str:
    lines = [
        f"📊 セクター相対強度レポート ({period}日)",
        f"{datetime.now(JST).strftime('%Y-%m-%d %H:%M')} JST",
        "",
    ]
    key = f"ret_{period}d"
    leader_syms  = set()
    laggard_syms = set()

    for sd in sectors_data:
        if not sd:
            continue
        leader_syms  |= {r["symbol"] for r in sd["leaders"]}
        laggard_syms |= {r["symbol"] for r in sd["laggards"]}

        lines.append(f"【{sd['sector']}】")

        # 国内株（メイン比較）
        for r in sd["domestic"]:
            ret  = r.get(key, float("nan"))
            bar  = "▲" if ret > 0 else "▼"
            mark = "🔥" if r["symbol"] in leader_syms else ("💤" if r["symbol"] in laggard_syms else "  ")
            lines.append(f"  {mark}{r['name'][:10]:10s} {bar}{abs(ret):5.1f}%  ({r['symbol']})")

        # 米国proxy（参考）
        if sd["us_proxy"]:
            lines.append("  ─ 米国参考 ─")
            for r in sorted(sd["us_proxy"], key=lambda x: -x.get(key, -999)):
                ret = r.get(key, float("nan"))
                if ret != ret:
                    continue
                bar = "▲" if ret > 0 else "▼"
                lines.append(f"    {r['name'][:10]:10s} {bar}{abs(ret):5.1f}%  ({r['symbol']})")

        # 出遅れ候補のハイライト
        if sd["laggards"] and sd["leaders"]:
            leader_name  = sd["leaders"][0]["name"]
            laggard_name = sd["laggards"][0]["name"]
            gap = sd["leaders"][0].get(key, 0) - sd["laggards"][0].get(key, 0)
            lines.append(f"  → 出遅れ候補: {laggard_name}（{leader_name}比 {gap:.1f}%乖離）")

        lines.append("")
    return "\n".join(lines)


def push(title: str, msg: str) -> None:
    import requests
    requests.post("https://api.pushover.net/1/messages.json", data={
        "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
        "title": title, "message": msg
    }, timeout=10)


# ── メイン ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sector", default=None, help="特定セクター名（省略で全セクター）")
    parser.add_argument("--days",   default="5",  help="基準期間（日数）例: 5 または 3,5,10")
    parser.add_argument("--notify", action="store_true", help="Pushover通知を送る")
    args = parser.parse_args()

    sector_map = json.loads(SECTOR_MAP.read_text(encoding="utf-8"))
    period = int(args.days.split(",")[0])

    # 対象セクター絞り込み
    targets = {k: v for k, v in sector_map.items()
               if args.sector is None or args.sector in k}

    if not targets:
        print(f"セクター '{args.sector}' が見つかりません")
        return

    # 全銘柄リスト収集
    all_stocks: dict[str, dict] = {}
    for sec_name, sec_data in targets.items():
        for s in sec_data.get("domestic", []) + sec_data.get("us_proxy", []):
            all_stocks[s["symbol"]] = s

    print(f"価格取得中: {len(all_stocks)}銘柄...")
    returns = fetch_returns(list(all_stocks.keys()))

    # セクターごとに分析（国内株メイン、米国proxy参考）
    sectors_data = []
    for sec_name, sec_data in targets.items():
        sd = detect_laggards(
            sec_name,
            sec_data.get("domestic", []),
            sec_data.get("us_proxy", []),
            returns, period,
        )
        if sd:
            sectors_data.append(sd)

    # レポート出力
    report = format_report(sectors_data, period)
    print(report)

    # 複数期間の比較テーブル
    print("\n=== 全銘柄 × 複数期間リターン ===")
    for sec_name, sec_data in targets.items():
        print(f"\n【{sec_name}】")
        header = f"{'銘柄':12s} {'役割':8s} " + " ".join(f"{d:>6}d" for d in DEFAULT_PERIODS)
        print(header)
        print("-" * len(header))
        domestic_stocks = sec_data.get("domestic", [])
        us_stocks       = sec_data.get("us_proxy", [])
        rows = []
        print("  [国内株]")
        for s in domestic_stocks:
            ret = returns.get(s["symbol"], {})
            rets = [ret.get(d, float("nan")) for d in DEFAULT_PERIODS]
            if all(r != r for r in rets):  # all NaN
                continue
            rows.append((s, rets))
        rows.sort(key=lambda x: x[1][DEFAULT_PERIODS.index(period)] if x[1][DEFAULT_PERIODS.index(period)] == x[1][DEFAULT_PERIODS.index(period)] else -999, reverse=True)
        for s, rets in rows:
            ret_str = " ".join(f"{r:+6.1f}%" if r == r else "   N/A " for r in rets)
            print(f"  {s['name'][:12]:12s} {s['role']:8s} {ret_str}")

        # 米国proxy（参考）
        us_rows = []
        for s in us_stocks:
            ret = returns.get(s["symbol"], {})
            rets = [ret.get(d, float("nan")) for d in DEFAULT_PERIODS]
            if all(r != r for r in rets):
                continue
            us_rows.append((s, rets))
        if us_rows:
            us_rows.sort(key=lambda x: x[1][DEFAULT_PERIODS.index(period)] if x[1][DEFAULT_PERIODS.index(period)] == x[1][DEFAULT_PERIODS.index(period)] else -999, reverse=True)
            print("  [米国参考]")
            for s, rets in us_rows:
                ret_str = " ".join(f"{r:+6.1f}%" if r == r else "   N/A " for r in rets)
                print(f"  {s['name'][:12]:12s} {s['role']:8s} {ret_str}")

    if args.notify:
        push("📊 セクター相対強度", report[:1000])
        print("\nPushover通知送信完了")


if __name__ == "__main__":
    env = Path(__file__).parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    main()
