"""株探(kabutan.jp) の銘柄基本情報ページから業種・概要を一括取得する。

sector_map.json への銘柄追加時、東証業種を株探で公式確認するためのヘルパー。

実行例:
    .venv/bin/python3 scripts/lookup_kabutan.py 247A 6433 6629 6330 5381 5243 4082
    .venv/bin/python3 scripts/lookup_kabutan.py --from-sector-map  # 既存 sector_map 全件
    .venv/bin/python3 scripts/lookup_kabutan.py 247A --json

注意:
- 株探は公開ページなのでスクレイピング可能だが、過剰アクセスは避ける（本実装は 1 req/sec）。
- 取得項目: 銘柄名・東証業種・概要文。テーマは株探プレミアム限定の場合があり対象外。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
SECTOR_MAP_PATH = ROOT / "data" / "sector_map.json"

KABUTAN_URL = "https://kabutan.jp/stock/?code={code}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

INDUSTRY_PATTERN = re.compile(
    r"<th[^>]*>業種</th>\s*<td[^>]*>(?:<a[^>]*>)?([^<]+?)(?:</a>)?</td>",
    re.DOTALL,
)
SUMMARY_PATTERN = re.compile(
    r"<th[^>]*>概要</th>\s*<td[^>]*>([^<]+?)</td>",
    re.DOTALL,
)
NAME_PATTERN = re.compile(
    r'<title>([^<\|【]+?)(?:【|\|)',
    re.DOTALL,
)
# 銘柄ページ「テーマ」セル全体（<th scope='row'>テーマ</th>... </td>）
THEME_CELL_PATTERN = re.compile(
    r"<th[^>]*>テーマ</th>\s*<td[^>]*>(.*?)</td>",
    re.DOTALL,
)
# テーマセル内の個別リンク
THEME_LINK_PATTERN = re.compile(r'/themes/\?theme=([^"\'>]+)')


def _strip_code(symbol: str) -> str:
    """`247A.T` / `247A` / `247A.JP` -> `247A`"""
    return symbol.replace(".T", "").replace(".JP", "").strip()


def fetch(code: str) -> dict[str, Optional[str]]:
    url = KABUTAN_URL.format(code=_strip_code(code))
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    name_m = NAME_PATTERN.search(html)
    industry_m = INDUSTRY_PATTERN.search(html)
    summary_m = SUMMARY_PATTERN.search(html)
    themes: list[str] = []
    theme_cell_m = THEME_CELL_PATTERN.search(html)
    if theme_cell_m:
        seen: set[str] = set()
        for raw in THEME_LINK_PATTERN.findall(theme_cell_m.group(1)):
            if raw in seen:
                continue
            seen.add(raw)
            try:
                themes.append(urllib.parse.unquote(raw))
            except Exception:
                themes.append(raw)

    return {
        "code": _strip_code(code),
        "name": name_m.group(1).strip() if name_m else None,
        "industry": industry_m.group(1).strip() if industry_m else None,
        "themes": themes,
        "summary": summary_m.group(1).strip() if summary_m else None,
        "url": url,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("codes", nargs="*", help="銘柄コード (例: 247A 6433.T 9984)")
    p.add_argument(
        "--from-sector-map",
        action="store_true",
        help=f"{SECTOR_MAP_PATH.relative_to(ROOT)} の domestic 全銘柄を一括検証",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="連続アクセス間のスリープ秒 (default: 1.0)",
    )
    p.add_argument("--json", action="store_true", help="JSON で出力 (デフォルトは表)")
    p.add_argument(
        "--diff-only",
        action="store_true",
        help="--from-sector-map 時、sector_map の所属セクター名と株探業種が異なるものだけ表示",
    )
    return p.parse_args()


def _collect_from_sector_map() -> list[tuple[str, str]]:
    """[(symbol, current_sector_name), ...] を返す.

    NOTE: `_doc` のようなメタ string キーはここで弾く
    (2026-04-30 daemon クラッシュの再発防止)
    """
    data = json.loads(SECTOR_MAP_PATH.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for sector_name, payload in data.items():
        if sector_name.startswith("_") or not isinstance(payload, dict):
            continue
        for entry in payload.get("domestic", []):
            sym = entry.get("symbol", "") if isinstance(entry, dict) else ""
            if sym and sym.endswith(".T"):
                out.append((sym, sector_name))
    return out


def main() -> int:
    args = parse_args()

    targets: list[tuple[str, Optional[str]]]
    if args.from_sector_map:
        targets = [(sym, sec) for sym, sec in _collect_from_sector_map()]
    else:
        if not args.codes:
            print("ERROR: codes か --from-sector-map のどちらかが必要", file=sys.stderr)
            return 2
        targets = [(c, None) for c in args.codes]

    rows: list[dict] = []
    for i, (sym, current_sector) in enumerate(targets):
        try:
            info = fetch(sym)
        except Exception as e:
            info = {"code": sym, "name": None, "industry": None, "summary": None, "url": None, "error": str(e)}
        info["current_sector_in_map"] = current_sector
        rows.append(info)
        if i + 1 < len(targets):
            time.sleep(args.sleep)

    if args.diff_only:
        rows = [r for r in rows if r.get("current_sector_in_map") and r.get("industry") and r["industry"] not in r["current_sector_in_map"]]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(f"=== 株探 業種・テーマルックアップ (n={len(rows)}) ===")
    for r in rows:
        code = r.get("code") or "?"
        name = (r.get("name") or "?")
        industry = r.get("industry") or "?"
        cur = r.get("current_sector_in_map") or "-"
        summary = (r.get("summary") or "")
        themes = r.get("themes") or []
        marker = ""
        if r.get("current_sector_in_map") and r.get("industry"):
            if r["industry"] not in r["current_sector_in_map"]:
                marker = " ⚠業種乖離"
        print(f"\n[{code}] {name}{marker}")
        print(f"  業種(株探): {industry}    現sector_map: {cur}")
        if themes:
            head = themes[:12]
            extra = f" 他+{len(themes) - 12}" if len(themes) > 12 else ""
            print(f"  テーマ({len(themes)}): {', '.join(head)}{extra}")
        if summary:
            print(f"  概要: {summary[:120]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
