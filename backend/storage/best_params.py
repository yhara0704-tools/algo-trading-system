"""best_params.json — 銘柄別最適パラメータの永続管理。

PDCAループが過学習なし＋改善閾値を満たす結果を発見すると自動更新する。
手動上書きも可能（source="manual"）。
get_jp_strategies と run_regime_analysis の両方がここを参照する。

ファイル形式:
{
  "7203.T": {
    "params": {"ema_fast": 3, "ema_slow": 5, "tp_pct": 0.005,
               "sl_pct": 0.003, "atr_min_pct": 0.001, "allow_short": true},
    "score":        45.2,
    "daily_pnl_jpy": 1250.0,
    "win_rate":      58.3,
    "last_updated":  "2026-04-01",
    "source":        "auto"   # "manual" | "auto"
  }
}
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_PARAMS_PATH = Path(__file__).parent.parent.parent / "data" / "best_params.json"
_PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)

# 硬コーディングされた初期値（1340通り総当たり検証結果 + 手動調整）
_DEFAULTS: dict[str, dict] = {
    "2413.T": dict(ema_fast=3, ema_slow=8,  tp_pct=0.004, sl_pct=0.002, atr_min_pct=0.001, allow_short=True),
    "3697.T": dict(ema_fast=3, ema_slow=5,  tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=True),
    "7267.T": dict(ema_fast=3, ema_slow=8,  tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=True),
    "6645.T": dict(ema_fast=3, ema_slow=8,  tp_pct=0.003, sl_pct=0.002, atr_min_pct=0.001, allow_short=True),
    "4568.T": dict(ema_fast=5, ema_slow=10, tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=True),
    "9432.T": dict(ema_fast=3, ema_slow=8,  tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=False),
    "7203.T": dict(ema_fast=3, ema_slow=5,  tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=True),
    "9433.T": dict(ema_fast=3, ema_slow=5,  tp_pct=0.004, sl_pct=0.002, atr_min_pct=0.001, allow_short=False),
    "8306.T": dict(ema_fast=5, ema_slow=13, tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=True),
    "6758.T": dict(ema_fast=3, ema_slow=8,  tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=True),
    "6098.T": dict(ema_fast=3, ema_slow=5,  tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=True),
    "6954.T": dict(ema_fast=3, ema_slow=8,  tp_pct=0.005, sl_pct=0.003, atr_min_pct=0.001, allow_short=True),
}

_lock = threading.Lock()


def _load_raw() -> dict:
    if _PARAMS_PATH.exists():
        try:
            return json.loads(_PARAMS_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("best_params.json 読み込み失敗: %s", e)
    return {}


def _save_raw(data: dict) -> None:
    _PARAMS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _init_if_empty() -> None:
    """ファイルが存在しない場合はデフォルト値で初期化する。"""
    with _lock:
        if not _PARAMS_PATH.exists():
            today = datetime.now().strftime("%Y-%m-%d")
            data = {
                sym: {
                    "params":        p,
                    "score":         0.0,
                    "daily_pnl_jpy": 0.0,
                    "win_rate":      0.0,
                    "last_updated":  today,
                    "source":        "manual",
                }
                for sym, p in _DEFAULTS.items()
            }
            _save_raw(data)
            logger.info("best_params.json を初期値で作成しました")


# ── Public API ────────────────────────────────────────────────────────────────

def get_params(symbol: str) -> dict:
    """銘柄の最新パラメータを返す。ファイル未設定時はデフォルトを返す。
    ファイルに不足キーがある場合もデフォルト値で補完する。
    """
    _fallback = dict(ema_fast=3, ema_slow=8, tp_pct=0.004, sl_pct=0.002,
                     atr_min_pct=0.001, allow_short=True)
    _init_if_empty()
    with _lock:
        data = _load_raw()
    entry = data.get(symbol)
    if entry:
        # 不足キーをフォールバックで補完（PDCAが部分更新した場合に対応）
        params = dict(_DEFAULTS.get(symbol, _fallback))
        params.update(entry["params"])
        return params
    return dict(_DEFAULTS.get(symbol, _fallback))


def get_all() -> dict:
    """全銘柄のエントリー（params + メタ情報）を返す。"""
    _init_if_empty()
    with _lock:
        return _load_raw()


def try_update(
    symbol: str,
    params: dict,
    score: float,
    daily_pnl_jpy: float,
    win_rate: float,
    is_robust: bool,
    num_trades: int,
    days_tested: int,
) -> bool:
    """条件を満たす場合のみパラメータを自動更新する。

    更新条件:
      - is_robust = True（過学習なし）
      - num_trades >= 10（十分なサンプル）
      - days_tested >= 7（最低7日分のデータ）
      - 現在のスコアより score_threshold 以上改善
      - daily_pnl_jpy > 0
    """
    if not is_robust:
        return False
    if num_trades < 10 or days_tested < 7 or daily_pnl_jpy <= 0:
        return False

    _init_if_empty()
    with _lock:
        data = _load_raw()
        current = data.get(symbol, {})
        current_score = current.get("score", -999)

        SCORE_THRESHOLD = 2.0   # 現状より2pt以上改善が必要
        if score < current_score + SCORE_THRESHOLD:
            return False

        # allow_short は変更しない（手動設定を維持）
        current_params = current.get("params", _DEFAULTS.get(symbol, {}))
        merged_params = dict(current_params)
        merged_params.update({
            k: v for k, v in params.items()
            if k in ("ema_fast", "ema_slow", "tp_pct", "sl_pct", "atr_min_pct")
        })

        data[symbol] = {
            "params":        merged_params,
            "score":         round(score, 2),
            "daily_pnl_jpy": round(daily_pnl_jpy, 1),
            "win_rate":      round(win_rate, 2),
            "last_updated":  datetime.now().strftime("%Y-%m-%d"),
            "source":        "auto",
        }
        _save_raw(data)
        logger.info(
            "best_params 更新 [%s] score %.1f→%.1f daily %.0f円 params=%s",
            symbol, current_score, score, daily_pnl_jpy, merged_params,
        )
    return True


def manual_set(symbol: str, params: dict) -> None:
    """手動でパラメータを上書きする（source='manual'としてマーク）。"""
    _init_if_empty()
    with _lock:
        data = _load_raw()
        existing = data.get(symbol, {})
        existing["params"] = params
        existing["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        existing["source"] = "manual"
        data[symbol] = existing
        _save_raw(data)
