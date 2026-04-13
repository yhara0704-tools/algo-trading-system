"""TOB事例 × EDINET大量保有報告書 共通シグナル分析.

過去のTOB公表前に、大量保有報告書・訂正報告書にどのような動きがあったかを
EDINET APIで遡って調査し、共通パターンを洗い出す。
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
from datetime import datetime, timedelta

import requests

EDINET_API_KEY = "3cc4c01281a54c8da278a88dfea35344"
BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"
OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "tob_research"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 証券コード → issuerEdinetCode 対応表 ─────────────────────────────────────
SEC_TO_EDINET = {
    "4581": "E25678", "9783": "E04939", "2899": "E00469",
    "9058": "E04208", "6640": "E01876", "4384": "E33966",
    "4917": "E01027", "6197": "E04878", "9749": "E04810",
    "3978": "E05372", "7518": "E04966", "6789": "E02054",
    "3294": "E30124", "8155": "E02677", "4726": "E05037",
    "9613": "E04911", "7451": "E02558", "1884": "E00067",
    "5481": "E01243", "9787": "E04874", "3391": "E03464",
    "7163": "E26990", "8289": "E03132",
}

# ── 調査対象TOB事例 ──────────────────────────────────────────────────────────
TOB_CASES = [
    # MBO
    ("4581", "大正製薬HD",       "2023-11-24", "MBO"),
    ("9783", "ベネッセHD",       "2024-01-30", "MBO"),
    ("2899", "永谷園HD",         "2024-06-03", "MBO"),
    ("9058", "トランコム",       "2024-09-17", "MBO"),
    ("6640", "I-PEX",            "2024-11-07", "MBO"),
    ("4384", "ラクスル",         "2025-12-11", "MBO"),
    ("4917", "マンダム",         "2025-09-26", "MBO"),
    ("6197", "ソラスト",         "2026-03-24", "MBO"),
    # ファンド/第三者買収
    ("9749", "富士ソフト",       "2024-08-01", "ファンド買収"),
    ("3978", "マクロミル",       "2024-11-14", "ファンド買収"),
    ("7518", "ネットワンシステムズ", "2024-11-06", "第三者買収"),
    ("6789", "ローランドDG",     "2024-05-01", "ファンド買収"),
    ("3294", "イーグランド",     "2026-03-31", "第三者買収"),
    # 親子上場解消
    ("8155", "三益半導体工業",   "2024-04-25", "親子上場解消"),
    ("4726", "SBテクノロジー",   "2024-04-25", "親子上場解消"),
    ("9613", "NTTデータ",        "2025-05-08", "親子上場解消"),
    ("7451", "三菱食品",         "2025-05-08", "親子上場解消"),
    ("1884", "日本道路",         "2025-05-14", "親子上場解消"),
    ("5481", "山陽特殊製鋼",     "2025-01-15", "親子上場解消"),
    ("9787", "イオンディライト", "2025-02-28", "親子上場解消"),
    ("3391", "ツルハHD",         "2025-03-01", "親子上場解消"),
    ("7163", "住信SBIネット銀行","2025-05-29", "親子上場解消"),
    # 身売り観測
    ("8289", "Olympicグループ",  "2026-04-08", "身売り観測"),
]

# ── APIキャッシュ（同じ日を二度叩かない） ─────────────────────────────────────
_doc_cache: dict[str, list[dict]] = {}
CACHE_FILE = OUT_DIR / "_api_cache.json"


def _load_cache():
    global _doc_cache
    if CACHE_FILE.exists():
        try:
            _doc_cache = json.loads(CACHE_FILE.read_text())
        except Exception:
            _doc_cache = {}


def _save_cache():
    CACHE_FILE.write_text(json.dumps(_doc_cache, ensure_ascii=False))


def fetch_documents(date_str: str) -> list[dict]:
    """指定日のEDINET提出書類一覧を取得する（キャッシュあり）。"""
    if date_str in _doc_cache:
        return _doc_cache[date_str]

    url = f"{BASE_URL}/documents.json"
    params = {
        "date": date_str,
        "type": 2,
        "Subscription-Key": EDINET_API_KEY,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", [])
    _doc_cache[date_str] = results
    time.sleep(0.5)  # レート制限対策
    return results


def search_large_holdings(sec_code: str, start_date: str, end_date: str) -> list[dict]:
    """指定期間の大量保有報告書（ordinanceCode=060）を日次で検索する。
    issuerEdinetCode で発行体を照合する。
    """
    issuer_edinet = SEC_TO_EDINET.get(sec_code)
    if not issuer_edinet:
        print(f"  [WARN] {sec_code} のEDINETコードが不明")
        return []

    results = []
    current = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    days_total = (end - current).days
    checked = 0

    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        # 土日はスキップ（EDINET提出なし）
        if current.weekday() < 5:
            try:
                docs = fetch_documents(date_str)
                for doc in docs:
                    if doc.get("ordinanceCode") != "060":
                        continue
                    if doc.get("issuerEdinetCode") == issuer_edinet:
                        results.append({
                            "date": date_str,
                            "docID": doc.get("docID"),
                            "docDescription": doc.get("docDescription", ""),
                            "filerName": doc.get("filerName", ""),
                            "issuerEdinetCode": doc.get("issuerEdinetCode", ""),
                            "amendmentFlag": doc.get("amendmentFlag", "0"),
                            "parentDocID": doc.get("parentDocID", ""),
                            "formCode": doc.get("formCode", ""),
                        })
            except Exception as e:
                print(f"  [WARN] {date_str}: {e}")
            checked += 1
            if checked % 30 == 0:
                print(f"    ... {checked}日チェック済み / 報告書 {len(results)}件発見")
                _save_cache()  # 中間キャッシュ保存
        current += timedelta(days=1)

    return results


def analyze_case(sec_code: str, name: str, tob_date: str, tob_type: str,
                 lookback_months: int = 6) -> dict:
    """1つのTOB事例について、公表前の大量保有報告書を調査する。"""
    end = datetime.strptime(tob_date, "%Y-%m-%d")
    start = end - timedelta(days=lookback_months * 30)

    print(f"\n{'='*60}")
    print(f"調査: {name} ({sec_code}) — TOB公表 {tob_date} [{tob_type}]")
    print(f"期間: {start.strftime('%Y-%m-%d')} 〜 {tob_date}")
    print(f"{'='*60}")

    filings = search_large_holdings(
        sec_code,
        start.strftime("%Y-%m-%d"),
        tob_date,
    )

    # 集計
    total = len(filings)
    amendments = [f for f in filings
                  if f.get("amendmentFlag") == "1"
                  or "訂正" in f.get("docDescription", "")
                  or (f.get("parentDocID") and f.get("parentDocID") != "")]
    changes = [f for f in filings if "変更" in f.get("docDescription", "")]
    new_reports = [f for f in filings if f.get("docDescription") == "大量保有報告書"]
    filers = list(set(f["filerName"] for f in filings))

    for f in filings:
        is_amend = (f.get("amendmentFlag") == "1"
                    or "訂正" in f.get("docDescription", "")
                    or (f.get("parentDocID") and f.get("parentDocID") != ""))
        tag = "[訂正]" if is_amend else ""
        print(f"  {f['date']} {tag} {f['filerName']} — {f['docDescription']}")

    # 訂正の対象が「古い」報告書かどうか（TOB前6ヶ月より前の元報告書を訂正）
    has_old_amendment = any(
        f.get("parentDocID") and f.get("parentDocID") != ""
        for f in amendments
    )

    result = {
        "sec_code": sec_code,
        "name": name,
        "tob_date": tob_date,
        "tob_type": tob_type,
        "lookback_months": lookback_months,
        "total_filings": total,
        "amendment_count": len(amendments),
        "change_report_count": len(changes),
        "new_report_count": len(new_reports),
        "unique_filers": filers,
        "filer_count": len(filers),
        "has_amendment": len(amendments) > 0,
        "has_old_amendment": has_old_amendment,
        "filings": filings,
    }

    print(f"\n  [結果] 報告書計 {total}件 (新規{len(new_reports)} 変更{len(changes)} 訂正{len(amendments)})")
    print(f"  提出者: {', '.join(filers) if filers else 'なし'}")

    return result


def main():
    _load_cache()
    all_results = []

    for sec_code, name, tob_date, tob_type in TOB_CASES:
        result = analyze_case(sec_code, name, tob_date, tob_type)
        all_results.append(result)

        # 中間保存
        out_path = OUT_DIR / "tob_edinet_analysis.json"
        out_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=2))
        _save_cache()

    # ── 全体集計 ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("全体集計")
    print("=" * 60)

    total_cases = len(all_results)
    with_filings = [r for r in all_results if r["total_filings"] > 0]
    with_amendments = [r for r in all_results if r["has_amendment"]]
    with_multi_filers = [r for r in all_results if r["filer_count"] >= 2]

    print(f"調査事例数: {total_cases}")
    print(f"大量保有報告書あり: {len(with_filings)}/{total_cases} ({len(with_filings)/total_cases*100:.0f}%)")
    print(f"訂正報告書あり: {len(with_amendments)}/{total_cases} ({len(with_amendments)/total_cases*100:.0f}%)")
    print(f"複数提出者: {len(with_multi_filers)}/{total_cases} ({len(with_multi_filers)/total_cases*100:.0f}%)")

    # 類型別集計
    for tob_type in ["MBO", "ファンド買収", "第三者買収", "親子上場解消", "身売り観測"]:
        subset = [r for r in all_results if r["tob_type"] == tob_type]
        if not subset:
            continue
        n = len(subset)
        n_filings = sum(1 for r in subset if r["total_filings"] > 0)
        n_amend = sum(1 for r in subset if r["has_amendment"])
        avg_filings = sum(r["total_filings"] for r in subset) / n
        print(f"\n  [{tob_type}] {n}件")
        print(f"    報告書あり: {n_filings}/{n}  平均件数: {avg_filings:.1f}")
        print(f"    訂正あり: {n_amend}/{n}")

    # サマリー保存
    summary = {
        "generated_at": datetime.now().isoformat(),
        "total_cases": total_cases,
        "cases_with_filings": len(with_filings),
        "cases_with_amendments": len(with_amendments),
        "cases_with_multi_filers": len(with_multi_filers),
        "results": all_results,
    }
    out_path = OUT_DIR / "tob_edinet_analysis.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n結果を保存: {out_path}")


if __name__ == "__main__":
    main()
