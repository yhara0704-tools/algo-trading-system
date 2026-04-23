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

    for row in symbols:
        symbol = row.get("symbol")
        strategy = row.get("strategy")
        if not symbol or not strategy:
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
        }
        if apply_sample_filter and min_oos_trades > 0 and (oos_trades is not None):
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
        kept.append(spec)

    kept.sort(key=lambda x: float(x.get("oos_daily_pnl", 0.0)), reverse=True)

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


def _divergence_report(session_pnl: float, sum_oos: float | None) -> dict | None:
    """Phase E3: ペーパー vs バックテスト (sum_oos) の乖離を算出する。"""
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
    return {
        "paper_pnl_jpy": round(float(session_pnl), 1),
        "backtest_sum_oos_jpy": round(sum_oos_f, 1),
        "diff_jpy": round(diff, 1),
        "diff_pct": round(diff_pct, 2),
        "severity": severity,
    }


async def _notify_divergence(date_str: str, report: dict) -> None:
    """Phase E3: 乖離が閾値を超えたら Pushover に通知する（失敗時は握り潰す）。"""
    sev = report.get("severity")
    if not sev:
        return
    try:
        from backend.notify import push  # 遅延 import（循環回避）

        title = f"ペーパー乖離 [{sev}] {date_str}"
        msg = (
            f"Paper PnL: {report['paper_pnl_jpy']:+.0f} 円\n"
            f"Backtest sum_oos: {report['backtest_sum_oos_jpy']:+.0f} 円\n"
            f"差分: {report['diff_jpy']:+.0f} 円 ({report['diff_pct']:+.1f}%)"
        )
        await push(title=title, message=msg, priority=1 if sev == "critical" else 0)
    except Exception as exc:  # pragma: no cover - 通知失敗は致命ではない
        logger.warning("paper_backtest_sync: divergence notify failed: %s", exc)


def finalize_paper_session(session_date: str, session_pnl_jpy: float, sum_oos_snapshot: float | None) -> dict:
    """大引け後: ペーパー損益が当日のバックテスト合計 OOS を上回ったら floor を更新。"""
    h = _load_handoff()
    promoted = float(h.get("paper_promoted_floor_jpy") or 0.0)
    beat = False
    if sum_oos_snapshot is not None:
        if session_pnl_jpy >= float(sum_oos_snapshot):
            promoted = max(promoted, session_pnl_jpy)
            beat = True

    divergence = _divergence_report(session_pnl_jpy, sum_oos_snapshot)

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
    # 乖離通知（非同期 fire-and-forget）
    if divergence and divergence.get("severity"):
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
