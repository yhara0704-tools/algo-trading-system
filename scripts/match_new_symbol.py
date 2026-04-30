#!/usr/bin/env python3
"""新規銘柄マッチャー — シンボル指定で「最短で最適手法に辿り着く」.

ユーザー指針 (2026-04-30 17:34):
> 新規で良さそうな銘柄を見つけた時に最短で最適手法に辿り着く可能性もあります。

使い方:
  python3 scripts/match_new_symbol.py 7974.T

処理フロー:
  1. 指定銘柄の 1m データを yfinance から 7 日分取得
  2. analyze_symbol() でプロファイル化 (vol/yoriten/observe_min/sd/...)
  3. categorize() でカテゴリ判定 (A-F)
  4. data/symbol_categories.json から該当カテゴリのテンプレ戦略を読み込み
  5. 推奨戦略リスト + 推奨パラメータ + 同じカテゴリの既存銘柄ベンチマーク表示

これにより「人間の感覚で銘柄選定 → 戦略あれこれ試す」のサイクルを
「アルゴが 60 秒で カテゴリ + 推奨戦略 + 期待 PnL レンジを提示」に置き換える。
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_symbol_open_profile import analyze_symbol  # noqa: E402
from scripts.categorize_symbols import categorize, CATEGORIES, _vol30  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", help="例: 7974.T (任天堂)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--cats", default="data/symbol_categories.json",
                    help="カテゴリ参照ファイル")
    args = ap.parse_args()

    sym = args.symbol.strip().upper()
    if "." not in sym:
        sym = f"{sym}.T"

    print(f"=== 新規銘柄マッチャー === {sym} (period={args.days}d)\n")

    # ── プロファイル取得 ────────────────────────────────────────────────
    print("[1/4] yfinance から 1m データ取得 + プロファイル分析中...")
    prof = analyze_symbol(sym, days=args.days)
    if prof is None:
        print(f"!! {sym} の 1m データ取得失敗。シンボル名/上場確認してください")
        sys.exit(1)
    print(f"  → {prof['n_days']} 日分のサンプル取得成功")

    # ── プロファイル表示 ──────────────────────────────────────────────
    vol30 = _vol30(prof)
    yoriten = prof.get("yoriten_pct", 0)
    abs_gap = prof.get("gap_stats", {}).get("abs_mean", 0)
    best = prof.get("best_observe_min") or {}

    print("\n[2/4] プロファイル特徴量:")
    print(f"  ボラ持続 vol@30min:    {vol30:>6.2f}%  (TP/SL 設計の基準)")
    print(f"  寄り天率 yoriten_pct:  {yoriten:>6.1f}%  (ショート/順張り判定の基準)")
    print(f"  ギャップ abs_mean:      {abs_gap:>6.2f}%  (寄り付きの動き量)")
    print(f"  ベスト観察期間:         {best.get('observe_min', '?')}min "
          f"(同方向一致率 {best.get('same_dir_pct', 0)}%, corr {best.get('corr', 0)})")

    # ── カテゴリ判定 ────────────────────────────────────────────────────
    cat_id = categorize(prof)
    cat_def = CATEGORIES.get(cat_id, {})
    print(f"\n[3/4] カテゴリ判定: {cat_id}")
    print(f"  → {cat_def.get('label', '?')}")

    # ── 推奨戦略 + 同カテゴリ既存銘柄 ────────────────────────────────
    cats_path = ROOT / args.cats
    if not cats_path.exists():
        print(f"\n!! {args.cats} がない。scripts/categorize_symbols.py を先に実行してください")
        sys.exit(1)
    cats = json.loads(cats_path.read_text())
    cat_full = cats["categories"].get(cat_id, {})
    rec_strats = cat_full.get("template_strategies", [])
    template_params = cat_full.get("template_params", {})
    members = cat_full.get("members", [])

    print(f"\n[4/4] 推奨戦略: {', '.join(rec_strats) if rec_strats else '(個別判断)'}")
    if template_params:
        print(f"\n  推奨パラメータ:")
        for strat, params in template_params.items():
            print(f"    {strat}:")
            for k, v in params.items():
                print(f"      {k}: {v}")

    if members:
        print(f"\n  同カテゴリの既存銘柄ベンチマーク ({len(members)} 銘柄):")
        for m in members[:5]:
            strats = ",".join(m.get("existing_strategies", [])[:3])
            print(f"    {m['symbol']:<7} {m.get('name', '')[:18]:<18} "
                  f"vol={m['vol30']:>5.2f}% 寄り天={m['yoriten']:>5.1f}% "
                  f"既存=[{strats}]")
        if len(members) > 5:
            print(f"    (+ {len(members)-5} 銘柄)")
    else:
        print(f"\n  ※ このカテゴリに既存銘柄はありません (新カテゴリ)")

    # ── 結論 + 次の一歩 ────────────────────────────────────────────────
    print("\n=== 結論 ===")
    if cat_id == "A_high_vol_short_pref":
        print("  寄り天 50%+ の高ボラ銘柄。MicroScalp_short が主力候補。")
        print("  本番投入前: 日次 30 取引以上見込まれるためリスク管理に注意")
    elif cat_id == "B_high_vol_trend_follow":
        print("  高ボラの順張り型。MicroScalp 両方向 + Breakout が候補。")
        print("  寄り直後 30 分の方向性が強いため、トレンドフォロー優位。")
    elif cat_id == "C_mid_vol_trend":
        print("  中ボラの順張り型。Scalp / MacdRci / Breakout が候補。")
        print("  MicroScalp は補助役 (TP=8/SL=4 相当) として活用可能。")
    elif cat_id == "D_mid_vol_neutral":
        print("  中ボラだが寄り天やや高め。Scalp 中心 + MacdRci で見極め。")
    elif cat_id == "E_low_vol_trend":
        print("  低ボラの順張り。MacdRci 専用が無難。MicroScalp は不適合。")
    elif cat_id == "F_low_vol_or_ng":
        print("  ボラ不足。MicroScalp 系 NG。MacdRci でも採算合うか要検証。")
        print("  → universe_active への新規追加は慎重に。")

    print(f"\n  次のステップ (本気で組み込むなら):")
    print(f"    1. python3 scripts/strategy_lab.py --symbol {sym} で戦略別 backtest")
    print(f"    2. 良ければ scripts/scan_full_pool.py の対象に追加")
    print(f"    3. WF 検証 (scripts/walkforward_strategy_compare.py)")
    print(f"    4. paper trading 14 日 → universe_active.json に正式登録")


if __name__ == "__main__":
    main()
