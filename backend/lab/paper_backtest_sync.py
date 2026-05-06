"""ペーパーテストをバックテスト正本（universe_active + fit_map + macd params）と同期する.

- 朝: 運用ユニバースに載っている銘柄×最適手法をそのまま Paper に載せる（連続テスト D 案と同じ材料）。
- 当日の比較基準: 各銘柄の OOS 日次損益の合計（sum_oos）。過去にペーパーがそれを上回った日があれば、その水準も次回以降の下支え（floor）として使う。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from backend.backtesting.strategy_factory import create as create_strategy

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

_REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = _REPO_ROOT / "data"
UNIVERSE_ACTIVE = DATA_DIR / "universe_active.json"
FIT_MAP_FILE = DATA_DIR / "strategy_fit_map.json"
MACD_PARAMS_FILE = DATA_DIR / "macd_rci_params.json"
HANDOFF_FILE = DATA_DIR / "paper_validation_handoff.json"
LOW_SAMPLE_EXCLUDED_FILE = DATA_DIR / "paper_low_sample_excluded_latest.json"

# Robust 集合には残しつつ推し運用（paper/live）からは外す最小 OOS トレード件数。
# 2026-04-23 の報告で明文化（bull_forecast/BACKTEST_REPORT: サンプル不足組の昇格抑止）。
# 環境変数で上書き可能。0 以下に設定するとフィルタ無効。
_DEFAULT_MIN_OOS_TRADES_FOR_LIVE = 30
# Walkforward が pass_ratio==1.0 かつ wf_window_total>=2 を満たす銘柄は、
# 再現性の追加エビデンスがあるとみなして閾値を緩める（既定 20）。
_DEFAULT_MIN_OOS_TRADES_FOR_LIVE_WF_RELAXED = 20
_DEFAULT_WF_RELAX_MIN_WINDOWS = 2


def _min_oos_trades_for_live() -> int:
    raw = os.getenv("MIN_OOS_TRADES_FOR_LIVE")
    if raw is None or str(raw).strip() == "":
        return _DEFAULT_MIN_OOS_TRADES_FOR_LIVE
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_MIN_OOS_TRADES_FOR_LIVE


def _min_oos_trades_for_live_wf_relaxed() -> int:
    raw = os.getenv("MIN_OOS_TRADES_FOR_LIVE_WF_RELAXED")
    if raw is None or str(raw).strip() == "":
        return _DEFAULT_MIN_OOS_TRADES_FOR_LIVE_WF_RELAXED
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_MIN_OOS_TRADES_FOR_LIVE_WF_RELAXED


def _wf_relax_min_windows() -> int:
    raw = os.getenv("MIN_OOS_TRADES_WF_RELAX_MIN_WINDOWS")
    if raw is None or str(raw).strip() == "":
        return _DEFAULT_WF_RELAX_MIN_WINDOWS
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_WF_RELAX_MIN_WINDOWS


def _wf_pass_meta(
    row: dict,
    fit_map_row: dict,
    strategy_name: str,
) -> tuple[int, float]:
    """(wf_window_total, wf_window_pass_ratio) を universe_active → strategy_fit_map の順で解決。"""
    fit_strategies = fit_map_row.get("strategies") if isinstance(fit_map_row, dict) else None
    fit_strategy_row: dict = {}
    if isinstance(fit_strategies, dict):
        raw = fit_strategies.get(strategy_name)
        if isinstance(raw, dict):
            fit_strategy_row = raw

    def _to_int(v) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    def _to_float(v) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    wf_total_raw = (
        row.get("wf_window_total")
        if row.get("wf_window_total") is not None
        else fit_strategy_row.get("wf_window_total")
    )
    wf_ratio_raw = (
        row.get("wf_window_pass_ratio")
        if row.get("wf_window_pass_ratio") is not None
        else fit_strategy_row.get("wf_window_pass_ratio")
    )
    return _to_int(wf_total_raw), _to_float(wf_ratio_raw)


def _load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_oos_trades(
    symbol: str,
    row: dict,
    macd_params_row: dict,
    fit_map_row: dict,
    strategy_name: str,
) -> int | None:
    """universe_active / macd_rci_params / strategy_fit_map から oos_trades を拾う（最初に見つかった整数）。

    ``strategy_fit_map.json`` は ``{symbol: {"strategies": {strategy_name: {..., "oos_trades": N}}}}``
    という入れ子構造なので、``strategies[strategy_name]`` を明示的に参照する。これにより
    ``macd_rci_params.json`` に載らない Breakout / Scalp 等でも OOS 件数が拾える。
    """
    fit_strategies = fit_map_row.get("strategies") if isinstance(fit_map_row, dict) else None
    fit_strategy_row: dict = {}
    if isinstance(fit_strategies, dict):
        raw = fit_strategies.get(strategy_name)
        if isinstance(raw, dict):
            fit_strategy_row = raw

    candidates = (
        row.get("oos_trades"),
        macd_params_row.get("oos_trades"),
        fit_strategy_row.get("oos_trades"),
        fit_map_row.get("oos_trades"),
    )
    for c in candidates:
        if c is None:
            continue
        try:
            return int(c)
        except (TypeError, ValueError):
            continue
    return None


def _audit_macd_rci_direct_pairs(
    macd_params: dict,
    active_pairs: set[tuple[str, str]],
    min_oos_trades: int,
    wf_relaxed_threshold: int,
    wf_relax_min_windows: int,
) -> list[dict]:
    """`macd_rci_params.json` の MacdRci robust 銘柄のうち、`universe_active`
    に居ないものを ``experiments`` DB から最新 robust 実績を引いて
    OOS 件数 / WF 通過率で評価する。閾値未満なら excluded リストを返す。

    `jp_live_runner` は `macd_rci_params.json` の `robust=true` から直接
    エントリー候補を拾うパスがあり、`universe_active` 外で paper に乗る
    銘柄は `paper_low_sample_excluded_latest.json` に登録されないと
    `_apply_low_sample_second_filter` の対象にもならず素通りする。
    本関数で穴を塞ぐ。
    """
    if not isinstance(macd_params, dict):
        return []
    direct_targets: list[str] = []
    for sym, row in macd_params.items():
        if not isinstance(sym, str) or sym.startswith("_"):
            continue
        if not isinstance(row, dict) or not row.get("robust"):
            continue
        if ("MacdRci", sym) in active_pairs:
            continue
        direct_targets.append(sym)
    if not direct_targets:
        return []
    try:
        from backend.storage.db import _DB_PATH  # 遅延 import (循環回避)
    except Exception:
        return []
    db_path = Path(str(_DB_PATH))
    if not db_path.exists():
        return []
    out: list[dict] = []
    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        try:
            for sym in direct_targets:
                cur = conn.execute(
                    """
                    SELECT id, oos_trades, oos_daily_pnl,
                           wf_window_total, wf_window_pass_ratio
                      FROM experiments
                     WHERE symbol = ? AND strategy_name = 'MacdRci' AND robust = 1
                     ORDER BY id DESC LIMIT 1
                    """,
                    (sym,),
                )
                row = cur.fetchone()
                if not row:
                    continue
                _, oos_trades_raw, oos_daily_raw, wf_total_raw, wf_ratio_raw = row
                if oos_trades_raw is None:
                    continue
                try:
                    oos_trades = int(oos_trades_raw)
                except (TypeError, ValueError):
                    continue
                try:
                    wf_total = int(wf_total_raw) if wf_total_raw is not None else 0
                except (TypeError, ValueError):
                    wf_total = 0
                try:
                    wf_ratio = float(wf_ratio_raw) if wf_ratio_raw is not None else 0.0
                except (TypeError, ValueError):
                    wf_ratio = 0.0
                wf_relaxed = (
                    wf_relaxed_threshold > 0
                    and wf_ratio >= 1.0 - 1e-9
                    and wf_total >= wf_relax_min_windows
                )
                effective_threshold = wf_relaxed_threshold if wf_relaxed else min_oos_trades
                if oos_trades >= effective_threshold:
                    continue
                out.append({
                    "strategy_name": "MacdRci",
                    "symbol": sym,
                    "name": sym,
                    "oos_daily_pnl": float(oos_daily_raw or 0.0),
                    "oos_trades": oos_trades,
                    "wf_window_total": wf_total,
                    "wf_window_pass_ratio": wf_ratio,
                    "effective_threshold": effective_threshold,
                    "reason": (
                        f"oos_trades<{effective_threshold}"
                        + (" (wf_relaxed)" if wf_relaxed else "")
                        + " (macd_rci_params direct, not in universe_active)"
                    ),
                })
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - DB エラーは致命ではない
        logger.info("audit_macd_rci_direct_pairs: skipped: %s", exc)
        return []
    return out


def collect_universe_specs(
    max_count: int = 30,
    force_macd_max_pyramid: int = -1,
    apply_sample_filter: bool = True,
) -> list[dict]:
    """連続テスト D 案と同じロジックで、ペーパー用の (strategy_name, symbol, params, oos) 行を返す。

    ``apply_sample_filter=True`` のとき、OOS trades が ``MIN_OOS_TRADES_FOR_LIVE``（既定 30）未満の
    銘柄は推し運用から除外する。除外された銘柄は ``data/paper_low_sample_excluded_latest.json`` に残す。
    除外された銘柄は ``macd_rci_params.json`` 側で ``robust: true`` のままにして、研究用の Robust 集合は変えない。
    """
    active = _load_json(UNIVERSE_ACTIVE, {})
    macd_params = _load_json(MACD_PARAMS_FILE, {})
    fit_map = _load_json(FIT_MAP_FILE, {})

    symbols = active.get("symbols", []) if isinstance(active, dict) else []
    if not symbols:
        return []

    min_oos_trades = _min_oos_trades_for_live() if apply_sample_filter else 0
    wf_relaxed_threshold = _min_oos_trades_for_live_wf_relaxed() if apply_sample_filter else 0
    wf_relax_min_windows = _wf_relax_min_windows()
    kept: list[dict] = []
    excluded: list[dict] = []
    wf_relaxed_hits: list[dict] = []

    observation_skipped: list[dict] = []
    for row in symbols:
        symbol = row.get("symbol")
        strategy = row.get("strategy")
        if not symbol or not strategy:
            continue
        # observation_only フラグの (symbol, strategy) は paper 推し運用から除外。
        # 5/1 朝報告で導入: バックテスト DB 上では robust 維持しつつ、サンプル不足や
        # 過適合疑い (例: 3103.T MacdRci oos_trades=14 / IS_pf=1.00) のペアを
        # 「観察対象」 として実弾候補から外す。研究用 Robust 集合は変えない。
        if bool(row.get("observation_only", False)):
            observation_skipped.append({
                "symbol": symbol,
                "strategy_name": strategy,
                "reason": str(row.get("observation_reason", "observation_only=True"))[:200],
            })
            continue
        fit_row = fit_map.get(symbol, {}) if isinstance(fit_map, dict) else {}
        macd_row = macd_params.get(symbol, {}) if isinstance(macd_params, dict) else {}
        params: dict = {}
        if strategy == "MacdRci":
            params = dict(macd_row)
            params.setdefault("interval", "5m")
            if force_macd_max_pyramid >= 0:
                params["max_pyramid"] = force_macd_max_pyramid
        oos_trades = _resolve_oos_trades(symbol, row, macd_row, fit_row, strategy)
        wf_total, wf_ratio = _wf_pass_meta(row, fit_row, strategy)
        spec = {
            "strategy_name": strategy,
            "symbol": symbol,
            "name": row.get("name", fit_row.get("name", symbol)),
            "oos_daily_pnl": float(row.get("oos_daily", 0.0)),
            "oos_trades": oos_trades,
            "wf_window_total": wf_total,
            "wf_window_pass_ratio": wf_ratio,
            "params": params,
            # N5 (2026-05-03): lot_multiplier を spec に伝搬。portfolio_sim と
            # jp_live_runner で 1 銘柄あたりのポジションサイズに乗じる。
            "lot_multiplier": float(row.get("lot_multiplier", 1.0) or 1.0),
            # N6 (2026-05-06): force_paper を spec に伝搬 (sample_filter バイパス監査用)
            "force_paper": bool(row.get("force_paper", False)),
        }
        # N6 (2026-05-06): force_paper=true は研究判断で慎重投入が決まっている entry。
        # sample_filter をバイパスして paper に必ず乗せる (lot_multiplier で抑制済の前提)。
        force_paper = bool(row.get("force_paper", False))

        if apply_sample_filter and min_oos_trades > 0 and (oos_trades is not None) and not force_paper:
            # WF 再現性が担保されている銘柄は閾値を緩和（pass_ratio=1.0 かつ windows>=N）
            wf_relaxed = (
                wf_relaxed_threshold > 0
                and wf_ratio >= 1.0 - 1e-9
                and wf_total >= wf_relax_min_windows
            )
            effective_threshold = wf_relaxed_threshold if wf_relaxed else min_oos_trades
            if oos_trades < effective_threshold:
                excluded.append({
                    "strategy_name": strategy,
                    "symbol": symbol,
                    "name": spec["name"],
                    "oos_daily_pnl": spec["oos_daily_pnl"],
                    "oos_trades": oos_trades,
                    "wf_window_total": wf_total,
                    "wf_window_pass_ratio": wf_ratio,
                    "effective_threshold": effective_threshold,
                    "reason": (
                        f"oos_trades<{effective_threshold}"
                        + (" (wf_relaxed)" if wf_relaxed else "")
                    ),
                })
                continue
            if wf_relaxed and oos_trades < min_oos_trades:
                wf_relaxed_hits.append({
                    "symbol": symbol,
                    "strategy_name": strategy,
                    "oos_trades": oos_trades,
                    "wf_window_total": wf_total,
                    "wf_window_pass_ratio": wf_ratio,
                    "effective_threshold": effective_threshold,
                })
        elif force_paper and apply_sample_filter:
            # force_paper の entry は wf_relaxed_hits に記録 (監査用)
            spec["force_paper_bypass"] = True
            wf_relaxed_hits.append({
                "symbol": symbol,
                "strategy_name": strategy,
                "oos_trades": oos_trades,
                "wf_window_total": wf_total,
                "wf_window_pass_ratio": wf_ratio,
                "effective_threshold": "bypass(force_paper=true)",
            })
        kept.append(spec)

    kept.sort(key=lambda x: float(x.get("oos_daily_pnl", 0.0)), reverse=True)

    # 2026-04-30 拡張: universe_active に居ない `macd_rci_params.json` 直拾いの
    # MacdRci robust 銘柄 (jp_live_runner が直接拾うパス) も低 OOS 評価対象に
    # 含める。`paper_low_sample_excluded_latest.json` に追加されると
    # `jp_live_runner._apply_low_sample_second_filter` で paper エントリーから
    # 弾かれる (4/30 4568.T のように oos_trades=17 で 6 trades -3,350 円を出す
    # 穴を塞ぐ)。
    if apply_sample_filter and min_oos_trades > 0:
        active_pairs = {
            (str(s.get("strategy", "")), str(s.get("symbol", "")))
            for s in symbols
            if isinstance(s, dict)
        }
        direct_excluded = _audit_macd_rci_direct_pairs(
            macd_params,
            active_pairs,
            min_oos_trades,
            wf_relaxed_threshold,
            wf_relax_min_windows,
        )
        if direct_excluded:
            excluded.extend(direct_excluded)

    if apply_sample_filter:
        try:
            _save_json(
                LOW_SAMPLE_EXCLUDED_FILE,
                {
                    "generated_at": datetime.now(JST).isoformat(),
                    "min_oos_trades_for_live": min_oos_trades,
                    "min_oos_trades_for_live_wf_relaxed": wf_relaxed_threshold,
                    "wf_relax_min_windows": wf_relax_min_windows,
                    "kept_count": len(kept),
                    "excluded_count": len(excluded),
                    "wf_relaxed_hits": wf_relaxed_hits,
                    "excluded": excluded,
                    "observation_skipped_count": len(observation_skipped),
                    "observation_skipped": observation_skipped,
                },
            )
        except Exception as exc:  # pragma: no cover - IO エラーは致命ではない
            logger.warning("paper_backtest_sync: failed to write low-sample excluded file: %s", exc)

        if excluded:
            logger.info(
                "paper_backtest_sync: サンプル不足除外 %d 件 (min_oos_trades=%d, wf_relaxed=%d): %s",
                len(excluded),
                min_oos_trades,
                wf_relaxed_threshold,
                ", ".join(
                    f"{e['symbol']}({e['oos_trades']}<{e['effective_threshold']})"
                    for e in excluded
                ),
            )
        if wf_relaxed_hits:
            logger.info(
                "paper_backtest_sync: WF緩和で採用 %d 件: %s",
                len(wf_relaxed_hits),
                ", ".join(
                    f"{h['symbol']}(oos={h['oos_trades']}, wf={h['wf_window_total']}/{h['wf_window_pass_ratio']:.2f})"
                    for h in wf_relaxed_hits
                ),
            )
        if observation_skipped:
            logger.info(
                "paper_backtest_sync: observation_only でスキップ %d 件: %s",
                len(observation_skipped),
                ", ".join(
                    f"{o['symbol']}/{o['strategy_name']}"
                    for o in observation_skipped
                ),
            )

    return kept[:max_count]


def instantiate_strategies(rows: list[dict]) -> list:
    strategies = []
    for r in rows:
        name = r.get("name") or r["symbol"].replace(".T", "")
        params = r.get("params") or {}
        try:
            strat = create_strategy(r["strategy_name"], r["symbol"], name=name, params=params)
            strategies.append(strat)
        except Exception as e:
            logger.warning("paper_backtest_sync: skip %s × %s: %s", r["strategy_name"], r["symbol"], e)
    return strategies


def _load_handoff() -> dict:
    raw = _load_json(HANDOFF_FILE, {})
    return raw if isinstance(raw, dict) else {}


def morning_challenge_target(sum_oos_jpy: float) -> tuple[float, dict]:
    """当日のチャレンジ目標（円/日）: max(本日の sum_oos, 過去にペーパーが上回った floor)。"""
    h = _load_handoff()
    floor = float(h.get("paper_promoted_floor_jpy") or 0.0)
    target = max(sum_oos_jpy, floor)
    return target, h


def record_morning_pending(sum_oos_jpy: float) -> float:
    """当日の sum_oos とチャレンジ目標を handoff に書き、目標値を返す（Robust フォールバックでも共通利用）。"""
    today = datetime.now(JST).strftime("%Y-%m-%d")
    challenge, handoff = morning_challenge_target(sum_oos_jpy)
    merged = {
        **handoff,
        "pending_date": today,
        "pending_sum_oos_jpy": sum_oos_jpy,
        "pending_challenge_jpy": challenge,
        "updated_at": datetime.now(JST).isoformat(),
    }
    _save_json(HANDOFF_FILE, merged)
    return challenge


async def prepare_from_backtest_universe(max_count: int = 30) -> tuple[list, dict]:
    """universe_active から戦略を組み立てる。空なら ([], {}) を返す（呼び出し側でフォールバック）。"""
    today = datetime.now(JST).strftime("%Y-%m-%d")
    rows = collect_universe_specs(max_count=max_count)
    if not rows:
        return [], {}

    sum_oos = sum(float(r["oos_daily_pnl"]) for r in rows)
    handoff_before = _load_handoff()
    challenge = record_morning_pending(sum_oos)

    strategies = instantiate_strategies(rows)
    if not strategies:
        return [], {}

    report = {
        "date": today,
        "source": "universe_active_backtest",
        "symbols": list({r["symbol"] for r in rows}),
        "rows": [
            {
                "strategy_name": r["strategy_name"],
                "symbol": r["symbol"],
                "oos_daily_pnl": r["oos_daily_pnl"],
            }
            for r in rows
        ],
        "backtest_sum_oos_jpy": sum_oos,
        "paper_promoted_floor_jpy": float(handoff_before.get("paper_promoted_floor_jpy") or 0.0),
        "session_challenge_jpy": challenge,
        "expected_daily_pnl": sum_oos,
        "total_robust": len(rows),
        "selected_count": len(strategies),
        "regime_map": {},
    }

    logger.info(
        "paper_backtest_sync: %d 戦略 sum_oos=%+.0f 円/日 challenge=%+.0f 円/日 (floor=%+.0f)",
        len(strategies),
        sum_oos,
        challenge,
        float(handoff_before.get("paper_promoted_floor_jpy") or 0.0),
    )

    return strategies, report


# ── Active universe 動的合計 (A 案, 2026-04-30 追加) ──────────────────────────
#
# `backtest_sum_oos_jpy` は `collect_universe_specs` の robust 集合 (今日 13 件)
# 全銘柄の `oos_daily` を素朴に合計した値で、当日シグナルが立たなかった銘柄も
# 含む「理論上限」になる。シグナル発生数が 13 件未満の日は構造的に paper が
# これを下回り、severity=critical が頻発しがちで判断しづらい。
#
# A 案: 当日 paper で実際にエントリーした (symbol, strategy_name) ペアだけに
# 絞った `oos_daily_pnl` 合計 (`active_sum_oos_jpy`) を併記し、こちらの diff_pct
# も `_divergence_report` に出す。これは「立った銘柄での平均ペースに対する
# 当日実績」のチェックになる。旧 `backtest_sum_oos_jpy` は「上限に対する
# 到達率」として残す。
#
# `jp_trade_executions.strategy_id` のプレフィックス → strategy_name の写像。
# `scripts/paper_observability_report.py:_PREFIX_TO_BUCKET` と同期させること。
_LIVE_PREFIX_TO_STRATEGY: tuple[tuple[str, str], ...] = (
    ("jp_parabolic_swing_", "ParabolicSwing"),
    ("jp_swing_donchian_",  "SwingDonchianD"),
    ("enhanced_macd_rci_",  "EnhancedMacdRci"),
    ("enhanced_scalp_",     "EnhancedScalp"),
    ("jp_macd_rci_",        "MacdRci"),
    ("jp_micro_scalp_",     "MicroScalp"),
    ("jp_scalp_",           "Scalp"),
    ("jp_breakout_",        "Breakout"),
    ("jp_bb_short_",        "BbShort"),
    ("jp_pullback_",        "Pullback"),
    ("jp_ma_vol_",          "MaVol"),
)


def _strategy_name_from_id(strategy_id: str) -> str | None:
    sid = strategy_id or ""
    for prefix, name in _LIVE_PREFIX_TO_STRATEGY:
        if sid.startswith(prefix):
            return name
    return None


def _oos_daily_for_pair(
    symbol: str,
    strategy_name: str,
    macd_params: dict,
    fit_map: dict,
) -> float | None:
    """(symbol, strategy_name) の oos_daily を canonical JSON から拾う。

    優先度:
      1. `MacdRci` のときは `macd_rci_params.json[symbol]["oos_daily"]`
      2. `strategy_fit_map.json[symbol]["strategies"][strategy_name]["oos_daily"]`
    どちらも無ければ None。
    """
    if strategy_name == "MacdRci" and isinstance(macd_params, dict):
        row = macd_params.get(symbol)
        if isinstance(row, dict):
            v = row.get("oos_daily")
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
    if isinstance(fit_map, dict):
        sym_row = fit_map.get(symbol)
        if isinstance(sym_row, dict):
            strats = sym_row.get("strategies")
            if isinstance(strats, dict):
                strat_row = strats.get(strategy_name)
                if isinstance(strat_row, dict):
                    v = strat_row.get("oos_daily")
                    if v is not None:
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
    return None


def _active_universe_sum_oos(date_str: str) -> tuple[float, list[dict]] | None:
    """当日 paper で実際にエントリーした (symbol, strategy_name) ペアの
    oos_daily を canonical から引いて合計する (A 案)。

    返り値: (合計, 内訳リスト)。DB 不在 / 当日約定ゼロのときは None。
    内訳リスト要素: ``{"symbol", "strategy_name", "strategy_id", "n_trades",
                      "paper_pnl_jpy", "oos_daily_pnl_jpy"}``。
    """
    try:
        from backend.storage.db import _DB_PATH  # 遅延 import (循環回避)
    except Exception:
        return None
    db_path = Path(str(_DB_PATH))
    if not db_path.exists():
        return None
    macd_params = _load_json(MACD_PARAMS_FILE, {})
    fit_map = _load_json(FIT_MAP_FILE, {})
    rows: list[tuple] = []
    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                """
                SELECT strategy_id, symbol,
                       COUNT(*) AS n,
                       COALESCE(SUM(pnl_jpy), 0) AS pnl
                  FROM jp_trade_executions
                 WHERE date = ?
                 GROUP BY strategy_id, symbol
                 ORDER BY symbol, strategy_id
                """,
                (date_str,),
            )
            rows = list(cur.fetchall())
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - DB 参照不可は致命ではない
        logger.info("active_universe_sum_oos: db read skipped: %s", exc)
        return None
    if not rows:
        return None
    breakdown: list[dict] = []
    total = 0.0
    for sid, sym, n, pnl in rows:
        strat = _strategy_name_from_id(str(sid or "")) or ""
        oos = _oos_daily_for_pair(str(sym or ""), strat, macd_params, fit_map) if strat else None
        if oos is not None:
            total += float(oos)
        breakdown.append({
            "symbol": str(sym or ""),
            "strategy_name": strat,
            "strategy_id": str(sid or ""),
            "n_trades": int(n or 0),
            "paper_pnl_jpy": round(float(pnl or 0.0), 1),
            "oos_daily_pnl_jpy": round(float(oos), 1) if oos is not None else None,
        })
    return total, breakdown


def _divergence_report(
    session_pnl: float,
    sum_oos: float | None,
    active_sum_oos: float | None = None,
    active_breakdown: list[dict] | None = None,
) -> dict | None:
    """Phase E3 (+A 案 2026-04-30): ペーパー vs バックテスト乖離を算出する。

    - ``sum_oos``: robust 集合の oos_daily 合計 (理論上限ベンチ)
    - ``active_sum_oos``: 当日 paper でエントリーした銘柄に絞った oos_daily 合計
      (シグナル発生数で正規化した実績ベンチ)。
    """
    if sum_oos is None:
        return None
    try:
        sum_oos_f = float(sum_oos)
    except (TypeError, ValueError):
        return None
    diff = float(session_pnl) - sum_oos_f
    denom = max(abs(sum_oos_f), 1000.0)  # <1,000円 は分母を 1,000 に丸めてノイズ抑制
    diff_pct = diff / denom * 100
    # 警告水準: 絶対値 > 50%、または期待プラスに対して -30% を下回る
    severity: str | None = None
    if sum_oos_f > 0 and diff_pct <= -30.0:
        severity = "critical" if diff_pct <= -60.0 else "warning"
    elif abs(diff_pct) >= 50.0:
        severity = "warning"

    out: dict = {
        "paper_pnl_jpy": round(float(session_pnl), 1),
        "backtest_sum_oos_jpy": round(sum_oos_f, 1),
        "diff_jpy": round(diff, 1),
        "diff_pct": round(diff_pct, 2),
        "severity": severity,
    }

    # A 案 (active universe) の評価を追加。active_sum_oos が None でも出す
    # (=シグナル発生ゼロ日で、その事実自体が情報になる)。
    if active_sum_oos is not None:
        try:
            active_f = float(active_sum_oos)
        except (TypeError, ValueError):
            active_f = None
    else:
        active_f = None

    if active_f is not None:
        active_diff = float(session_pnl) - active_f
        active_denom = max(abs(active_f), 1000.0)
        active_diff_pct = active_diff / active_denom * 100
        # active_severity は「下振れのみ」を警告対象とする (旧 severity は abs >= 50%
        # で上振れも warning にしてしまうが、これはノイズが多いため active 側では除外)。
        active_sev: str | None = None
        if active_f > 0 and active_diff_pct <= -30.0:
            active_sev = "critical" if active_diff_pct <= -60.0 else "warning"
        elif active_f <= 0 and active_diff_pct <= -50.0:
            # 期待値が 0 円以下で、それすら大きく下回った日も warning
            active_sev = "warning"
        out["active_sum_oos_jpy"] = round(active_f, 1)
        out["active_diff_jpy"] = round(active_diff, 1)
        out["active_diff_pct"] = round(active_diff_pct, 2)
        out["active_severity"] = active_sev
    if active_breakdown is not None:
        out["active_pair_count"] = len(active_breakdown)
        out["active_pairs"] = active_breakdown
    return out


async def _notify_divergence(date_str: str, report: dict) -> None:
    """Phase E3: 乖離が閾値を超えたら Pushover に通知する（失敗時は握り潰す）。

    A 案 (active universe) の severity と sum_oos も併記し、当日シグナル発生
    銘柄ベースでの判定を一目で確認できるようにする。
    """
    sev = report.get("severity")
    active_sev = report.get("active_severity")
    # 旧 severity / active_severity いずれかが立っていれば通知
    if not sev and not active_sev:
        return
    try:
        from backend.notify import push  # 遅延 import（循環回避）

        primary = sev or active_sev
        title = f"ペーパー乖離 [{primary}] {date_str}"
        lines = [
            f"Paper PnL: {report['paper_pnl_jpy']:+.0f} 円",
            (
                f"Robust 13 上限: {report['backtest_sum_oos_jpy']:+.0f} 円  "
                f"diff={report['diff_jpy']:+.0f} ({report['diff_pct']:+.1f}%) "
                f"sev={sev or '-'}"
            ),
        ]
        if "active_sum_oos_jpy" in report:
            n = report.get("active_pair_count", "?")
            lines.append(
                f"Active {n} 銘柄: {report['active_sum_oos_jpy']:+.0f} 円  "
                f"diff={report['active_diff_jpy']:+.0f} "
                f"({report['active_diff_pct']:+.1f}%) sev={active_sev or '-'}"
            )
        msg = "\n".join(lines)
        # critical (旧 / active のいずれか) が立っていれば priority=1
        crit = (sev == "critical") or (active_sev == "critical")
        await push(title=title, message=msg, priority=1 if crit else 0)
    except Exception as exc:  # pragma: no cover - 通知失敗は致命ではない
        logger.warning("paper_backtest_sync: divergence notify failed: %s", exc)


def _write_divergence_report_file(date_str: str, divergence: dict) -> Path | None:
    """severity (旧) または active_severity (A 案) が立った日は
    ``data/paper_vs_backtest_divergence_<DATE>.txt`` に自動保存する。
    SQLite ベースの詳細内訳までは出さず、サマリ行と active pair 内訳を残す
    (詳細は ``scripts/`` 配下の手動レポートで補完する想定)。
    """
    sev = divergence.get("severity")
    active_sev = divergence.get("active_severity")
    if not sev and not active_sev:
        return None
    out = DATA_DIR / f"paper_vs_backtest_divergence_{date_str}.txt"
    try:
        sym_rows = []
        try:
            from backend.storage.db import _DB_PATH  # 遅延 import（循環回避）

            db_path = Path(str(_DB_PATH))
            if db_path.exists():
                import sqlite3

                conn = sqlite3.connect(str(db_path))
                try:
                    cur = conn.execute(
                        """
                        SELECT symbol,
                               COUNT(*) AS n,
                               COALESCE(SUM(pnl_jpy), 0) AS pnl,
                               GROUP_CONCAT(DISTINCT exit_reason) AS reasons
                          FROM jp_trade_executions
                         WHERE date = ?
                         GROUP BY symbol
                         ORDER BY pnl ASC
                        """,
                        (date_str,),
                    )
                    sym_rows = list(cur.fetchall())
                finally:
                    conn.close()
        except Exception as exc:  # pragma: no cover - DB 参照不可は致命ではない
            logger.info("divergence report: db breakdown skipped: %s", exc)

        lines: list[str] = []
        lines.append(f"# paper_vs_backtest_divergence ({date_str})")
        lines.append(f"severity={sev or '-'} active_severity={active_sev or '-'}")
        lines.append(
            f"paper_pnl_jpy={divergence.get('paper_pnl_jpy'):+.0f} "
            f"backtest_sum_oos_jpy={divergence.get('backtest_sum_oos_jpy'):+.0f} "
            f"diff_jpy={divergence.get('diff_jpy'):+.0f} "
            f"diff_pct={divergence.get('diff_pct'):+.2f}"
        )
        if "active_sum_oos_jpy" in divergence:
            lines.append(
                f"active_sum_oos_jpy={divergence.get('active_sum_oos_jpy'):+.0f} "
                f"active_diff_jpy={divergence.get('active_diff_jpy'):+.0f} "
                f"active_diff_pct={divergence.get('active_diff_pct'):+.2f} "
                f"active_pair_count={divergence.get('active_pair_count', '?')}"
            )
        lines.append(
            "auto_saved_by=backend.lab.paper_backtest_sync.finalize_paper_session"
        )
        # active pair (=当日シグナルが立った銘柄) の paper vs oos_daily 内訳
        pairs = divergence.get("active_pairs") or []
        if pairs:
            lines.append("")
            lines.append("active pair breakdown (paper vs oos_daily):")
            lines.append(
                f"{'symbol':<10} {'strategy':<18} {'n':>3} "
                f"{'paper_pnl':>10} {'oos_daily':>10} {'diff':>10}"
            )
            lines.append("-" * 70)
            for p in pairs:
                paper = float(p.get("paper_pnl_jpy") or 0.0)
                oos = p.get("oos_daily_pnl_jpy")
                oos_v = float(oos) if oos is not None else 0.0
                diff_v = paper - oos_v if oos is not None else 0.0
                oos_disp = f"{oos_v:+.0f}" if oos is not None else "n/a"
                diff_disp = f"{diff_v:+.0f}" if oos is not None else "n/a"
                lines.append(
                    f"{p.get('symbol','')[:10]:<10} "
                    f"{p.get('strategy_name','')[:18]:<18} "
                    f"{p.get('n_trades',0):>3d} "
                    f"{paper:>+10.0f} {oos_disp:>10} {diff_disp:>10}"
                )
        if sym_rows:
            lines.append("")
            lines.append("symbol      n      pnl     reasons")
            lines.append("-" * 60)
            for row in sym_rows:
                sym = str(row[0] or "")
                n = int(row[1] or 0)
                pnl = float(row[2] or 0.0)
                reasons = (row[3] or "").replace(",", "|") if len(row) > 3 else ""
                lines.append(f"{sym:<10} {n:>4} {pnl:>+8.0f}  {reasons}")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(
            "divergence report saved: %s (severity=%s active=%s)",
            out, sev, active_sev,
        )
        return out
    except Exception as exc:  # pragma: no cover - 書き込み失敗も致命ではない
        logger.warning("paper_backtest_sync: divergence report write failed: %s", exc)
        return None


def finalize_paper_session(session_date: str, session_pnl_jpy: float, sum_oos_snapshot: float | None) -> dict:
    """大引け後: ペーパー損益が当日のバックテスト合計 OOS を上回ったら floor を更新。

    A 案 (2026-04-30): `_active_universe_sum_oos(session_date)` で当日 paper の
    エントリー銘柄に絞った oos_daily 合計を併算し、divergence report に
    `active_*` フィールドとして併記する。
    """
    h = _load_handoff()
    promoted = float(h.get("paper_promoted_floor_jpy") or 0.0)
    beat = False
    if sum_oos_snapshot is not None:
        if session_pnl_jpy >= float(sum_oos_snapshot):
            promoted = max(promoted, session_pnl_jpy)
            beat = True

    active_pair = _active_universe_sum_oos(session_date)
    if active_pair is not None:
        active_sum, active_breakdown = active_pair
    else:
        active_sum, active_breakdown = None, None

    divergence = _divergence_report(
        session_pnl_jpy,
        sum_oos_snapshot,
        active_sum_oos=active_sum,
        active_breakdown=active_breakdown,
    )

    out = {
        **h,
        "paper_promoted_floor_jpy": promoted,
        "last_session_date": session_date,
        "last_session_pnl_jpy": round(float(session_pnl_jpy), 1),
        "last_pending_sum_oos_jpy": sum_oos_snapshot,
        "last_beat_backtest_sum_oos": beat,
        "last_divergence": divergence,
        "updated_at": datetime.now(JST).isoformat(),
    }
    _save_json(HANDOFF_FILE, out)
    logger.info(
        "paper_validation handoff: pnl=%+.0f sum_oos_snap=%s beat=%s promoted_floor=%+.0f divergence=%s",
        session_pnl_jpy,
        sum_oos_snapshot,
        beat,
        promoted,
        divergence,
    )
    # 乖離通知（非同期 fire-and-forget）と自動レポート保存
    if divergence and divergence.get("severity"):
        _write_divergence_report_file(session_date, divergence)
        try:
            import asyncio

            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_notify_divergence(session_date, divergence))
            else:
                asyncio.run(_notify_divergence(session_date, divergence))
        except Exception as exc:  # pragma: no cover
            logger.warning("paper_backtest_sync: divergence schedule failed: %s", exc)
    return out
