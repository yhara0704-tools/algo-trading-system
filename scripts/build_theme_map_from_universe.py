#!/usr/bin/env python3
"""universe_active.json の銘柄を株探で逆引きして theme_map.json を補完する.

ユーザー指針 (2026-04-30 17:52):
> A〜Fのカテゴライズされた中身の銘柄は動的に変化するよ。
> やっぱりずっと強いテーマもあれば、すぐに廃れるテーマもあるからね。

→ 「テーマ × カテゴリ」のクロス分析を機能させるには、universe 銘柄が
  どのテーマに属するかを theme_map.json に網羅的に登録する必要がある。

  既存 theme_map.json は手動登録 6 テーマ + 8 銘柄のみで、
  universe_active 35 銘柄をほとんどカバーしていない。

このスクリプトで universe を株探で順次調査し、テーマを自動収集する。
出力は data/theme_map.json (既存テーマに追記、重複排除、reason="auto_universe_backfill")。
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lookup_kabutan import fetch  # noqa: E402

UNIVERSE_PATH = ROOT / "data/universe_active.json"
THEME_MAP_PATH = ROOT / "data/theme_map.json"
SECTOR_MAP_PATH = ROOT / "data/sector_map.json"


def load_universe_symbols() -> list[str]:
    if not UNIVERSE_PATH.exists():
        return []
    data = json.loads(UNIVERSE_PATH.read_text())
    pairs = data.get("pairs", []) or data.get("symbols", [])
    syms: list[str] = []
    seen: set[str] = set()
    for p in pairs:
        if isinstance(p, dict):
            s = p.get("symbol")
        else:
            s = p
        if s and s not in seen:
            seen.add(s)
            syms.append(s)
    return syms


def load_industry_map() -> dict[str, str]:
    """sector_map.json から symbol -> industry name を構築."""
    if not SECTOR_MAP_PATH.exists():
        return {}
    data = json.loads(SECTOR_MAP_PATH.read_text())
    out: dict[str, str] = {}
    for sector_name, payload in data.items():
        if sector_name.startswith("_") or not isinstance(payload, dict):
            continue
        for entry in payload.get("domestic", []):
            if isinstance(entry, dict):
                sym = entry.get("symbol", "")
                if sym:
                    out[sym] = sector_name
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleep", type=float, default=1.5)
    ap.add_argument("--limit", type=int, default=0, help="調査銘柄数の上限 (0=全件)")
    ap.add_argument("--symbols", nargs="*", help="universe 以外の追加銘柄 (例 7974.T)")
    ap.add_argument("--dry-run", action="store_true", help="theme_map.json を上書きしない")
    args = ap.parse_args()

    universe_syms = load_universe_symbols()
    extra_syms = args.symbols or []
    targets = list(dict.fromkeys(universe_syms + extra_syms))
    if args.limit:
        targets = targets[:args.limit]

    print(f"=== universe テーマ逆引き ({len(targets)} 銘柄, sleep={args.sleep}s) ===\n")

    industry_map = load_industry_map()

    # 既存 theme_map.json を読込
    if THEME_MAP_PATH.exists():
        theme_data = json.loads(THEME_MAP_PATH.read_text())
    else:
        theme_data = {"_doc": "auto-generated", "themes": {}}
    themes_dict: dict[str, dict] = theme_data.setdefault("themes", {})

    # 銘柄 → テーマ列を取得
    sym_themes: dict[str, list[str]] = {}
    sym_names: dict[str, str] = {}
    for i, sym in enumerate(targets, 1):
        try:
            info = fetch(sym)
        except Exception as e:
            print(f"  [{i}/{len(targets)}] {sym}: ERROR {e}")
            continue
        sym_themes[sym] = info.get("themes", []) or []
        sym_names[sym] = info.get("name") or ""
        themes_str = ", ".join(sym_themes[sym][:5]) if sym_themes[sym] else "(no themes)"
        print(f"  [{i}/{len(targets)}] {sym} {sym_names[sym][:18]:<18} → {themes_str}")
        time.sleep(args.sleep)

    # ── 銘柄を該当テーマに追加 ────────────────────────────────────────
    today = datetime.now().strftime("%Y-%m-%d")
    n_added = 0
    n_themes_new = 0
    for sym, theme_names in sym_themes.items():
        for tn in theme_names:
            if tn not in themes_dict:
                themes_dict[tn] = {
                    "kabutan_url": f"https://kabutan.jp/themes/?theme={tn}",
                    "constituents_total": None,
                    "tracked": [],
                }
                n_themes_new += 1
            tracked = themes_dict[tn].setdefault("tracked", [])
            existing_syms = {e.get("symbol") for e in tracked if isinstance(e, dict)}
            if sym in existing_syms:
                continue
            tracked.append({
                "symbol": sym,
                "name": sym_names.get(sym, ""),
                "kabutan_industry": industry_map.get(sym, ""),
                "added_at": today,
                "reason": "auto_universe_backfill",
            })
            n_added += 1

    theme_data["updated_at"] = datetime.now().isoformat() + "+09:00"

    # ── 出力 ────────────────────────────────────────────────────────
    print(f"\n=== 集計 ===")
    print(f"  銘柄調査: {len(sym_themes)}")
    print(f"  新規テーマ追加: {n_themes_new}")
    print(f"  銘柄→テーマ ひも付け追加: {n_added}")
    print(f"  theme_map.json 内テーマ総数: {len(themes_dict)}")

    # universe カバレッジ
    universe_covered = {
        s for s in universe_syms if any(s in {e.get("symbol") for e in t.get("tracked", [])}
                                         for t in themes_dict.values())
    }
    print(f"  universe カバレッジ: {len(universe_covered)}/{len(universe_syms)} 銘柄")

    if args.dry_run:
        print("\n  --dry-run のため theme_map.json は未更新")
        return
    THEME_MAP_PATH.write_text(
        json.dumps(theme_data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\n=> 更新: {THEME_MAP_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
