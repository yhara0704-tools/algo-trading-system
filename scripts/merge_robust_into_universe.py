#!/usr/bin/env python3
"""`macd_rci_params.json` の Robust 集合を `universe_active.json` に反映する.

背景:
- `weekly_universe_rotation.py` は `strategy_fit_map.json` の `best_strategy` と
  `is_oos_pass=True` を要件として universe を生成するため、
  `macd_rci_params.json` で robust=True の MacdRci パラメータが出ていても
  `is_oos_pass=False` や別戦略が best のケースで取りこぼす（例: 8306.T / 3382.T）。
- 本スクリプトは **macd_rci_params.json の robust=true を唯一の根拠**として
  MacdRci を universe に上書き追加し、日次で universe_active.json を自己回復させる。

挙動:
- Robust ∖ Universe: 追加（strategy=MacdRci、oos_daily は macd_rci_params の値）
- Robust ∩ Universe で strategy ≠ MacdRci または macd_rci の oos_daily が
  universe の値より大きい場合: strategy=MacdRci に上書きし、oos_daily も更新
- Universe のその他エントリは **据え置き**（Robust に無いものを削除しない）
- 併せて ``data/universe_robust_merge_latest.json`` に差分レポートを保存

出力:
- data/universe_active.json（上書き；実行前のバックアップを同ディレクトリに保存）
- data/universe_robust_merge_latest.json（差分レポート）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
MACD_PARAMS = DATA_DIR / "macd_rci_params.json"
UNIVERSE_ACTIVE = DATA_DIR / "universe_active.json"
MERGE_REPORT = DATA_DIR / "universe_robust_merge_latest.json"
NIGHTLY_WF_LATEST = DATA_DIR / "nightly_walkforward_latest.json"
OBSERVATION_PAIRS = DATA_DIR / "universe_observation_pairs.json"
JST = timezone(timedelta(hours=9))


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(f".backup_{ts}.json")
    shutil.copyfile(path, backup)
    return backup


def _robust_symbols(params: dict) -> dict[str, dict]:
    return {
        s: p for s, p in (params or {}).items()
        if isinstance(p, dict) and p.get("robust")
    }


def _symbol_row_from_macd(symbol: str, p: dict, name: str = "") -> dict:
    # universe_active.json の symbols[] と同じスキーマに合わせる
    return {
        "symbol": symbol,
        "name": name or symbol,
        "strategy": "MacdRci",
        "score": round(float(p.get("oos_daily") or 0.0) * 0.7 + float(p.get("is_daily") or 0.0) * 0.3 + 200.0, 1),
        "is_daily": round(float(p.get("is_daily") or 0.0), 1),
        "oos_daily": round(float(p.get("oos_daily") or 0.0), 1),
        "is_pf": round(float(p.get("is_pf") or 0.0), 3),
        "is_trades": int(p.get("is_trades") or 0),
        "robust": True,
        "is_oos_pass": bool(p.get("is_oos_pass", False)),
        "calmar": round(float(p.get("calmar") or 0.0), 3),
        "source": "macd_rci_params.robust_merge",
        # N7 (2026-05-06): 新規 MacdRci 行に lot_multiplier 既定値 1.0 を付与。
        # 既存値は後段の merge ループで保護される。
        "lot_multiplier": 1.0,
        "force_paper": False,
    }


# N7 (2026-05-06): 既存 universe entry のうち、merge_robust の metric 更新時にも
# 引き継ぐべきフィールド一覧。N1/N4 で投入した銘柄の lot_multiplier、force_paper、
# caveat、expected_value_per_day、検証メタ (n1_validation, n4_validation) などは
# 朝 cron で自動消失すると Phase D gate 試算が崩れるため、必ず保護する。
PROTECTED_FIELDS = (
    "observation_only",
    "observation_reason",
    "halted",
    "halt_reason",
    "lot_multiplier",
    "force_paper",
    "expected_value_per_day",
    "caveat",
    "added_at",
    "n1_validation",
    "n4_validation",
    "n1_updated_at",
    "n4_updated_at",
    "n4_recomputed_at",
    "params",
)


def _load_nightly_wf_demote(path: Path) -> tuple[set[tuple[str, str]], dict[tuple[str, str], dict]]:
    """夜間ウォークフォワード再検証の demote 候補を (symbol, strategy) 集合で返す.

    `nightly_walkforward_revalidation.py` が出力する ``data/nightly_walkforward_latest.json``
    の ``demote_candidates`` を読み、(symbol, strategy) のセットと、各キーに
    紐づく診断行（oos_win_rate / pass_ratio / oos_total_trades 等）の dict を返す。
    `low_sample_candidates`（取引 0 件等）は **取り込まない**（資金不足由来の偽陽性を
    universe から外さないため）。

    ファイルが存在しない / 古い形式 / 解析失敗時は空集合を返す（保守的に振る舞う）。
    """
    pairs: set[tuple[str, str]] = set()
    detail: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return pairs, detail
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return pairs, detail
    if not isinstance(data, dict):
        return pairs, detail
    for row in data.get("demote_candidates", []) or []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "")
        strat = str(row.get("strategy") or "")
        if not sym or not strat:
            continue
        key = (sym, strat)
        pairs.add(key)
        detail[key] = {
            "oos_win_rate": row.get("oos_win_rate"),
            "pass_ratio": row.get("pass_ratio"),
            "oos_daily_mean": row.get("oos_daily_mean"),
            "oos_total_trades": row.get("oos_total_trades"),
            "windows": row.get("windows"),
        }
    return pairs, detail


def _name_lookup() -> dict[str, str]:
    fit_map_path = DATA_DIR / "strategy_fit_map.json"
    data = _load_json(fit_map_path, {})
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for s, row in data.items():
            if isinstance(row, dict) and row.get("name"):
                out[s] = str(row["name"])
    # fallback: market_universe_all.json
    uni_all = _load_json(DATA_DIR / "market_universe_all.json", [])
    if isinstance(uni_all, list):
        for r in uni_all:
            if isinstance(r, dict) and r.get("symbol") and r.get("name"):
                out.setdefault(str(r["symbol"]), str(r["name"]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="保存せず差分のみ表示")
    ap.add_argument(
        "--macd-params",
        default=str(MACD_PARAMS),
    )
    ap.add_argument(
        "--universe",
        default=str(UNIVERSE_ACTIVE),
    )
    ap.add_argument(
        "--min-oos-daily",
        type=float,
        default=0.0,
        help="Robust であってもこの値未満は採用しない（既定 0）",
    )
    ap.add_argument(
        "--require-is-oos-pass",
        type=int,
        default=1,
        help="1=is_oos_pass=True 以外は採用・置換しない（既定 1）",
    )
    ap.add_argument(
        "--min-oos-trades",
        type=int,
        default=30,
        help="oos_trades がこの値未満は採用・置換しない（既定 30）",
    )
    ap.add_argument(
        "--min-oos-pf",
        type=float,
        default=1.1,
        help="oos_pf がこの値未満は採用・置換しない（既定 1.1）",
    )
    ap.add_argument(
        "--no-backup",
        action="store_true",
        help="既存 universe_active.json のバックアップを取らない",
    )
    ap.add_argument(
        "--nightly-wf-path",
        default=str(NIGHTLY_WF_LATEST),
        help=(
            "夜間 WF 再検証の最新出力（demote_candidates を読む）。"
            "存在しない場合は二段ゲート無効として動作する。"
        ),
    )
    ap.add_argument(
        "--ignore-nightly-wf",
        action="store_true",
        help="二段ゲート（nightly WF demote）を無効化する。緊急時のみ。",
    )
    ap.add_argument(
        "--enforce-nightly-wf-removal",
        action="store_true",
        help=(
            "夜間 WF demote 入りペアを既存 universe からも除去する（破壊的）。"
            "既定 OFF（観察のみ）— 直近 OOS のノイズで主力 Robust が大量消失するのを"
            "防ぐため、レポートに記録するだけで universe は据え置く。"
            "数日連続して demote が継続したら ON を検討。"
        ),
    )
    ap.add_argument(
        "--observation-pairs-path",
        default=str(OBSERVATION_PAIRS),
        help=(
            "手動観察候補（5-split WF 5/5 通過だが trades 不足など）を読み込む JSON。"
            "merge 後の universe_active.json に source='manual_observation' で追加する。"
            "実 paper 投入は jp_live_runner 側の sector_strength gate と組み合わせて段階導入。"
        ),
    )
    ap.add_argument(
        "--ignore-observation-pairs",
        action="store_true",
        help="観察候補マージを無効化する。",
    )
    args = ap.parse_args()

    params = _load_json(Path(args.macd_params), {})
    universe_payload = _load_json(Path(args.universe), {})
    names = _name_lookup()

    # 2026-04-28: 夜間ウォークフォワード再検証で「直近の OOS 勝率/通過率が閾値割れ」
    # と判定されたペアを (symbol, strategy) で取得し、
    #   (a) Robust 採用拒否（追加・置換のガード）
    #   (b) 既に universe にある同一ペアの除去
    # の二段ゲートに使う。`low_sample_candidates`（取引 0 件等）は除外される。
    nightly_demote_set: set[tuple[str, str]] = set()
    nightly_demote_detail: dict[tuple[str, str], dict] = {}
    if not args.ignore_nightly_wf:
        nightly_demote_set, nightly_demote_detail = _load_nightly_wf_demote(
            Path(args.nightly_wf_path)
        )

    robust = _robust_symbols(params)
    # min_oos_daily / is_oos_pass / oos_trades / oos_pf フィルタ
    rejected: list[dict] = []
    accepted: dict[str, dict] = {}
    for s, p in robust.items():
        oos_daily = float(p.get("oos_daily") or 0.0)
        oos_trades = int(p.get("oos_trades") or 0)
        oos_pf = float(p.get("oos_pf") or 0.0)
        is_oos_pass = bool(p.get("is_oos_pass", False))
        reasons = []
        if oos_daily < float(args.min_oos_daily):
            reasons.append(f"oos_daily<{args.min_oos_daily}")
        if args.require_is_oos_pass and not is_oos_pass:
            reasons.append("is_oos_pass=False")
        if oos_trades < args.min_oos_trades:
            reasons.append(f"oos_trades<{args.min_oos_trades}")
        if oos_pf < args.min_oos_pf:
            reasons.append(f"oos_pf<{args.min_oos_pf}")
        # 2026-04-28: 二段ゲート — 夜間 WF で MacdRci ペアが demote 入りしていれば拒否。
        nightly_hit = nightly_demote_detail.get((s, "MacdRci"))
        if nightly_hit is not None:
            reasons.append(
                "nightly_wf_demote("
                f"win={nightly_hit.get('oos_win_rate')},"
                f"pass={nightly_hit.get('pass_ratio')},"
                f"daily={nightly_hit.get('oos_daily_mean')},"
                f"trades={nightly_hit.get('oos_total_trades')}"
                ")"
            )
        if reasons:
            rejected.append({
                "symbol": s,
                "oos_daily": oos_daily,
                "oos_trades": oos_trades,
                "oos_pf": oos_pf,
                "is_oos_pass": is_oos_pass,
                "reasons": reasons,
            })
            continue
        accepted[s] = p
    robust = accepted
    robust_set = set(robust.keys())

    existing_symbols = list((universe_payload or {}).get("symbols") or [])
    # 2026-05-01: 同一 symbol で複数 strategy を持つ並走運用 (e.g. 9984.T MacdRci +
    # 9984.T EnhancedMacdRci) と observation_only フラグを保持するため、
    # キーを (symbol, strategy) のペアに変更。以前は symbol 単一キーだったため
    # 並走戦略の片側が毎回 cron で消失するバグがあった。
    existing_by_pair: dict[tuple[str, str], dict] = {}
    existing_by_sym_strats: dict[str, set[str]] = {}
    for row in existing_symbols:
        if isinstance(row, dict) and row.get("symbol"):
            sym_key = row["symbol"]
            strat_key = str(row.get("strategy") or "")
            existing_by_pair[(sym_key, strat_key)] = row
            existing_by_sym_strats.setdefault(sym_key, set()).add(strat_key)

    added: list[dict] = []
    promoted: list[dict] = []
    untouched_in_universe: list[str] = []
    protected_observation: list[dict] = []
    protected_alt_strategy: list[dict] = []

    for sym, p in robust.items():
        macd_key = (sym, "MacdRci")
        # ペア (sym, MacdRci) が既に存在しない場合のみ MacdRci 行を追加。
        # 既存に他戦略 (EnhancedMacdRci 等) が居ても触らない（並走を許容）。
        if macd_key not in existing_by_pair:
            row = _symbol_row_from_macd(sym, p, names.get(sym, sym))
            existing_by_pair[macd_key] = row
            existing_by_sym_strats.setdefault(sym, set()).add("MacdRci")
            added.append(row)
        else:
            cur = existing_by_pair[macd_key]
            # 観察フラグ付きエントリは絶対保護 (上書きしない)
            if bool(cur.get("observation_only", False)):
                protected_observation.append({
                    "symbol": sym,
                    "strategy": "MacdRci",
                    "reason": cur.get("observation_reason", "observation_only"),
                })
                untouched_in_universe.append(sym)
                continue
            cur_oos = float(cur.get("oos_daily") or 0.0)
            macd_oos = float(p.get("oos_daily") or 0.0)
            # MacdRci 同士なら oos_daily が改善した場合のみメトリクス更新
            if macd_oos > cur_oos + 0.5:
                new_row = _symbol_row_from_macd(sym, p, cur.get("name") or names.get(sym, sym))
                new_row["name"] = cur.get("name") or new_row["name"]
                # N7: 既存の保護フィールド全部を維持 (lot_multiplier/force_paper/caveat/params 等)
                for keep_field in PROTECTED_FIELDS:
                    if keep_field in cur:
                        new_row[keep_field] = cur[keep_field]
                existing_by_pair[macd_key] = new_row
                promoted.append({
                    "symbol": sym,
                    "prev_strategy": "MacdRci",
                    "prev_oos_daily": cur_oos,
                    "new_strategy": "MacdRci",
                    "new_oos_daily": macd_oos,
                })
            else:
                untouched_in_universe.append(sym)

    # 2026-05-01: 既存の MacdRci 以外戦略 (EnhancedMacdRci, Breakout, Pullback 等) は
    # robust merge では一切上書き・削除しない。これにより consolidate_universe_active.py
    # の結果や observation_only マークが朝 cron で吹っ飛ぶのを防ぐ。
    for (sym, strat), row in list(existing_by_pair.items()):
        if strat == "MacdRci":
            continue
        protected_alt_strategy.append({
            "symbol": sym,
            "strategy": strat,
            "observation_only": bool(row.get("observation_only", False)),
        })

    # 2026-04-28: 二段ゲートの後段 — 既に universe にあるペアで夜間 WF demote 入りの
    # ものを観察し、`--enforce-nightly-wf-removal` ON のときだけ実際に除去する。
    # 既定 OFF にするのは、直近 4 日程度の OOS で「主力 Robust（3103.T, 6613.T 等）」
    # が誤って一気に消えるリスクを避けるため。観察のみのときも `removed_by_nightly_wf`
    # にレポートを残し、数日連続 demote が継続したら手動で `paused_pairs` に追加する
    # 運用フローを採る。
    removed_by_demote: list[dict] = []
    observed_demote: list[dict] = []
    for (sym, cur_strat) in list(existing_by_pair.keys()):
        if (sym, cur_strat) not in nightly_demote_set:
            continue
        cur = existing_by_pair[(sym, cur_strat)]
        # observation_only エントリは demote 対象外（既に観察モード）
        if bool(cur.get("observation_only", False)):
            continue
        d = nightly_demote_detail.get((sym, cur_strat), {})
        entry = {
            "symbol": sym,
            "strategy": cur_strat,
            "oos_win_rate": d.get("oos_win_rate"),
            "pass_ratio": d.get("pass_ratio"),
            "oos_daily_mean": d.get("oos_daily_mean"),
            "oos_total_trades": d.get("oos_total_trades"),
            "windows": d.get("windows"),
            "reason": "nightly_wf_demote",
        }
        if args.enforce_nightly_wf_removal:
            removed_by_demote.append(entry)
            del existing_by_pair[(sym, cur_strat)]
        else:
            observed_demote.append(entry)

    # 2026-04-28: 観察候補マージ — trades 不足で Robust 採用閾値を満たさないが、
    # 5-split rolling WF で 5/5 OOS positive を確認したペアを source='manual_observation'
    # で universe に追加する。`force_paper=False` のため jp_live_runner は
    # 既定の paper_low_sample_excluded ロジックでスキップする想定（C7 で sector_strength
    # gate と併せて段階解除）。重複追加を避けるため symbol が既に存在する場合は skip。
    observation_added: list[dict] = []
    observation_skipped: list[dict] = []
    if not args.ignore_observation_pairs:
        obs_payload = _load_json(Path(args.observation_pairs_path), {}) or {}
        for entry in obs_payload.get("pairs", []) or []:
            if not isinstance(entry, dict):
                continue
            sym = entry.get("symbol")
            strat = entry.get("strategy")
            if not sym or not strat:
                continue
            pair_key = (sym, strat)
            if pair_key in existing_by_pair:
                observation_skipped.append({
                    "symbol": sym,
                    "strategy": strat,
                    "reason": "already_in_universe",
                })
                continue
            row = {
                "symbol": sym,
                "name": entry.get("name") or names.get(sym, sym),
                "strategy": strat,
                "score": 0.0,
                "is_daily": 0.0,
                "oos_daily": 0.0,
                "is_pf": 0.0,
                "is_trades": 0,
                "robust": False,
                "is_oos_pass": False,
                "calmar": 0.0,
                "source": "manual_observation",
                "observation_meta": {
                    "reason": entry.get("reason", ""),
                    "evidence": entry.get("evidence", ""),
                    "added_at": entry.get("added_at", ""),
                    "force_paper": bool(entry.get("force_paper", False)),
                },
            }
            existing_by_pair[pair_key] = row
            observation_added.append({
                "symbol": sym,
                "strategy": strat,
                "reason": entry.get("reason", ""),
            })

    # 全 (symbol, strategy) ペアを並べ score で sort
    new_symbols = list(existing_by_pair.values())
    new_symbols.sort(key=lambda r: float(r.get("score") or 0.0), reverse=True)

    now = datetime.now(JST).isoformat()
    new_payload = {
        **(universe_payload or {}),
        "updated": datetime.now(JST).strftime("%Y-%m-%d"),
        "updated_at": now,
        "active_count": len(new_symbols),
        "symbols": new_symbols,
        "last_merge": {
            "at": now,
            "source": "macd_rci_params.robust",
            "added": [a["symbol"] for a in added],
            "promoted": [p["symbol"] for p in promoted],
            "robust_count": len(robust_set),
        },
    }

    prev_syms = {row.get("symbol") for row in existing_symbols if isinstance(row, dict)}
    report = {
        "computed_at": now,
        "macd_params_path": args.macd_params,
        "universe_path": args.universe,
        "robust_total": len(robust_set),
        "prev_universe_size": len(existing_symbols),
        "new_universe_size": len(new_symbols),
        "added": added,
        "promoted": promoted,
        "rejected_low_quality": rejected,
        "removed_by_nightly_wf": removed_by_demote,
        "observed_nightly_wf_demote": observed_demote,
        "nightly_wf_demote_total": len(nightly_demote_set),
        "nightly_wf_path": args.nightly_wf_path,
        "nightly_wf_ignored": bool(args.ignore_nightly_wf),
        "nightly_wf_enforce_removal": bool(args.enforce_nightly_wf_removal),
        "observation_added": observation_added,
        "observation_skipped": observation_skipped,
        "observation_pairs_path": args.observation_pairs_path,
        "observation_ignored": bool(args.ignore_observation_pairs),
        "untouched_robust_in_universe": sorted(untouched_in_universe),
        "outside_robust_in_universe": sorted(prev_syms - robust_set),
        "protected_observation": protected_observation,
        "protected_alt_strategy": protected_alt_strategy,
        "thresholds": {
            "min_oos_daily": args.min_oos_daily,
            "require_is_oos_pass": bool(args.require_is_oos_pass),
            "min_oos_trades": args.min_oos_trades,
            "min_oos_pf": args.min_oos_pf,
        },
        "dry_run": bool(args.dry_run),
    }

    # 人間向け summary
    print(f"robust_total={len(robust_set)}  prev_universe={len(existing_symbols)}  new_universe={len(new_symbols)}")
    print(
        f"added={len(added)}  promoted(metric update)={len(promoted)}  "
        f"untouched_robust={len(untouched_in_universe)}  "
        f"rejected_low_quality={len(rejected)}  "
        f"removed_by_nightly_wf={len(removed_by_demote)}  "
        f"observed_nightly_wf_demote={len(observed_demote)}  "
        f"protected_observation={len(protected_observation)}  "
        f"protected_alt_strategy={len(protected_alt_strategy)}  "
        f"nightly_wf_demote_total={len(nightly_demote_set)}  "
        f"enforce_removal={bool(args.enforce_nightly_wf_removal)}"
    )
    if protected_observation:
        print()
        print("=== 観察モード保護 (observation_only=True を上書きしなかった) ===")
        for r in protected_observation:
            print(f"  {r['symbol']:<8} {r['strategy']:<18} reason={r.get('reason')}")
    if protected_alt_strategy:
        print()
        print("=== 並走戦略保護 (MacdRci 以外の既存ペアを上書きしなかった) ===")
        for r in protected_alt_strategy:
            obs_mark = "[OBS]" if r.get("observation_only") else "     "
            print(f"  {obs_mark} {r['symbol']:<8} {r['strategy']}")
    if removed_by_demote:
        print()
        print("=== Universe から除去 (夜間 WF demote 入り, --enforce-nightly-wf-removal) ===")
        for r in removed_by_demote:
            print(
                f"  {r['symbol']:<8} {r['strategy']:<12} "
                f"win={r.get('oos_win_rate')} pass={r.get('pass_ratio')} "
                f"daily={r.get('oos_daily_mean')} trades={r.get('oos_total_trades')}"
            )
    if observed_demote:
        print()
        print("=== 観察のみ (夜間 WF demote 入りだが既存 universe からは外さない) ===")
        for r in observed_demote:
            print(
                f"  {r['symbol']:<8} {r['strategy']:<12} "
                f"win={r.get('oos_win_rate')} pass={r.get('pass_ratio')} "
                f"daily={r.get('oos_daily_mean')} trades={r.get('oos_total_trades')}"
            )
    if rejected:
        print()
        print("=== 採用拒否 (品質ゲート不通過) ===")
        for r in rejected[:20]:
            print(f"  {r['symbol']:<8} oos_daily={r['oos_daily']:>7.1f} "
                  f"oos_trades={r['oos_trades']:>4d} oos_pf={r['oos_pf']:>4.2f} "
                  f"is_oos_pass={r['is_oos_pass']} reasons={','.join(r['reasons'])}")
        if len(rejected) > 20:
            print(f"  ... and {len(rejected) - 20} more")
    if added:
        print()
        print("=== 追加 (Robust だが universe 未掲載だった) ===")
        for a in added:
            print(
                f"  {a['symbol']:<8} {a['name']:<14} oos_daily={a['oos_daily']:>8.1f}  "
                f"is_pf={a['is_pf']:>5.2f}  is_trades={a['is_trades']}"
            )
    if promoted:
        print()
        print("=== 戦略置換 (Robust 確証に合わせて MacdRci へ) ===")
        for p in promoted:
            print(
                f"  {p['symbol']:<8} {p['prev_strategy']:<12} oos={p['prev_oos_daily']:>7.1f}  →  "
                f"{p['new_strategy']:<8} oos={p['new_oos_daily']:>7.1f}"
            )

    if args.dry_run:
        print("\n(dry-run: 保存しません)")
        return

    # 保存
    uni_path = Path(args.universe)
    if not args.no_backup:
        b = _backup(uni_path)
        if b:
            print(f"backup: {b}")
    uni_path.write_text(json.dumps(new_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    MERGE_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved: {uni_path}")
    print(f"saved: {MERGE_REPORT}")


if __name__ == "__main__":
    main()
